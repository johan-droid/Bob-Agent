"""Unit + security tests — secret redaction (v3.1 §14)."""

from __future__ import annotations

from agent_system.services.secrets import (
    is_secret_key,
    is_secret_path,
    redact_dict,
    redact_value,
)


class TestKeyDetection:
    def test_markers(self) -> None:
        assert is_secret_key("API_KEY")
        assert is_secret_key("anthropic_api_key")
        assert is_secret_key("db_password")
        assert is_secret_key("ACCESS_TOKEN")
        assert is_secret_key("DATABASE_URL")
        assert not is_secret_key("task_count")
        assert not is_secret_key("model_name")


class TestValueRedaction:
    def test_openai_key(self) -> None:
        assert (
            redact_value("call with sk-abcdefghijklmnop123456 please")
            == "call with [REDACTED] please"
        )

    def test_aws_key(self) -> None:
        assert redact_value("AKIAIOSFODNN7EXAMPLE") == "[REDACTED]"

    def test_jwt(self) -> None:
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        assert redact_value(f"Bearer {jwt}") == "Bearer [REDACTED]"

    def test_github_token(self) -> None:
        assert redact_value("token ghp_0123456789abcdefghijklmnopqrstuvwxyz") == "token [REDACTED]"

    def test_private_key_block(self) -> None:
        blob = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
        assert redact_value(blob) == "[REDACTED]"

    def test_plain_text_untouched(self) -> None:
        assert redact_value("hello world") == "hello world"


class TestDictRedaction:
    def test_nested(self) -> None:
        payload = {
            "query": "test",
            "api_key": "sk-abcdefghijklmnop123456",
            "nested": {"password": "hunter2", "note": "safe"},
        }
        cleaned = redact_dict(payload)  # type: ignore[arg-type]
        assert cleaned["api_key"] == "[REDACTED]"
        assert cleaned["nested"] == {"password": "[REDACTED]", "note": "safe"}  # type: ignore[index]
        assert cleaned["query"] == "test"


class TestPathDetection:
    def test_env_files(self) -> None:
        assert is_secret_path(".env")
        assert is_secret_path("config/.env.local")
        assert is_secret_path("app/.env.production")

    def test_ssh_and_cloud(self) -> None:
        assert is_secret_path(".ssh/id_rsa")
        assert is_secret_path("home/user/.aws/credentials")
        assert is_secret_path(".kube/config")

    def test_named_secret_files(self) -> None:
        assert is_secret_path("secrets.json")
        assert is_secret_path("config/service-account.json")

    def test_normal_paths_allowed(self) -> None:
        assert not is_secret_path("src/main.py")
        assert not is_secret_path("requirements.txt")
        assert not is_secret_path("docs/readme.md")
