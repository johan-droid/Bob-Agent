"""Swarm manager: bounded multi-worker fan-out under one master (v1, additive)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_WORKERS = 4
ABSOLUTE_MAX_WORKERS = 12


@dataclass
class SwarmSpec:
    role: str
    title: str
    goal: str
    task_type: str = "general"
    depends_on: tuple[str, ...] = ()


@dataclass
class SwarmDecision:
    use_swarm: bool
    complexity: str = "LOW"
    worker_count: int = 1
    reason: str = ""
    specs: list[SwarmSpec] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "use_swarm": self.use_swarm,
            "complexity": self.complexity,
            "worker_count": self.worker_count,
            "reason": self.reason,
            "specs": [
                {
                    "role": s.role,
                    "title": s.title,
                    "goal": s.goal,
                    "task_type": s.task_type,
                    "depends_on": list(s.depends_on),
                }
                for s in self.specs
            ],
        }


_COMPLEXITY_SIGNALS = (
    "audit",
    "security",
    "release",
    "migrate",
    "refactor",
    "end-to-end",
    "e2e",
    "full",
    "entire",
    "repository",
    "repo",
    "and",
    "plus",
    "also",
)


def estimate_complexity(goal: str) -> tuple[str, int]:
    """Estimate LOW/MEDIUM/HIGH + workstream count from goal text (pure)."""
    text = (goal or "").lower()
    signals = sum(1 for s in _COMPLEXITY_SIGNALS if s in text)
    workstreams = 1
    for sep in (" and ", " plus ", " also ", ";", " then "):
        workstreams += max(0, text.count(sep))
    workstreams = max(1, min(workstreams, ABSOLUTE_MAX_WORKERS))
    if signals >= 4 or workstreams >= 5 or len(text) > 600:
        return "HIGH", workstreams
    if signals >= 2 or workstreams >= 2 or len(text) > 200:
        return "MEDIUM", workstreams
    return "LOW", workstreams


_ROLE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("secur", "SECURITY_AUDITOR"),
    ("audit", "SECURITY_AUDITOR"),
    ("test", "TEST_ENGINEER"),
    ("verify", "TEST_ENGINEER"),
    ("doc", "DOCUMENTATION_AGENT"),
    ("release", "DEVOPS_AGENT"),
    ("deploy", "DEVOPS_AGENT"),
    ("research", "RESEARCHER"),
    ("investigat", "RESEARCHER"),
    ("browser", "BROWSER_AGENT"),
    ("frontend", "FRONTEND_ENGINEER"),
    ("backend", "BACKEND_ENGINEER"),
    ("database", "DATABASE_ENGINEER"),
    ("debug", "DEBUGGER"),
    ("fix", "DEBUGGER"),
    ("bug", "DEBUGGER"),
)


def _role_for_chunk(chunk: str, fallback: str = "CODER") -> str:
    lowered = f" {chunk.lower()} "
    for keyword, role in _ROLE_KEYWORDS:
        if keyword in lowered or keyword in chunk.lower():
            return role
    return fallback


def _split_goal(goal: str, count: int) -> list[str]:
    import re

    parts = re.split(r"\s+(?:and|plus|also|then)\s+|;\s*", goal.strip(), flags=re.I)
    parts = [p.strip(" .") for p in parts if p.strip()]
    if len(parts) >= count:
        return parts[:count]
    if not parts:
        return [goal] * count
    out = list(parts)
    while len(out) < count:
        longest = max(range(len(out)), key=lambda i: len(out[i]))
        text = out.pop(longest)
        mid = len(text) // 2
        cut = text.rfind(" ", 0, mid)
        cut = cut if cut > 20 else mid
        out[longest:longest] = [text[:cut].strip(), text[cut:].strip()]
        out = [o for o in out if o]
    return out[:count]


def plan_swarm(
    goal: str,
    max_workers: int = DEFAULT_MAX_WORKERS,
    task_type: str = "general",
) -> SwarmDecision:
    """Decide single-agent vs bounded swarm (pure; no I/O, no task creation)."""
    cap = max(1, min(int(max_workers or 1), ABSOLUTE_MAX_WORKERS))
    complexity, workstreams = estimate_complexity(goal)
    if complexity == "LOW":
        return SwarmDecision(
            use_swarm=False,
            complexity="LOW",
            worker_count=1,
            reason="low complexity: single agent owns the whole job",
        )
    count = min(2, cap) if complexity == "MEDIUM" else min(max(3, workstreams), cap, 5)
    chunks = _split_goal(goal, count)
    specs = [
        SwarmSpec(
            role=_role_for_chunk(chunk, "CODER" if i == 0 else "REVIEWER"),
            title=f"{_role_for_chunk(chunk).title()} workstream {i + 1}",
            goal=chunk.strip(),
            task_type=task_type,
        )
        for i, chunk in enumerate(chunks)
    ]
    return SwarmDecision(
        use_swarm=True,
        complexity=complexity,
        worker_count=len(specs),
        reason=f"{complexity} complexity: {len(specs)} bounded workers under one master",
        specs=specs,
    )


def create_swarm(
    bus: Any,
    factory: Any,
    session_id: str,
    master_task_id: str,
    decision: SwarmDecision,
    supervisor: Any | None = None,
    owner_user_id: str | None = None,
) -> list[str]:
    """Materialize a swarm: one child task per spec + membership rows.

    Children are ordinary supervisor tasks (identical lifecycle/permissions/
    verifier); the membership table only records master ownership.
    """
    from agent_system.domain import ids
    from agent_system.domain.events import Event
    from agent_system.infra.db import session_scope
    from agent_system.infra.models import SwarmMember, Task

    if not decision.specs:
        raise ValueError("swarm decision carries no worker specs")
    if len(decision.specs) > ABSOLUTE_MAX_WORKERS:
        raise ValueError(f"swarm exceeds absolute cap {ABSOLUTE_MAX_WORKERS}")
    if supervisor is None:
        from agent_system.services.orchestrator import Supervisor

        supervisor = Supervisor(bus)
    worker_ids: list[str] = []
    # Child tasks are created through the supervisor, which opens its own
    # transaction per call. On SQLite a second writer would block against the
    # outer transaction below ("database is locked"), so create the tasks
    # FIRST, then record membership + events atomically in one pass.
    for spec in decision.specs:
        task_id = supervisor.add_task(
            factory,
            session_id,
            task_type=spec.task_type,
            title=spec.title,
            input_json={
                "goal": spec.goal,
                "master_task_id": master_task_id,
                "worker_role": spec.role,
            },
        )
        worker_ids.append(task_id)
    with session_scope(factory) as db:
        for spec, task_id in zip(decision.specs, worker_ids, strict=True):
            if owner_user_id is not None:
                child_row = db.get(Task, task_id)
                if child_row is not None:
                    child_row.owner_user_id = owner_user_id
            db.add(
                SwarmMember(
                    id=ids.new_id("swarm"),
                    master_task_id=master_task_id,
                    worker_task_id=task_id,
                    role=spec.role,
                    status="pending",
                )
            )
            bus.emit(
                Event(
                    type="swarm.worker_created",
                    session_id=session_id,
                    task_id=master_task_id,
                    actor="swarm_manager",
                    payload={"worker_task_id": task_id, "role": spec.role, "title": spec.title},
                ),
                db,
            )
        bus.emit(
            Event(
                type="swarm.created",
                session_id=session_id,
                task_id=master_task_id,
                actor="swarm_manager",
                payload={"worker_count": len(worker_ids), "complexity": decision.complexity},
            ),
            db,
        )
    try:
        supervisor.plan(factory, session_id)
    except Exception:
        pass
    try:
        with session_scope(factory) as db2:
            rows = db2.query(SwarmMember).filter(SwarmMember.master_task_id == master_task_id).all()
            for row in rows:
                if row.status == "pending":
                    row.status = "queued"
    except Exception:
        pass
    return worker_ids


def verify_swarm(
    factory: Any, bus: Any, master_task_id: str, session_id: str | None = None
) -> dict[str, Any]:
    """Master verification: aggregate child outcomes (additive, read-only)."""
    from agent_system.domain.events import Event, utcnow
    from agent_system.infra.db import session_scope
    from agent_system.infra.models import SwarmMember, Task

    with session_scope(factory) as db:
        members = db.query(SwarmMember).filter(SwarmMember.master_task_id == master_task_id).all()
        summary: dict[str, Any] = {
            "total": len(members),
            "succeeded": 0,
            "failed": 0,
            "pending": 0,
            "roles": {},
        }
        for member in members:
            child = db.get(Task, member.worker_task_id)
            state = child.state if child is not None else "missing"
            if state == "SUCCEEDED":
                summary["succeeded"] += 1
                member.status = "verified"
                member.verified_at = utcnow()
            elif state in ("FAILED", "CANCELLED", "missing"):
                summary["failed"] += 1
                member.status = "failed"
            else:
                summary["pending"] += 1
            summary["roles"][member.role] = summary["roles"].get(member.role, 0) + 1
        passed = bool(members) and summary["failed"] == 0 and summary["pending"] == 0
        try:
            bus.emit(
                Event(
                    type="swarm.verified",
                    session_id=session_id,
                    task_id=master_task_id,
                    actor="swarm_manager",
                    payload={"passed": passed, **summary},
                ),
                db,
            )
        except Exception:
            pass
        summary["passed"] = passed
        return summary


__all__ = [
    "ABSOLUTE_MAX_WORKERS",
    "DEFAULT_MAX_WORKERS",
    "SwarmDecision",
    "SwarmSpec",
    "create_swarm",
    "estimate_complexity",
    "plan_swarm",
    "verify_swarm",
]
