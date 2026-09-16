"""Contract tests for Vault Notes & Workspace Templates APIs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_system.api.main import app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    vault_dir = tmp_path / "vault"
    templates_dir = tmp_path / "templates"
    workspaces_dir = tmp_path / "workspaces"
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("TEMPLATES_DIR", str(templates_dir))
    monkeypatch.setenv("WORKSPACES_DIR", str(workspaces_dir))

    with TestClient(app) as client:
        token = client.app.state.authenticator.bootstrap_token
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


class TestVaultNotesApi:
    def test_vault_crud_lifecycle(self, client: TestClient) -> None:
        # 1. Create a note
        payload = {
            "title": "System Security Best Practices",
            "layer": "SYSTEM",
            "source": "user",
            "body": "Always validate inputs and sanitize tokens.",
            "tags": ["security", "best-practices"],
            "links": ["AuthService"],
        }
        res = client.post("/api/v1/vault/notes", json=payload)
        assert res.status_code == 201, res.text
        created = res.json()
        assert created["title"] == "System Security Best Practices"
        assert created["layer"] == "SYSTEM"
        assert "security" in created["tags"]
        note_path = created["path"]

        # 2. List notes
        res_list = client.get("/api/v1/vault/notes")
        assert res_list.status_code == 200
        notes = res_list.json()
        assert len(notes) >= 1
        assert any(n["title"] == "System Security Best Practices" for n in notes)

        # 3. Read specific note
        res_get = client.get(f"/api/v1/vault/note?path={note_path}")
        assert res_get.status_code == 200
        detail = res_get.json()
        assert "Always validate inputs" in detail["body"]

        # 4. Filter by search
        res_search = client.get("/api/v1/vault/notes?search=security")
        assert res_search.status_code == 200
        assert len(res_search.json()) >= 1

        # 5. Delete note
        res_del = client.delete(f"/api/v1/vault/notes?path={note_path}")
        assert res_del.status_code == 204


class TestTemplatesApi:
    def test_templates_lifecycle(self, client: TestClient) -> None:
        # 1. Create a workspace
        ws_res = client.post("/api/v1/workspaces", json={"name": "test-ws-source"})
        assert ws_res.status_code == 201, ws_res.text
        ws = ws_res.json()
        ws_id = ws["id"]

        # 2. Add a file to workspace
        import base64

        b64_content = base64.b64encode(b"console.log('hello');").decode()
        client.put(
            f"/api/v1/workspaces/{ws_id}/file",
            json={"path": "index.js", "content_b64": b64_content},
        )

        # 3. Create template snapshot
        tpl_res = client.post(
            "/api/v1/templates",
            json={"workspace_id": ws_id, "name": "NodeJS Starter Template"},
        )
        assert tpl_res.status_code == 201, tpl_res.text
        tpl = tpl_res.json()
        assert tpl["name"] == "NodeJS Starter Template"
        tpl_id = tpl["template_id"]

        # 4. List templates
        tpl_list = client.get("/api/v1/templates")
        assert tpl_list.status_code == 200
        assert any(t["template_id"] == tpl_id for t in tpl_list.json())

        # 5. Restore template into a new workspace
        restore_res = client.post(
            f"/api/v1/templates/{tpl_id}/restore",
            json={"workspace_name": "restored-node-app"},
        )
        assert restore_res.status_code == 201, restore_res.text
        restored_ws = restore_res.json()
        assert restored_ws["name"] == "restored-node-app"

        # 6. Delete template
        del_res = client.delete(f"/api/v1/templates/{tpl_id}")
        assert del_res.status_code == 204
