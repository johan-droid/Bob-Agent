"""Unit tests: OpenConnector integration (HTTP actions + MCP-over-HTTP).

Covers the real oomol-lab/open-connector runtime API (``/v1`` envelope,
Bearer runtime tokens, ``x-oo-connector-alias``), the streamable-HTTP MCP
transport (JSON + SSE responses, ``Mcp-Session-Id``), the implicit
``openconnector`` MCP server, and the new tool registrations.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_system.services import mcp, openconnector
from agent_system.services.tools import build_registry


def _oc_settings(**over: Any) -> SimpleNamespace:
    base = {
        "openconnector_base_url": "http://oc.test:3000",
        "openconnector_runtime_token": "rt-token",
        "openconnector_admin_token": "admin-token",
        "openconnector_api_key": None,
        "openconnector_alias": "",
        "mcp_servers": "[]",
        "tools_shell_mode": "off",
        "tools_require_approval": False,
        "tools_max_iters": 4,
        "tools_fs_roots": "",
        "vault_path": "/tmp/vault",
        "skills_dir": "/tmp/skills",
        "soul_path": "",
        "memory_recall_top_k": 0,
        "default_provider": "echo",
        "default_model": None,
        "workspaces_dir": "/tmp/ws",
    }
    base.update(over)
    return SimpleNamespace(**base)


class _Router(httpx.BaseTransport):
    """Minimal canned-response router for httpx.Client."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    def _factory(_timeout: float) -> httpx.Client:
        def spy(request: httpx.Request) -> httpx.Response:
            return handler(request)

        return httpx.Client(transport=_Router(spy))

    monkeypatch.setattr(openconnector, "_client", _factory)


# ---------------------------------------------------------------------------
# OpenConnector HTTP runtime API
# ---------------------------------------------------------------------------


def test_execute_action_posts_v1_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"user": "octocat"},
                "meta": {"executionId": "run_123", "actionId": "github.get_current_user"},
            },
        )

    _patch_client(monkeypatch, handler)
    result = openconnector.execute_action(_oc_settings(), "github.get_current_user", {"who": "me"})
    assert result["data"] == {"user": "octocat"}
    assert result["meta"]["executionId"] == "run_123"
    assert seen["url"].endswith("/v1/actions/github.get_current_user")
    assert seen["auth"] == "Bearer rt-token"
    assert seen["body"] == {"input": {"who": "me"}}


def test_execute_action_sends_connection_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["alias"] = request.headers.get("x-oo-connector-alias")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"ok": True}})

    _patch_client(monkeypatch, handler)
    openconnector.execute_action(
        _oc_settings(openconnector_alias="work"), "github.get_current_user", {}, "work"
    )
    assert seen["alias"] == "work"
    assert seen["body"]["connectionName"] == "work"


def test_error_envelope_maps_to_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"code": "action_not_allowed", "message": "blocked by policy"}},
        )

    _patch_client(monkeypatch, handler)
    with pytest.raises(openconnector.ConnectorError) as excinfo:
        openconnector.execute_action(_oc_settings(), "github.create_issue", {})
    assert excinfo.value.code == "action_not_allowed"
    assert excinfo.value.status == 403
    assert "blocked by policy" in str(excinfo.value)


def test_list_actions_filters_by_service(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "data": {
                    "actions": [
                        {"id": "github.get_current_user", "name": "Whoami"},
                        {"id": "github.create_issue", "name": "New issue"},
                    ]
                }
            },
        )

    _patch_client(monkeypatch, handler)
    actions = openconnector.list_actions(_oc_settings(), service="github")
    assert len(actions) == 2
    assert "service=github" in seen["url"]


def test_unconfigured_hides_implicit_server() -> None:
    assert openconnector.implicit_mcp_server(_oc_settings(openconnector_base_url="")) is None
    implicit = openconnector.implicit_mcp_server(_oc_settings())
    assert implicit is not None
    assert implicit["name"] == "openconnector"
    assert implicit["url"] == "http://oc.test:3000/mcp"
    assert implicit["headers"]["Authorization"] == "Bearer rt-token"


