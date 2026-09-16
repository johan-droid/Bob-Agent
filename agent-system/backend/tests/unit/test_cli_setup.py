"""Unit tests — interactive setup wizard (writes .env.local)."""

from __future__ import annotations

import pathlib

from agent_system.cli import setup as setup_mod


def test_write_env_creates_or_merges(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = setup_mod.write_env({"ANTHROPIC_API_KEY": "sk-abc"})
    assert path.resolve() == (tmp_path / ".env.local").resolve()
    raw = (tmp_path / ".env.local").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-abc" in raw

    # Merge adds a new key, keeps existing.
    setup_mod.write_env({"OPENAI_API_KEY": "sk-xyz"})
    raw = (tmp_path / ".env.local").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-abc" in raw
    assert "OPENAI_API_KEY=sk-xyz" in raw


def test_write_env_quotes_values_with_leading_hash(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    setup_mod.write_env({"SOME_KEY": "#notcomment"})
    raw = (tmp_path / ".env.local").read_text(encoding="utf-8")
    assert 'SOME_KEY="#notcomment"' in raw


def test_gather_cancelled_returns_none(monkeypatch) -> None:
    # Simulate confirming "no" to the final write prompt -> returns None.
    monkeypatch.setattr(setup_mod, "_masked", lambda *a, **k: "")
    monkeypatch.setattr(setup_mod, "_ask", lambda *a, **k: "")
    monkeypatch.setattr(setup_mod, "_confirm", lambda *a, **k: False)
    result = setup_mod.gather()
    assert result is None


def test_defaults_reads_dotenv(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("VAULT_PATH=/x/y\nFOO=bar\n", encoding="utf-8")
    defaults = setup_mod._defaults()
    assert defaults["VAULT_PATH"] == "/x/y"
    assert defaults["FOO"] == "bar"


def test_gather_unknown_section_returns_none(monkeypatch) -> None:
    result = setup_mod.gather(section="nope")
    assert result is None


def test_gather_yes_mode_never_prompts(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOB_PROFILE", str(tmp_path / "profile.json"))

    def _boom(*a, **k):
        raise AssertionError("no prompts allowed in --yes mode")

    monkeypatch.setattr(setup_mod, "_masked", _boom)
    monkeypatch.setattr(setup_mod, "_ask", _boom)
    monkeypatch.setattr(setup_mod, "_confirm", _boom)
    result = setup_mod.gather(yes=True, skip_test=True)
    assert result is not None
    assert result["DEFAULT_PROVIDER"] == "echo"
    assert "API_SESSION_SECRET" in result
    assert "AGENT_BOOTSTRAP_SECRET" in result


def test_profile_roundtrip_and_no_secrets(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "setup.json"
    setup_mod.save_profile(
        path,
        {
            "DEFAULT_PROVIDER": "groq",
            "GROQ_API_KEY": "sk-secret",
            "VAULT_PATH": "/vault",
        },
    )
    loaded = setup_mod.load_profile(path)
    assert loaded["DEFAULT_PROVIDER"] == "groq"
    assert loaded["VAULT_PATH"] == "/vault"
    assert "GROQ_API_KEY" not in loaded  # secrets never remembered


def test_load_profile_missing_or_corrupt(tmp_path: pathlib.Path) -> None:
    assert setup_mod.load_profile(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("not json{{{", encoding="utf-8")
    assert setup_mod.load_profile(bad) == {}


def test_setup_command_rejects_bad_section() -> None:
    import typer

    try:
        setup_mod.setup_command(section="nope")
    except typer.Exit as exc:
        assert exc.exit_code == 2
    else:
        raise AssertionError("expected typer.Exit(2)")
