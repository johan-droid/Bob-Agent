"""OpenConnector client — oomol-lab/open-connector (self-hosted connector gateway).

An open-source Pipedream/Composio alternative: connect SaaS accounts once
(GitHub, Gmail, Slack, Notion, …), then call 1,000+ providers / 10,000+
prebuilt Actions through one authenticated gateway. Bob integrates BOTH of
its agent-facing surfaces:

- HTTP runtime API (``/v1/*``) — direct Action execution (this module).
- MCP over streamable HTTP (``POST /mcp``) — exposed through
  ``services/mcp.py`` as an implicit ``openconnector`` MCP server, so every
  OpenConnector Action also appears as an MCP tool.

Configure (env or ``/settings set``):

    OPENCONNECTOR_BASE_URL=http://localhost:3000   # runtime (docker: -p 3000:3000)
    OPENCONNECTOR_RUNTIME_TOKEN=...                # runtime token (optional locally)
    OPENCONNECTOR_ADMIN_TOKEN=...                  # admin API (action guides) (optional)
    OPENCONNECTOR_ALIAS=work                       # optional x-oo-connector-alias

Unconfigured (no base URL) = the OpenConnector tools stay hidden (no stubs).
Run a local runtime: ``docker run -p 3000:3000 ghcr.io/oomol-lab/open-connector``.
"""

from __future__ import annotations

from typing import Any

import httpx


