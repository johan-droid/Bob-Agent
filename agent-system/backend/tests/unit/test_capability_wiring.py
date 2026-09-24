"""Capability wiring contracts — the seams that used to drift silently.

Three real defects are pinned here:

1. **Duplicate Telegram responses.** ``GatewayRelay.flush()`` enqueued an
   event unconditionally while the recovery sweep's ``replay()`` deduped by
   ``event_id``. When a drive outlived the 10s recovery interval (or paused on
   an approval), the same event reached the outbox twice and the user saw the
   same message twice. The guard now lives on the single write seam
   (``Outbox.enqueue``), so *every* event-driven enqueue is idempotent.
2. **Skill/tool drift.** ``SKILL.md`` packs name the tools the model should
   use; a skill naming a tool that does not exist produces calls that always
   fail. Every backticked tool name in a ``## Required Tools`` section must be a
   registered capability, asserted here so it cannot drift again.
3. **Missing logical web capabilities.** Prompts and skills speak
   ``web_search`` / ``web_fetch``; the registry only had the ``research_*``
   group names, so a model emitting the documented name was told the
   capability did not exist.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_system.infra.db import make_engine, make_session_factory, session_scope
from agent_system.infra.models import Base, DeliveryOutbox
from agent_system.services.outbox import Outbox
from agent_system.services.tools.registry import build_registry

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

#: Capabilities that only register when the matching integration is configured
#: (OpenConnector / MCP). Skills may legitimately name them.
_CONDITIONAL_CAPABILITIES = frozenset(
    {"openconnector_execute", "openconnector_list", "mcp_call", "mcp_list"}
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        openconnector_base_url="",
        mcp_servers="[]",
        tools_plugin_dir="tools_plugins",
    )


def _registry_names() -> set[str]:
    return set(build_registry(_settings()).names())


def _required_tools(skill_md: Path) -> list[str]:
    """Backticked capability names listed under ``## Required Tools``."""
    text = skill_md.read_text(encoding="utf-8")
    match = re.search(
        r"^##\s+Required Tools\s*$(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        return []
    return re.findall(r"`([a-z][a-z0-9_]*)`", match.group(1))


class TestSkillCapabilityWiring:
    """A skill must never instruct the model to call a capability that is absent."""

    def test_every_seed_skill_names_real_capabilities(self) -> None:
        known = _registry_names() | _CONDITIONAL_CAPABILITIES
        skill_files = sorted(SKILLS_DIR.glob("*/SKILL.md"))
        assert skill_files, "expected shipped skill packs on disk"
        offenders: list[str] = []
        for skill_md in skill_files:
            for name in _required_tools(skill_md):
                if name not in known:
                    offenders.append(f"{skill_md.parent.name}: {name}")
        assert not offenders, (
            "skills reference capabilities that are not registered "
            f"(the model would emit calls that always fail): {offenders}"
        )

    def test_guard_is_not_vacuous(self) -> None:
        # A malformed heading would make the drift check pass trivially.
        assert _required_tools(SKILLS_DIR / "research" / "SKILL.md")


class TestWebCapabilityAliases:
    """The documented logical web tools exist, and are read-tier."""

    def test_web_search_and_web_fetch_registered(self) -> None:
        registry = build_registry(_settings())
        assert registry.get("web_search") is not None
        assert registry.get("web_fetch") is not None

    def test_web_aliases_are_read_tier_and_unapproved(self) -> None:
        registry = build_registry(_settings())
        for name in ("web_search", "web_fetch"):
            tool = registry.get(name)
            assert tool is not None
            # Read tier: browsing public documents needs no approval.
            assert tool.risk == "read"
            assert tool.permission_required() is False

    def test_web_search_requires_a_query(self) -> None:
        registry = build_registry(_settings())
        tool = registry.get("web_search")
        assert tool is not None
        assert "query" in tool.parameters["required"]


class TestOutboxIdempotency:
    """One event -> at most one outbound message (duplicate-response fix)."""

    def _outbox(self, tmp_path: Path) -> tuple[Outbox, Any]:
        engine = make_engine(f"sqlite:///{tmp_path / 'outbox.db'}")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        outbox = Outbox(factory, SimpleNamespace(telegram_bot_token=None))
        return outbox, factory

    @staticmethod
    def _rows(factory: Any) -> list[Any]:
        with session_scope(factory) as db:
            return db.query(DeliveryOutbox).all()

    def test_same_event_id_enqueues_once(self, tmp_path: Path) -> None:
        outbox, factory = self._outbox(tmp_path)
        first = outbox.enqueue(
            kind="notification",
            chat_id=700,
            text="the final answer",
            event_id="evt_duplicate_1",
        )
        second = outbox.enqueue(
            kind="notification",
            chat_id=700,
            text="the final answer",
            event_id="evt_duplicate_1",
        )
        assert first is not None
        # The duplicate collapses onto the SAME row instead of enqueueing twice.
        assert second == first
        assert len(self._rows(factory)) == 1

    def test_distinct_events_still_enqueue(self, tmp_path: Path) -> None:
        outbox, factory = self._outbox(tmp_path)
        outbox.enqueue(kind="notification", chat_id=700, text="a", event_id="evt_a")
        outbox.enqueue(kind="notification", chat_id=700, text="b", event_id="evt_b")
        assert len(self._rows(factory)) == 2

    def test_enqueue_without_event_id_is_never_deduped(self, tmp_path: Path) -> None:
        # Acks and chat replies carry no event id; they must keep working.
        outbox, factory = self._outbox(tmp_path)
        outbox.enqueue(kind="command_response", chat_id=700, text="ok")
        outbox.enqueue(kind="command_response", chat_id=700, text="ok")
        assert len(self._rows(factory)) == 2
