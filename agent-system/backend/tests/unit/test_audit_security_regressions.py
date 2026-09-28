"""Regressions for the SSRF / cross-user-isolation audit findings.

Each test pins a specific bypass that was live in the code before this file
existed. They are deliberately exploit-shaped: a private target must be
refused, not merely "not encouraged".
"""

from __future__ import annotations

import pathlib

import pytest

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.builtin.research import _assert_fetchable

# Every one of these resolved to (or was) a loopback/private/link-local address
# reachable from the agent process.
INTERNAL_TARGETS = [
    "http://127.0.0.1:8000/admin",
    "http://127.0.0.1/admin",
    "http://169.254.169.254/latest/meta-data/",  # AWS IMDS
    "http://100.100.100.200/latest/meta-data/",  # Alibaba IMDS (CGNAT)
    "http://localhost/",
    "http://metadata.google.internal/",
    "http://[::1]/",
    "https://127.0.0.1.nip.io/",  # hostname that resolves to loopback
    "http://192.168.1.1/",
    "http://10.0.0.5/",
]


@pytest.mark.parametrize("url", INTERNAL_TARGETS)
def test_assert_fetchable_blocks_internal_targets(url: str) -> None:
    with pytest.raises(ToolError):
        _assert_fetchable(url)


def test_assert_fetchable_blocks_non_standard_port() -> None:
    with pytest.raises(ToolError, match="port"):
        _assert_fetchable("https://example.com:8443/")


def test_assert_fetchable_allows_public_https() -> None:
    # Must not over-block: a public target still resolves and passes.
    _assert_fetchable("https://example.com/")


def test_research_agent_fetch_honours_passed_guard() -> None:
    """ResearchAgent must run a caller-supplied guard on URL and every redirect.

    Before the fix, web_fetch / research_citations reached any internal host
    because ResearchAgent.fetch did its own unguarded httpx.get with
    follow_redirects=True. The guard is now injected by the tool callers.
    """
    from agent_system.agents.browser_research import ResearchAgent

    with pytest.raises((ToolError, ValueError)):
        ResearchAgent().fetch("http://127.0.0.1:8000/admin", guard=_assert_fetchable)


def test_web_fetch_tool_blocks_internal_target() -> None:
    """End-to-end through the registered tool, not just the helper."""
    from agent_system.services.tools.builtin.research import _web_fetch

    with pytest.raises(ToolError):
        _web_fetch({"url": "http://127.0.0.1:8000/admin"}, None)


class TestTaskOwnerInheritance:
    """Telegram creates an owned session; its tasks must carry that owner.

    owner_user_id=None means "no filter" in memory recall and in the
    permission gate, so a NULL owner on an owned session is a cross-user read.
    """

    def test_task_inherits_owner_from_session(self, tmp_path) -> None:
        from agent_system.infra.db import make_engine, make_session_factory, session_scope
        from agent_system.infra.event_bus import EventBus
        from agent_system.infra.models import Base, Session, Task
        from agent_system.services.orchestrator import Supervisor

        engine = make_engine(f"sqlite:///{tmp_path / 'owner.db'}")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()
        with session_scope(factory) as db:
            db.add(Session(id="s1", goal="g", owner_user_id="usr_alice"))
        task_id = Supervisor(bus).add_task(factory, "s1", task_type="llm", title="t")
        with session_scope(factory) as db:
            assert db.get(Task, task_id).owner_user_id == "usr_alice"

    def test_task_owner_explicit_wins(self, tmp_path) -> None:
        from agent_system.infra.db import make_engine, make_session_factory, session_scope
        from agent_system.infra.event_bus import EventBus
        from agent_system.infra.models import Base, Session, Task
        from agent_system.services.orchestrator import Supervisor

        engine = make_engine(f"sqlite:///{tmp_path / 'owner2.db'}")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        bus = EventBus()
        with session_scope(factory) as db:
            db.add(Session(id="s1", goal="g", owner_user_id="usr_alice"))
        task_id = Supervisor(bus).add_task(
            factory, "s1", task_type="llm", title="t", owner_user_id="usr_bob"
        )
        with session_scope(factory) as db:
            assert db.get(Task, task_id).owner_user_id == "usr_bob"


