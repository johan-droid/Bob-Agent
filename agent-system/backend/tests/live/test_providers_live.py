"""Optional live provider tests — run ONLY when explicitly enabled.

Activated per provider via environment variables; normal CI never runs them
(a temporarily unavailable provider must not fail the suite):

    LIVE_GROQ_TEST=true        GROQ_API_KEY=gsk_...        pytest tests/live -q
    LIVE_GEMINI_TEST=true      GEMINI_API_KEY=...
    LIVE_OPENROUTER_TEST=true  OPENROUTER_API_KEY=...
    LIVE_OPENCODE_TEST=true    OPENCODE_API_KEY=...

Each test makes one minimal real API call (single-shot + streaming + the
staged diagnostics), asserting only that Bob's adapter seam round-trips the
provider — not model quality.
"""

from __future__ import annotations

import os

import pytest

from agent_system.config import Settings
from agent_system.services.providers import diagnose_provider

pytestmark = pytest.mark.skipif(
    not os.environ.get("LIVE_PROVIDER_TESTS"),
    reason="live provider tests are opt-in (set LIVE_PROVIDER_TESTS=true)",
)


def _live_enabled(provider_env: str) -> bool:
    return (
        os.environ.get(provider_env, "").strip().lower() == "true"
        and bool(Settings(_env_file=None).provider_api_key(_PROVIDER_KEY[provider_env]))
    )


_PROVIDER_KEY = {
    "LIVE_GROQ_TEST": "groq",
    "LIVE_GEMINI_TEST": "gemini",
    "LIVE_OPENROUTER_TEST": "openrouter",
    "LIVE_OPENCODE_TEST": "opencode",
}


def _skip_unless(provider_env: str) -> None:
    if not _live_enabled(provider_env):
        pytest.skip(f"{provider_env}=true and its API key required")


class TestLiveGroq:
    def test_completion_and_streaming(self) -> None:
        _skip_unless("LIVE_GROQ_TEST")
        settings = Settings(_env_file=None)
        result = diagnose_provider(settings, "groq", include_tools=True)
        stages = result["stages"]
        assert stages["credentials"]["status"] == "PASS"
        assert stages["completion"]["status"] == "PASS", stages["completion"]
        assert stages["streaming"]["status"] == "PASS", stages["streaming"]


class TestLiveGemini:
    def test_completion_and_streaming(self) -> None:
        _skip_unless("LIVE_GEMINI_TEST")
        settings = Settings(_env_file=None)
        result = diagnose_provider(settings, "gemini", include_tools=True)
        stages = result["stages"]
        assert stages["completion"]["status"] == "PASS", stages["completion"]
        assert stages["streaming"]["status"] == "PASS", stages["streaming"]


class TestLiveOpenRouter:
    def test_completion(self) -> None:
        _skip_unless("LIVE_OPENROUTER_TEST")
        settings = Settings(_env_file=None)
        result = diagnose_provider(settings, "openrouter", include_tools=False)
        stages = result["stages"]
        assert stages["completion"]["status"] == "PASS", stages["completion"]


class TestLiveOpenCode:
    def test_completion(self) -> None:
        _skip_unless("LIVE_OPENCODE_TEST")
        settings = Settings(_env_file=None)
        result = diagnose_provider(settings, "opencode", include_tools=False)
        stages = result["stages"]
        assert stages["completion"]["status"] == "PASS", stages["completion"]
