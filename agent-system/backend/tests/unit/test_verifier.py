"""Unit & regression tests for Verifier, Truth Boundaries, and Telegram failure relaying."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from agent_system.config import get_settings
from agent_system.domain.events import Event
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Base, DeliveryOutbox
from agent_system.services.gateway import GatewayRelay
from agent_system.services.outbox import Outbox
from agent_system.services.verifier import Verifier


def _setup_db(tmp_path: Any) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'test_verifier_relay.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_verifier_deterministic_failure() -> None:
    verifier = Verifier()
    # stopped with error
    res = verifier.verify({"goal": "test"}, {"stopped": "error", "output": "provider timeout"})
    assert res.passed is False
    assert "ended in error" in res.reason

    # verified is False
    res2 = verifier.verify(
        {"goal": "test"}, {"verified": False, "verification_reason": "file not modified"}
    )
    assert res2.passed is False
    assert "file not modified" in res2.reason

    # tests_failed > 0
    res3 = verifier.verify({"goal": "test"}, {"tests_failed": 2})
    assert res3.passed is False
    assert "tests_failed=2" in res3.reason


def test_gateway_relay_failed_task_reports_truthful_failure(tmp_path: Any) -> None:
    factory = _setup_db(tmp_path)
    settings = get_settings()
    bus = EventBus()
    outbox = Outbox(factory, settings)
    relay = GatewayRelay(factory, outbox, bus)

    # Mock chat_for_task resolution
    relay._chat_for_task = MagicMock(return_value=12345)  # type: ignore[assignment]

    # Emit task.failed event
    failed_event = Event(
        type="task.failed",
        task_id="task_fail_1",
        session_id="sess_1",
        actor="worker",
        payload={"error": "LLM provider rate limit exceeded (HTTP 429)"},
    )

    handled = relay._relay(failed_event)
    assert handled is True

    # Check outbox item text in DB
    with factory() as db:
        items = db.query(DeliveryOutbox).all()
        assert len(items) == 1
        assert "Done" not in items[0].text
        assert "ran into an issue" in items[0].text or "failed" in items[0].text.lower()
