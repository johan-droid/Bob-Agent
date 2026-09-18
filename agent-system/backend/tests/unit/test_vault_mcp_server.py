"""Bob Agent Vault MCP server — protocol, scrubbing and record invariants.

The server is what keeps the Obsidian vault updated on the user's behalf, so the
invariants pinned here are the ones a user would notice: secrets never land in
the vault, the dedicated Bob Agent record keeps one entry per event and stays
bounded, bad arguments fail *visibly* (``isError``), and a malformed line or a
write failure can never kill the transport loop.

Every test drives the production module
(``agent_system.mcp_servers.vault``); nothing is re-implemented here.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from agent_system.mcp_servers import vault


def _call(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    reply = vault.handle_message(
        root,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert reply is not None
    return reply["result"] if "result" in reply else reply


class TestProtocol:
    def test_initialize_echoes_protocol_and_identifies_server(self, tmp_path: Path) -> None:
        reply = vault.handle_message(
            tmp_path, {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}
        )
        assert reply is not None
        assert reply["id"] == 7
        assert reply["result"]["serverInfo"]["name"] == vault.SERVER_NAME
        assert reply["result"]["protocolVersion"] == vault.PROTOCOL_VERSION
        assert reply["result"]["capabilities"]["tools"] == {"listChanged": False}

    def test_notification_gets_no_reply(self, tmp_path: Path) -> None:
        assert (
            vault.handle_message(
                tmp_path, {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            is None
        )

    def test_every_advertised_tool_is_a_closed_schema(self, tmp_path: Path) -> None:
        reply = vault.handle_message(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert reply is not None
        tools = reply["result"]["tools"]
        assert {tool["name"] for tool in tools} == {
            "vault_write_note",
            "vault_append_daily",
            "vault_record",
            "vault_read_record",
            "vault_recall",
            "vault_status",
        }
        for tool in tools:
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            assert (
                schema.get("additionalProperties", False) is False or tool["name"] == "vault_status"
            )
            assert tool["description"]

    def test_unknown_method_is_a_jsonrpc_error(self, tmp_path: Path) -> None:
        reply = vault.handle_message(
            tmp_path, {"jsonrpc": "2.0", "id": 2, "method": "nope/unknown"}
        )
        assert reply is not None
        assert reply["error"]["code"] == vault._METHOD_NOT_FOUND

    def test_unknown_tool_fails_visibly(self, tmp_path: Path) -> None:
        result = _call(tmp_path, "vault_nope", {})
        assert result["isError"] is True
        assert "unknown tool" in result["content"][0]["text"]

    def test_malformed_line_does_not_kill_the_loop(self, tmp_path: Path) -> None:
        stdin = io.StringIO(
            'not json at all\n{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n'
        )
        stdout = io.StringIO()
        assert vault.serve(tmp_path, stdin=stdin, stdout=stdout) == 0
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        assert replies[0]["error"]["code"] == vault._PARSE_ERROR
        assert replies[1]["result"] == {}


class TestWriteNote:
    def test_note_lands_in_layer_dir_with_frontmatter_and_links(self, tmp_path: Path) -> None:
        result = _call(
            tmp_path,
            "vault_write_note",
            {
                "title": "Gate decision",
                "body": "Bob keeps the vault updated.",
                "layer": "system",
                "tags": ["gate"],
                "links": ["Bob Agent"],
            },
        )
        assert result["isError"] is False
        path = Path(result["structuredContent"]["path"])
        assert path.parent == tmp_path / "memory" / "system"
        text = path.read_text(encoding="utf-8")
        assert "layer: SYSTEM" in text
        assert "[[Bob Agent]]" in text

    def test_secret_shaped_values_never_reach_the_vault(self, tmp_path: Path) -> None:
        _call(
            tmp_path,
            "vault_write_note",
            {"title": "leak", "body": "key sk-abcdefghijklmnopqrstuvwx here"},
        )
        written = (tmp_path / "memory" / "task").glob("*.md")
        text = next(written).read_text(encoding="utf-8")
        assert "sk-abcdefghijklmnopqrstuvwx" not in text
        assert "[REDACTED]" in text

    def test_unknown_layer_and_missing_fields_are_rejected(self, tmp_path: Path) -> None:
        bad_layer = _call(
            tmp_path, "vault_write_note", {"title": "t", "body": "b", "layer": "NOPE"}
        )
        assert bad_layer["isError"] is True
        assert "unknown layer" in bad_layer["content"][0]["text"]
        missing = _call(tmp_path, "vault_write_note", {"title": "t"})
        assert missing["isError"] is True
        assert "'body' is required" in missing["content"][0]["text"]

    def test_oversized_body_is_reported_not_raised(self, tmp_path: Path) -> None:
        from agent_system.services.memory import MAX_NOTE_BYTES

        result = _call(
            tmp_path, "vault_write_note", {"title": "big", "body": "x" * (MAX_NOTE_BYTES + 1)}
        )
        assert result["isError"] is True


class TestDailyLog:
    def test_two_appends_share_one_dated_note(self, tmp_path: Path) -> None:
        _call(tmp_path, "vault_append_daily", {"text": "first", "date": "2026-09-18"})
        _call(tmp_path, "vault_append_daily", {"text": "second", "date": "2026-09-18"})
        text = (tmp_path / "daily" / "2026-09-18.md").read_text(encoding="utf-8")
        assert text.count("type: daily-log") == 1
        assert "- " in text and "first" in text and "second" in text

    @pytest.mark.parametrize("bad", ["../../etc/passwd", "18-09-2026", "2026/09/18"])
    def test_date_is_validated_against_traversal(self, tmp_path: Path, bad: str) -> None:
        result = _call(tmp_path, "vault_append_daily", {"text": "x", "date": bad})
        assert result["isError"] is True
        assert "'date' must be YYYY-MM-DD" in result["content"][0]["text"]
        assert not (tmp_path / "daily").exists()


class TestBobAgentRecord:
    def test_record_is_a_separate_note_and_counts_each_event(self, tmp_path: Path) -> None:
        first = _call(tmp_path, "vault_record", {"event": "release-gate", "details": "DEF-002"})
        second = _call(tmp_path, "vault_record", {"event": "release-gate", "details": "verified"})
        assert first["isError"] is False and second["isError"] is False
        assert first["structuredContent"]["events"] == 1
        assert second["structuredContent"]["events"] == 2
        path = Path(second["structuredContent"]["path"])
        assert path == tmp_path / "records" / "bob-agent.md"
        text = path.read_text(encoding="utf-8")
        assert "type: agent-record" in text and "agent: Bob Agent" in text
        activity = text.split("## Activity", 1)[1]
        assert len([line for line in activity.splitlines() if line.startswith("- ")]) == 2

    def test_created_stamp_is_preserved_across_updates(self, tmp_path: Path) -> None:
        _call(tmp_path, "vault_record", {"event": "one"})
        created = vault.read_record(tmp_path)["created"]
        _call(tmp_path, "vault_record", {"event": "two"})
        record = vault.read_record(tmp_path)
        assert record["created"] == created
        assert record["updated"] != created

    def test_activity_log_is_bounded_but_counter_is_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vault, "MAX_RECORD_EVENTS", 3)
        for i in range(6):
            _call(tmp_path, "vault_record", {"event": f"event-{i}"})
        record = vault.read_record(tmp_path, limit=100)
        assert record["events"] == 6
        assert len(record["entries"]) == 3
        assert "event-5" in record["entries"][-1]
        assert "event-0" not in "".join(record["entries"])

    def test_record_scrubs_secrets_from_event_and_details(self, tmp_path: Path) -> None:
        _call(
            tmp_path,
            "vault_record",
            {
                "event": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "details": "sk-abcdefghijklmnopqrstuvwx",
            },
        )
        text = vault.record_path(tmp_path).read_text(encoding="utf-8")
        assert "ghp_AAAA" not in text and "sk-abcdefghijklmnopqrstuvwx" not in text
        assert "[REDACTED]" in text

    def test_missing_record_reads_as_absent_not_an_error(self, tmp_path: Path) -> None:
        result = _call(tmp_path, "vault_read_record", {})
        assert result["isError"] is False
        assert result["structuredContent"]["exists"] is False
        assert result["structuredContent"]["events"] == 0

    def test_record_requires_an_event_name(self, tmp_path: Path) -> None:
        result = _call(tmp_path, "vault_record", {"details": "no event"})
        assert result["isError"] is True
        assert "'event' is required" in result["content"][0]["text"]


class TestRecallAndStatus:
    def test_recall_uses_the_production_vault_retrieval(self, tmp_path: Path) -> None:
        _call(
            tmp_path,
            "vault_write_note",
            {"title": "Deployment", "body": "Heroku dyno restarts recovery", "layer": "SYSTEM"},
        )
        result = _call(tmp_path, "vault_recall", {"query": "heroku recovery", "limit": 3})
        assert result["isError"] is False
        assert result["structuredContent"]["count"] >= 1
        assert any("Heroku" in note["snippet"] for note in result["structuredContent"]["notes"])

    def test_recall_requires_a_query(self, tmp_path: Path) -> None:
        assert _call(tmp_path, "vault_recall", {})["isError"] is True

    def test_status_reports_layers_and_record_state(self, tmp_path: Path) -> None:
        _call(tmp_path, "vault_write_note", {"title": "n", "body": "b", "layer": "USER"})
        _call(tmp_path, "vault_record", {"event": "status-check"})
        payload = _call(tmp_path, "vault_status", {})["structuredContent"]
        assert payload["layers"]["USER"] == 1
        assert payload["notes"] == 1
        assert payload["record_exists"] is True
        assert payload["record_events"] == 1
        assert payload["vault"] == str(tmp_path)


class TestVaultResolution:
    def test_override_wins_then_env_then_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert vault.resolve_vault_root(str(tmp_path)) == tmp_path
        monkeypatch.setenv("VAULT_PATH", str(tmp_path / "from-env"))
        assert vault.resolve_vault_root(None) == tmp_path / "from-env"
        monkeypatch.delenv("VAULT_PATH")
        assert vault.resolve_vault_root(None).is_absolute()

    def test_main_wires_argv_into_the_stdio_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_serve(root: Path, stdin: Any = None, stdout: Any = None) -> int:
            captured["root"] = root
            return 0

        monkeypatch.setattr(vault, "serve", _fake_serve)
        assert vault.main(["--vault", str(tmp_path)]) == 0
        assert captured["root"] == tmp_path