# ---------------------------------------------------------------------------
# MCP over streamable HTTP (OpenConnector's POST /mcp)
# ---------------------------------------------------------------------------


class _McpHandler(BaseHTTPRequestHandler):
    """Tiny MCP server: initialize -> tools/list -> tools/call, SSE responses."""

    session_id = "sess-42"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _reply_sse(self, body: dict) -> None:
        payload = json.dumps(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", self.session_id)
        self.end_headers()
        self.wfile.write(f"data: {payload}\n\n".encode())

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        request = json.loads(self.rfile.read(length))
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            self._reply_sse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "oc", "version": "1"},
                    },
                }
            )
        elif method == "tools/list":
            self._reply_sse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [{"name": "github.get_current_user", "description": "Whoami"}]
                    },
                }
            )
        elif method == "tools/call":
            self._reply_sse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "octocat"}],
                        "structuredContent": {"user": "octocat"},
                    },
                }
            )
        elif method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
        else:
            self._reply_sse(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "nope"}}
            )


@pytest.fixture()
def mcp_http_server() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _McpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    server.shutdown()


def test_mcp_http_client_initialize_list_call(mcp_http_server: Any) -> None:
    config = mcp.McpServerConfig(name="openconnector", url=mcp_http_server)
    client = mcp.McpHttpClient(config)
    client.open()
    try:
        assert client._session_id == "sess-42"  # session captured from response
        tools = client.list_tools()
        assert tools[0]["name"] == "github.get_current_user"
        result = client.call_tool("github.get_current_user", {})
        assert result["structuredContent"] == {"user": "octocat"}
    finally:
        client.close()


def test_all_servers_appends_implicit_openconnector() -> None:
    servers = mcp.all_servers(_oc_settings())
    names = [s.name for s in servers]
    assert "openconnector" in names
    oc = next(s for s in servers if s.name == "openconnector")
    assert oc.url == "http://oc.test:3000/mcp"
    assert oc.headers["Authorization"] == "Bearer rt-token"


def test_explicit_openconnector_entry_wins() -> None:
    explicit = json.dumps([{"name": "openconnector", "url": "http://elsewhere:9999/mcp"}])
    servers = mcp.all_servers(_oc_settings(mcp_servers=explicit))
    assert len(servers) == 1
    assert servers[0].url == "http://elsewhere:9999/mcp"


def test_parse_servers_accepts_url_entries() -> None:
    raw = json.dumps([{"name": "remote", "url": "https://x/mcp"}])
    servers = mcp.parse_servers(_oc_settings(mcp_servers=raw))
    assert servers[0].url == "https://x/mcp"
    with pytest.raises(mcp.McpError):
        mcp.parse_servers(_oc_settings(mcp_servers=json.dumps([{"name": "bad"}])))


# ---------------------------------------------------------------------------
# Tool registry wiring
# ---------------------------------------------------------------------------


def test_registry_registers_openconnector_and_mcp_tools() -> None:
    registry = build_registry(_oc_settings())
    names = registry.names()
    assert "openconnector_execute" in names
    assert "openconnector_list" in names
    assert "mcp_list" in names
    assert "mcp_call" in names
    execute_tool = registry.get("openconnector_execute")
    assert execute_tool is not None
    assert "action" in execute_tool.parameters["properties"]


def test_registry_hides_openconnector_when_unconfigured() -> None:
    registry = build_registry(_oc_settings(openconnector_base_url=""))
    names = registry.names()
    assert "openconnector_execute" not in names
    assert "openconnector_list" not in names


def test_registry_mcp_list_lists_server_names() -> None:
    registry = build_registry(_oc_settings())
    tool = registry.get("mcp_list")
    assert tool is not None
    result = tool.handler({}, SimpleNamespace(settings=_oc_settings(), factory=None))
    assert "openconnector" in result["servers"]
