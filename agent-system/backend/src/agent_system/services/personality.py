"""Personality (v3.1 Phase 17, §14 feature note).

Versioned per-agent personality configs (tone / verbosity / reasoning style /
prompt override). The feedback loop infers adjustments after N ratings —
adjustments touch ONLY presentation fields; security, permission, safety, and
limit settings are structurally unreachable from here.
"""

from __future__ import annotations

from typing import Any

from agent_system.domain import ids
from agent_system.domain.events import utcnow
from agent_system.infra.db import session_scope
from agent_system.infra.models import AgentPersonality, FeedbackLog

LEARNING_THRESHOLD = 10  # ratings required before inference runs


class PersonalityError(ValueError):
    pass


# Fields the feedback loop may adjust — everything else is out of bounds.
_ADJUSTABLE = ("tone", "verbosity", "reasoning_style", "system_prompt_override")


class PersonalityManager:
    def __init__(self, factory: Any, bus: Any = None) -> None:
        self._factory = factory

    def get(self, agent_id: str) -> dict[str, Any] | None:
        with session_scope(self._factory) as db:
            row = (
                db.query(AgentPersonality)
                .filter_by(agent_id=agent_id)
                .order_by(AgentPersonality.version.desc())
                .first()
            )
            if row is None:
                return None
            return self._out(row)

    def update(self, agent_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply allowed updates; bumps version. Unknown keys rejected."""
        bad = set(updates) - set(_ADJUSTABLE)
        if bad:
            raise PersonalityError(f"fields not personality-adjustable: {sorted(bad)}")
        with session_scope(self._factory) as db:
            row = (
                db.query(AgentPersonality)
                .filter_by(agent_id=agent_id)
                .order_by(AgentPersonality.version.desc())
                .first()
            )
            created_here = row is None
            if row is None:
                row = AgentPersonality(id=ids.new_id("pers"), agent_id=agent_id)
                db.add(row)
                db.flush()
            for key, value in updates.items():
                setattr(row, key, value)
            if not created_here:
                row.version += 1
            row.updated_at = utcnow()
            return self._out(row)

    def record_feedback(
        self, agent_id: str, rating: int, comment: str | None, session_id: str | None
    ) -> dict[str, Any]:
        if not 1 <= rating <= 5:
            raise PersonalityError("rating must be 1..5")
        with session_scope(self._factory) as db:
            db.add(
                FeedbackLog(
                    id=ids.new_id("fb"),
                    agent_id=agent_id,
                    session_id=session_id,
                    rating=rating,
                    comment=comment[:2000] if comment else None,
                )
            )
            count = db.query(FeedbackLog).filter_by(agent_id=agent_id).count()
        return {
            "agent_id": agent_id,
            "ratings_recorded": count,
            "learning_threshold": LEARNING_THRESHOLD,
        }

    def learn_from_feedback(self, agent_id: str) -> dict[str, Any]:
        """N>=10 ratings -> conservative prompt-only adjustments.

        Inference is deterministic and bounded: low average rating lowers
        verbosity toward concise; high average keeps current. Security fields
        are unreachable by construction (only _ADJUSTABLE keys are set).
        """
        with session_scope(self._factory) as db:
            ratings = [
                r.rating
                for r in db.query(FeedbackLog)
                .filter_by(agent_id=agent_id)
                .order_by(FeedbackLog.created_at.desc())
                .limit(LEARNING_THRESHOLD)
                .all()
            ]
        if len(ratings) < LEARNING_THRESHOLD:
            return {
                "agent_id": agent_id,
                "learned": False,
                "reason": f"need {LEARNING_THRESHOLD} ratings, have {len(ratings)}",
            }
        avg = sum(ratings) / len(ratings)
        current = self.get(agent_id) or {}
        updates: dict[str, Any] = {}
        if avg < 2.5:
            updates["verbosity"] = max(1, int(current.get("verbosity", 5)) - 2)
            updates["reasoning_style"] = "concise"
        elif avg > 4.5:
            updates["verbosity"] = min(10, int(current.get("verbosity", 5)) + 1)
        if updates:
            result = self.update(agent_id, updates)
            return {
                "agent_id": agent_id,
                "learned": True,
                "average_rating": round(avg, 2),
                "version": result["version"],
                "updates": updates,
            }
        return {
            "agent_id": agent_id,
            "learned": True,
            "average_rating": round(avg, 2),
            "updates": {},
        }

    @staticmethod
    def _out(row: AgentPersonality) -> dict[str, Any]:
        return {
            "agent_id": row.agent_id,
            "tone": row.tone,
            "verbosity": row.verbosity,
            "reasoning_style": row.reasoning_style,
            "system_prompt_override": row.system_prompt_override,
            "version": row.version,
            "learned_from_feedback_count": row.learned_from_feedback_count,
        }
