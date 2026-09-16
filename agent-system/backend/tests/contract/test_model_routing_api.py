"""Contract tests — model-routing provider list + test endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_system.api.main import app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        token = client.app.state.authenticator.bootstrap_token  # type: ignore[attr-defined]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


class TestListProviders:
    def test_lists_all_catalog_providers(self, client: TestClient) -> None:
        r = client.get("/api/v1/model-routing/providers")
        assert r.status_code == 200
        body = r.json()
        assert len(body["providers"]) >= 12
        assert "default_provider" in body
        assert "default_model" in body
        assert "pricing" in body
        assert "router_active" in body
        keys = {p["key"] for p in body["providers"]}
        for expected in ("groq", "ollama", "openrouter", "gemini", "anthropic", "tokenrouter"):
            assert expected in keys

    def test_requires_auth(self) -> None:
        with TestClient(app) as anon:
            r = anon.get("/api/v1/model-routing/providers")
            assert r.status_code in (401, 403)


class TestProviderCheck:
    def test_unknown_provider_returns_not_ok(self, client: TestClient) -> None:
        r = client.post("/api/v1/model-routing/test", json={"provider": "nonexistent"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
