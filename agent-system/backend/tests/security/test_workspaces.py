"""Security tests — workspaces & templates (v3.1 Phase 5/14 acceptance)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.services.workspaces import (
    TemplateManager,
    WorkspaceError,
    WorkspaceManager,
)


@pytest.fixture()
def ws(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "workspaces")


@pytest.fixture()
def tm(tmp_path: Path) -> TemplateManager:
    return TemplateManager(tmp_path / "templates")


def test_create_write_read_tree(ws: WorkspaceManager) -> None:
    ws.create("ws_1")
    ws.write_file("ws_1", "src/main.py", b"print('hi')\n")
    ws.write_file("ws_1", "docs/a/b.txt", b"deep")
    assert ws.read_file("ws_1", "src/main.py") == b"print('hi')\n"
    assert ws.tree("ws_1") == ["docs/a/b.txt", "src/main.py"]


def test_path_traversal_blocked(ws: WorkspaceManager) -> None:
    ws.create("ws_1")
    with pytest.raises(WorkspaceError, match="traversal"):
        ws.write_file("ws_1", "../../outside.txt", b"evil")
    with pytest.raises(WorkspaceError, match="traversal"):
        ws.read_file("ws_1", "../../../etc/passwd")


def test_file_size_limit(ws: WorkspaceManager) -> None:
    ws.create("ws_big")
    with pytest.raises(WorkspaceError, match="exceeds"):
        ws.write_file("ws_big", "big.bin", b"x" * (11 * 1024 * 1024))


def test_fingerprint_changes_with_content(ws: WorkspaceManager) -> None:
    ws.create("ws_fp")
    before = ws.fingerprint("ws_fp")
    ws.write_file("ws_fp", "a.txt", b"v1")
    v1 = ws.fingerprint("ws_fp")
    ws.write_file("ws_fp", "a.txt", b"v2")
    v2 = ws.fingerprint("ws_fp")
    assert before != v1
    assert v1 != v2


def test_template_snapshot_excludes_secrets(
    ws: WorkspaceManager, tm: TemplateManager, tmp_path: Path
) -> None:
    ws.create("ws_src")
    ws.write_file("ws_src", "src/main.py", b"print('ok')")
    ws.write_file("ws_src", ".env", b"API_KEY=sk-123")
    ws.write_file("ws_src", "config/secrets.json", b"{}")
    ws.write_file("ws_src", ".ssh/id_rsa", b"PRIVATE")
    workspace = tmp_path / "workspaces" / "ws_src"

    tm.snapshot("tpl_1", workspace, name="demo")
    skipped = tm.last_skipped
    assert ".env" in skipped
    assert "config/secrets.json" in skipped
    assert ".ssh/id_rsa" in skipped


def test_clone_restores_files_but_never_secrets(
    ws: WorkspaceManager, tm: TemplateManager, tmp_path: Path
) -> None:
    ws.create("ws_src")
    ws.write_file("ws_src", "src/main.py", b"print('ok')")
    ws.write_file("ws_src", ".env", b"API_KEY=sk-123")
    workspace = tmp_path / "workspaces" / "ws_src"
    tm.snapshot("tpl_2", workspace, name="demo2")

    target = tmp_path / "clones" / "ws_new"
    restored = tm.clone("tpl_2", target)
    assert "src/main.py" in restored
    assert (target / "src/main.py").read_bytes() == b"print('ok')"
    assert not (target / ".env").exists()


def test_clone_rejects_traversal_members(
    ws: WorkspaceManager, tm: TemplateManager, tmp_path: Path
) -> None:
    """A malicious template archive must not escape the target dir."""
    import io
    import tarfile

    malicious = tmp_path / "templates" / "tpl_evil.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"evil"
        info = tarfile.TarInfo(name="../../evil.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    malicious.write_bytes(buf.getvalue())

    target = tmp_path / "clones" / "ws_victim"
    restored = tm.clone("tpl_evil", target)
    assert restored == []
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()


def test_clone_missing_template(ws: WorkspaceManager, tm: TemplateManager, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="not found"):
        tm.clone("tpl_missing", tmp_path / "target")