class TestFileSearchCannotExfiltrate:
    """file_search must enforce the same jail + denylist a direct read does.

    Before the fix, `_iter_files` only validated the search *root*: it followed
    symlinks out of the allowed roots and swept up `.env` / `id_rsa` that
    `file_read` correctly refuses, with no approval (read tier).
    """

    @staticmethod
    def _tree(root: pathlib.Path) -> pathlib.Path:
        ws = root / "ws"
        (ws / "sub").mkdir(parents=True)
        (root / "outside").mkdir()
        (root / "outside" / "secret.txt").write_text("TOPSECRET-OUTSIDE-JAIL\n")
        (ws / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nz\n")
        (ws / ".env").write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345\n")
        (ws / "app.py").write_text("x = 1  # TODO fix this\n")
        (ws / "link.txt").symlink_to(root / "outside" / "secret.txt")
        return ws

    def _ctx(self, root: pathlib.Path) -> object:
        from agent_system.services.tools.registry import ToolContext

        class _S:
            tools_fs_roots = str(root)
            max_file_size_mb = 10

        return ToolContext(session_id="s", task_id="t", settings=_S())

    def test_secret_and_symlink_content_is_not_returned(self, tmp_path) -> None:
        from agent_system.services.tools.builtin.filesystem import _file_search

        ws = self._tree(tmp_path)
        out = _file_search(
            {"path": str(ws), "query": "sk-|TOPSECRET|PRIVATE KEY"},
            self._ctx(tmp_path),
        )
        assert out["count"] == 0, out

    def test_legitimate_matches_still_returned(self, tmp_path) -> None:
        from agent_system.services.tools.builtin.filesystem import _file_search

        ws = self._tree(tmp_path)
        out = _file_search({"path": str(ws), "query": "TODO"}, self._ctx(tmp_path))
        assert out["count"] == 1
        assert out["matches"][0]["text"] == "x = 1  # TODO fix this"


class TestSandboxAllowlistContainment:
    """The allowlist matched a string prefix and blacklisted metachars.

    That let `python3 -c "__import__('os').popen('id').read()"` through with
    `python3` allowlisted (quotes/parens/spaces were never blacklisted), and
    `git` authorise `git-evil`. On Heroku this allowlist is the only boundary,
    because there is no Docker.
    """

    @pytest.mark.parametrize(
        "command,allow",
        [
            ('python3 -c "import os"', "python3"),
            ("sh -c id", "sh"),
            ("ls | evilcmd", "ls"),
            ("ls; evilcmd", "ls"),
            ("ls > out", "ls"),
            ("ls $(id)", "ls"),
            ("git -c alias.b=!f() b", "git"),
            ("git-evil --help", "git"),
        ],
    )
    def test_bypasses_are_rejected(self, command: str, allow: str) -> None:
        from agent_system.services.sandbox import SandboxError, check_allowlist

        with pytest.raises(SandboxError):
            check_allowlist(command, allow)

    @pytest.mark.parametrize(
        "command,allow",
        [("ls -la", "ls"), ("cat f.txt", "/bin/cat,ls"), ("rg TODO src", "rg")],
    )
    def test_legitimate_commands_pass(self, command: str, allow: str) -> None:
        from agent_system.services.sandbox import check_allowlist

        check_allowlist(command, allow)  # must not raise

    def test_empty_allowlist_is_unrestricted(self) -> None:
        from agent_system.services.sandbox import check_allowlist

        check_allowlist("anything; at all", "")


class TestUpdateLedgerRedaction:
    """Telegram persisted the raw update, so `/setup` wrote every pasted SSH
    key and provider token into SQLite and every backup in the clear."""

    def test_api_key_is_redacted_in_persisted_copy(self) -> None:
        from agent_system.services.telegram import _redact_update

        upd = {
            "update_id": 1,
            "message": {"text": "key is sk-abcdefghijklmnopqrstuvwxyz012345 ok", "from": {"id": 5}},
        }
        stored = _redact_update(upd)
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in stored["message"]["text"]
        # The in-memory copy used to process the message must be untouched.
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" in upd["message"]["text"]
        # Non-secret structure survives.
        assert stored["message"]["from"] == {"id": 5}
        assert stored["update_id"] == 1

    def test_pasted_pem_is_redacted(self) -> None:
        from agent_system.services.telegram import _redact_update

        upd = {
            "update_id": 2,
            "message": {"text": "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzTOPSECRET"},
        }
        assert "TOPSECRET" not in _redact_update(upd)["message"]["text"]


class TestVaultKeyNotDerivedFromPublicDefault:
    """The vault fell back to api_session_secret, which ships as
    `dev-only-secret-change-me` — so a deployment that never set
    BOB_MASTER_ENCRYPTION_KEY encrypted every credential under a public key."""

    def test_default_secret_is_refused(self, monkeypatch) -> None:
        from agent_system.config import DEFAULT_SECRET, clear_settings_cache
        from agent_system.services.credentials import get_master_key_bytes

        monkeypatch.setenv("BOB_MASTER_ENCRYPTION_KEY", "")
        monkeypatch.setenv("API_SESSION_SECRET", DEFAULT_SECRET)
        monkeypatch.setenv("AGENT_BOOTSTRAP_SECRET", DEFAULT_SECRET)
        clear_settings_cache()
        with pytest.raises(RuntimeError, match="BOB_MASTER_ENCRYPTION_KEY"):
            get_master_key_bytes()
        clear_settings_cache()

    def test_dedicated_key_is_still_honoured(self, monkeypatch) -> None:
        from agent_system.config import clear_settings_cache
        from agent_system.services.credentials import get_master_key_bytes

        monkeypatch.setenv("BOB_MASTER_ENCRYPTION_KEY", "a" * 64)
        clear_settings_cache()
        assert len(get_master_key_bytes()) == 32
        clear_settings_cache()


class TestRoleGates:
    """require_role is the shared primitive behind the owner-only settings
    write and the OWNER/ADMIN approval decision."""

    def test_member_is_rejected(self) -> None:
        from fastapi import HTTPException

        from agent_system.api.deps import require_role
        from agent_system.services.identity import Role

        class P:
            role = Role.MEMBER

        with pytest.raises(HTTPException) as e:
            require_role(P(), "owner", "admin")
        assert e.value.status_code == 403

    @pytest.mark.parametrize("role", ["owner", "admin"])
    def test_owner_and_admin_pass(self, role: str) -> None:
        from agent_system.api.deps import require_role
        from agent_system.services.identity import Role

        require_role(type("P", (), {"role": Role(role)})(), "owner", "admin")

    def test_missing_role_fails_closed(self) -> None:
        from fastapi import HTTPException

        from agent_system.api.deps import require_role

        with pytest.raises(HTTPException):
            require_role(object(), "owner")
