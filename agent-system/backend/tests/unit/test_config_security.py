"""Production secret validation regressions."""

import pytest


@pytest.mark.parametrize(
    "secret", ["", "   ", "dev-only-secret-change-me", "change-me-to-a-long-random-string"]
)
@pytest.mark.parametrize("field", ["api_session_secret", "agent_bootstrap_secret"])
def test_production_rejects_published_or_empty_secrets(secret: str, field: str) -> None:
    from agent_system.config import Settings

    values = {
        "agent_env": "production",
        "api_session_secret": "private-session-secret-for-test",
        "agent_bootstrap_secret": "private-bootstrap-secret-for-test",
        field: secret,
    }
    with pytest.warns(RuntimeWarning), pytest.raises(RuntimeError, match="Refusing to start"):
        Settings(_env_file=None, **values)