class ConnectorError(RuntimeError):
    """OpenConnector returned an error envelope or was unreachable."""

    def __init__(self, message: str, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def is_configured(settings: Any) -> bool:
    """True when a base URL is set (token optional — local runtimes need none)."""
    return bool(str(getattr(settings, "openconnector_base_url", "") or "").strip())


def _base(settings: Any) -> str:
    return str(getattr(settings, "openconnector_base_url", "")).rstrip("/")


def _headers(settings: Any, *, admin: bool = False) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = str(
        getattr(
            settings,
            "openconnector_admin_token" if admin else "openconnector_runtime_token",
            "",
        )
        or ""
    )
    if not token and not admin:
        # Back-compat: the generic api-key setting also works as a runtime token.
        token = str(getattr(settings, "openconnector_api_key", "") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    alias = str(getattr(settings, "openconnector_alias", "") or "")
    if alias:
        headers["x-oo-connector-alias"] = alias
    return headers


def _unwrap(resp: httpx.Response) -> dict[str, Any]:
    """Unwrap the OpenConnector ``/v1`` envelope (``success`` + ``data``)."""
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        err = body.get("error") if isinstance(body, dict) else None
        code = str(err.get("code", "")) if isinstance(err, dict) else ""
        message = str(err.get("message", "") or body) if isinstance(err, dict) else str(body)
        if isinstance(body, dict) and body.get("errorCode"):
            code = body["errorCode"]
            message = str(body.get("message", "") or message)
        raise ConnectorError(
            f"openconnector {resp.status_code}: {message or resp.reason_phrase}",
            code=code,
            status=resp.status_code,
        )
    data = resp.json()
    if isinstance(data, dict):
        if data.get("success") is False:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            fallback = data if not isinstance(err, dict) else data.get("message", data)
            message = (
                str(err.get("message", fallback))
                if isinstance(err, dict)
                else str(data.get("message", data))
            )
            code = str(
                data.get("errorCode", "") or (err.get("code", "") if isinstance(err, dict) else "")
            )
            raise ConnectorError(message, code=code, status=200)
        if "data" in data:  # success envelope -> return the payload dict
            return data
    return data if isinstance(data, dict) else {"data": data}


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def health_check(settings: Any) -> bool:
    """True when the runtime answers ``GET /health`` with ``{"ok": true}``."""
    try:
        with _client(10.0) as client:
            resp = client.get(f"{_base(settings)}/health")
        return bool(resp.json().get("ok"))
    except Exception:
        return False


def catalog_summary(settings: Any) -> dict[str, Any]:
    """Catalog counts via ``/v1/catalog`` if the runtime exposes it ({} otherwise).

    The self-hosted Node runtime has no ``/v1/catalog`` (that endpoint serves
    the hosted ``connector.oomol.com``); count via ``list_actions`` instead.
    """
    try:
        with _client(15.0) as client:
            resp = client.get(f"{_base(settings)}/v1/catalog", headers=_headers(settings))
        if resp.status_code == 200:
            data = _unwrap(resp)
            return dict(data.get("data", data))
    except Exception:
        pass
    try:
        actions = list_actions(settings)
        services = {str(a.get("id", "") or "").split(".")[0] for a in actions if a.get("id")}
        return {"providerCount": len(services), "actionCount": len(actions)}
    except Exception:
        return {}


def list_actions(settings: Any, service: str | None = None) -> list[dict[str, Any]]:
    """List action contracts: ``GET /v1/actions[?service=<service>]``."""
    params = {"service": service} if service else None
    with _client(30.0) as client:
        resp = client.get(
            f"{_base(settings)}/v1/actions", headers=_headers(settings), params=params
        )
    data = _unwrap(resp)
    payload = data.get("data", data)
    if isinstance(payload, dict):
        for key in ("actions", "items", "data"):
            if isinstance(payload.get(key), list):
                return list(payload[key])
        return [payload]
    return list(payload) if isinstance(payload, list) else []


def get_action_guide(settings: Any, action_id: str) -> str:
    """Agent-readable markdown guide for one Action (admin API; '' if unavailable)."""
    try:
        with _client(15.0) as client:
            resp = client.get(
                f"{_base(settings)}/api/actions/{action_id}/agent.md",
                headers=_headers(settings, admin=True),
            )
        if resp.status_code == 200 and resp.text.strip():
            return resp.text
    except Exception:
        pass
    return ""


def execute_action(
    settings: Any,
    action_id: str,
    input_data: dict[str, Any] | None = None,
    connection_name: str | None = None,
) -> dict[str, Any]:
    """Execute one Action: ``POST /v1/actions/:actionId`` with ``{"input": …}``.

    Credentials stay inside the OpenConnector runtime (never in Bob); the
    response carries ``data`` plus ``meta.executionId`` (audit trail) and is
    returned as ``{"data": …, "meta": …}``.
    """
    payload: dict[str, Any] = {"input": input_data or {}}
    if connection_name:
        payload["connectionName"] = connection_name
    with _client(120.0) as client:
        resp = client.post(
            f"{_base(settings)}/v1/actions/{action_id}",
            headers=_headers(settings),
            json=payload,
        )
    body = _unwrap(resp)
    return {
        "data": body.get("data"),
        "meta": body.get("meta", {}),
    }


def mcp_url(settings: Any) -> str:
    """Streamable-HTTP MCP endpoint (``POST /mcp``) for this runtime."""
    return f"{_base(settings)}/mcp"


def mcp_headers(settings: Any) -> dict[str, str]:
    """Auth headers for the MCP endpoint (runtime token + connection alias).

    The MCP transport sets its own ``Accept`` (application/json +
    text/event-stream); only identity headers are forwarded here.
    """
    return {
        k: v for k, v in _headers(settings).items() if k not in ("Content-Type", "Accept") and v
    }


def implicit_mcp_server(settings: Any) -> dict[str, Any] | None:
    """MCP_SERVERS-style dict for this runtime, or None when unconfigured."""
    if not is_configured(settings):
        return None
    server: dict[str, Any] = {"name": "openconnector", "url": mcp_url(settings)}
    headers = mcp_headers(settings)
    if headers:
        server["headers"] = headers
    alias = str(getattr(settings, "openconnector_alias", "") or "")
    if alias:
        server["alias"] = alias
    return server


__all__ = [
    "ConnectorError",
    "catalog_summary",
    "execute_action",
    "get_action_guide",
    "health_check",
    "implicit_mcp_server",
    "is_configured",
    "list_actions",
    "mcp_headers",
    "mcp_url",
]
