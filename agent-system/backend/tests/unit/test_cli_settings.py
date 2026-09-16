"""Unit tests — settings catalog, CRUD, wizard, CLI, and REPL wiring."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_system.cli import settings as settings_mod
from agent_system.cli.main import EXIT_OK, app
from agent_system.config import Settings

runner = CliRunner()


@pytest.fixture()
def isolated(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return tmp_path


class TestCatalog:
    def test_covers_every_settings_field(self) -> None:
        fields = set(Settings.model_fields)
        cataloged = {s.field for s in settings_mod.catalog()}
        assert cataloged == fields

    def test_env_names_are_uppercase_fields(self) -> None:
        for spec in settings_mod.catalog():
            assert spec.env == spec.field.upper()

    def test_secrets_detected(self) -> None:
        by_env = {s.env: s for s in settings_mod.catalog()}
        assert by_env["GROQ_API_KEY"].secret is True
        assert by_env["API_SESSION_SECRET"].secret is True
        assert by_env["TELEGRAM_BOT_TOKEN"].secret is True
        assert by_env["DAILY_BUDGET_USD"].secret is False

    def test_groups_valid(self) -> None:
        for spec in settings_mod.catalog():
            assert spec.group in settings_mod.GROUP_ORDER


class TestParse:
    def _spec(self, env: str) -> Any:
        return settings_mod._find_spec(env)

    def test_bool_words(self) -> None:
        from agent_system.cli.settings import SettingSpec

        spec = SettingSpec("x", "X", "core", False, "bool", "")
        assert settings_mod.parse_value(spec, "yes") is True
        assert settings_mod.parse_value(spec, "0") is False
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.parse_value(spec, "maybe")

    def test_int_float(self) -> None:
        assert settings_mod.parse_value(self._spec("MAX_RETRIES"), "5") == 5
        assert settings_mod.parse_value(self._spec("DAILY_BUDGET_USD"), "2.5") == 2.5

    def test_bad_int_rejected(self) -> None:
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.parse_value(self._spec("MAX_RETRIES"), "many")

    def test_provider_validated(self) -> None:
        assert settings_mod.parse_value(self._spec("DEFAULT_PROVIDER"), "groq") == "groq"
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.parse_value(self._spec("DEFAULT_PROVIDER"), "skynet")

    def test_base_url_validated(self) -> None:
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.parse_value(self._spec("GROQ_BASE_URL"), "not-a-url")

    def test_headers_must_be_json_object(self) -> None:
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.parse_value(self._spec("PROVIDER_EXTRA_HEADERS"), "[1,2]")
        out = settings_mod.parse_value(self._spec("PROVIDER_EXTRA_HEADERS"), '{"a":"b"}')
        assert json.loads(out) == {"a": "b"}

    def test_unknown_key(self, isolated: pathlib.Path) -> None:
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.get_setting("NOPE_NOT_REAL")


class TestRoundtrip:
    def test_set_get_unset(self, isolated: pathlib.Path) -> None:
        row = settings_mod.set_setting("daily_budget_usd", "7.5")
        assert row["value"] == "7.5"
        assert row["source"] == ".env.local"

        row = settings_mod.get_setting("DAILY_BUDGET_USD")
        assert row["value"] == "7.5"

        result = settings_mod.unset_setting("DAILY_BUDGET_USD")
        assert result["removed"] == "True"
        row = settings_mod.get_setting("DAILY_BUDGET_USD")
        assert row["source"] == "default"

    def test_secret_masked_by_default(self, isolated: pathlib.Path) -> None:
        settings_mod.set_setting("GROQ_API_KEY", "sk-live-123")
        assert settings_mod.get_setting("GROQ_API_KEY")["value"] == "•••••••• (set)"
        assert "sk-live" in settings_mod.get_setting("GROQ_API_KEY", show_secrets=True)["value"]

    def test_list_groups(self, isolated: pathlib.Path) -> None:
        rows = settings_mod.list_settings(group="auth")
        assert rows and all(r["group"] == "auth" for r in rows)
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.list_settings(group="nope")

    def test_check_ok(self, isolated: pathlib.Path) -> None:
        report = settings_mod.check_settings()
        assert report["ok"] is True
        assert "default_provider" in report


class TestWizard:
    def test_cancel_returns_empty(self, isolated: pathlib.Path, monkeypatch) -> None:
        from agent_system.cli import setup as setup_mod

        monkeypatch.setattr(setup_mod, "_masked", lambda *a, **k: "")
        monkeypatch.setattr(setup_mod, "_ask", lambda *a, **k: "")
        monkeypatch.setattr(setup_mod, "_confirm", lambda *a, **k: False)
        assert settings_mod.run_wizard() == {}

    def test_yes_mode_never_prompts(self, isolated: pathlib.Path, monkeypatch) -> None:
        from agent_system.cli import setup as setup_mod

        def _boom(*a, **k):
            raise AssertionError("no prompts in --yes mode")

        monkeypatch.setattr(setup_mod, "_masked", _boom)
        monkeypatch.setattr(setup_mod, "_ask", _boom)
        monkeypatch.setattr(setup_mod, "_confirm", _boom)
        assert settings_mod.run_wizard(yes=True) == {}


class TestCli:
    def test_list(self, isolated: pathlib.Path) -> None:
        result = runner.invoke(app, ["settings", "list", "--group", "cost"])
        assert result.exit_code == EXIT_OK, result.output
        assert "DAILY_BUDGET_USD" in result.output

    def test_set_get_unset(self, isolated: pathlib.Path) -> None:
        assert runner.invoke(app, ["settings", "set", "MAX_RETRIES", "5"]).exit_code == EXIT_OK
        result = runner.invoke(app, ["settings", "get", "MAX_RETRIES"])
        assert result.exit_code == EXIT_OK and "5" in result.output
        assert runner.invoke(app, ["settings", "unset", "MAX_RETRIES"]).exit_code == EXIT_OK

    def test_set_bad_value_fails(self, isolated: pathlib.Path) -> None:
        result = runner.invoke(app, ["settings", "set", "MAX_RETRIES", "many"])
        assert result.exit_code != EXIT_OK

    def test_check(self, isolated: pathlib.Path) -> None:
        result = runner.invoke(app, ["settings", "check"])
        assert result.exit_code == EXIT_OK, result.output

    def test_json_schema(self, isolated: pathlib.Path) -> None:
        result = runner.invoke(app, ["--json", "settings", "get", "API_PORT"])
        assert result.exit_code == EXIT_OK
        payload = json.loads(result.output)
        assert payload["key"] == "API_PORT"


class TestReplWiring:
    def test_settings_list_inline(self, isolated: pathlib.Path, capsys) -> None:
        from agent_system.cli import chat as chat_mod

        chat_mod.handle_line(chat_mod.ChatState(), "/settings list cost")
        assert "DAILY_BUDGET_USD" in capsys.readouterr().out

    def test_settings_set_inline(self, isolated: pathlib.Path, capsys) -> None:
        from agent_system.cli import chat as chat_mod

        chat_mod.handle_line(chat_mod.ChatState(), "/settings set MAX_RETRIES 4")
        assert "MAX_RETRIES" in capsys.readouterr().out

    def test_soul_missing_hint(self, isolated: pathlib.Path, capsys) -> None:
        from agent_system.cli import chat as chat_mod

        chat_mod.handle_line(chat_mod.ChatState(), "/soul")
        assert "SOUL.md" in capsys.readouterr().out

    def test_soul_shows_file(self, isolated: pathlib.Path, monkeypatch, capsys) -> None:
        from agent_system.cli import chat as chat_mod

        (isolated / "SOUL.md").write_text("# Soul\n\nYou are Bob.\n", encoding="utf-8")
        monkeypatch.chdir(isolated)
        chat_mod.handle_line(chat_mod.ChatState(), "/soul")
        assert "You are Bob" in capsys.readouterr().out
