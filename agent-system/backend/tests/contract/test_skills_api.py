"""Contract tests — skills CRUD + import endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_system.api.main import app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    with TestClient(app) as client:
        token = client.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def _create(client: TestClient, name: str = "demo-skill", **extra) -> dict:
    payload = {
        "name": name,
        "description": "A demo skill.",
        "instructions": "Always be helpful.",
        **extra,
    }
    r = client.post("/api/v1/skills", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


class TestList:
    def test_empty_dir_lists_empty(self, client: TestClient) -> None:
        r = client.get("/api/v1/skills")
        assert r.status_code == 200
        assert r.json()["skills"] == []

    def test_seed_skills_present_with_defaults(self) -> None:
        with TestClient(app) as client:
            token = client.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
            client.headers["Authorization"] = f"Bearer {token}"
            r = client.get("/api/v1/skills")
            assert r.status_code == 200
            names = {s["name"] for s in r.json()["skills"]}
            assert {"web-research", "document-craft", "qa-assist"} <= names


class TestCreateGet:
    def test_create_and_get_detail(self, client: TestClient) -> None:
        created = _create(client, agents=["research"], config={"depth": 2})
        assert created["name"] == "demo-skill"
        assert created["config"] == {"depth": 2}
        r = client.get("/api/v1/skills/demo-skill")
        assert r.status_code == 200
        assert r.json()["instructions"] == "Always be helpful."

    def test_agent_created_skill(self, client: TestClient) -> None:
        created = _create(client, name="agent-made", author="agent:research")
        assert created["author"] == "agent:research"

    def test_duplicate_conflicts(self, client: TestClient) -> None:
        _create(client)
        r = client.post(
            "/api/v1/skills",
            json={"name": "demo-skill", "description": "x", "instructions": "y"},
        )
        assert r.status_code == 409

    def test_unknown_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/skills/ghost").status_code == 404

    def test_bad_slug_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/skills",
            json={"name": "Bad Name!", "description": "x", "instructions": "y"},
        )
        assert r.status_code == 422


class TestUpdateDelete:
    def test_disable_and_config(self, client: TestClient) -> None:
        _create(client)
        r = client.patch("/api/v1/skills/demo-skill", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        r = client.patch("/api/v1/skills/demo-skill", json={"config": {"depth": 9}})
        assert r.json()["config"] == {"depth": 9}

    def test_delete(self, client: TestClient) -> None:
        _create(client)
        r = client.delete("/api/v1/skills/demo-skill")
        assert r.status_code == 200
        assert client.get("/api/v1/skills/demo-skill").status_code == 404

    def test_agent_filter(self, client: TestClient) -> None:
        _create(client, name="for-research", agents=["research"])
        _create(client, name="for-all")
        r = client.get("/api/v1/skills", params={"agent_type": "research"})
        names = {s["name"] for s in r.json()["skills"]}
        assert names == {"for-research", "for-all"}


class TestImport:
    def test_import_from_local_dir(self, client: TestClient, tmp_path: Path) -> None:
        src = tmp_path / "pack" / "cool-skill"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text(
            "---\nname: cool-skill\ndescription: Cool.\n---\n\nBe cool.\n",
            encoding="utf-8",
        )
        r = client.post("/api/v1/skills/import", json={"source": str(tmp_path / "pack")})
        assert r.status_code == 201, r.text
        assert r.json()["skills"][0]["name"] == "cool-skill"

    def test_import_missing_source_404(self, client: TestClient) -> None:
        r = client.post("/api/v1/skills/import", json={"source": "/nope/nothing"})
        assert r.status_code == 404
