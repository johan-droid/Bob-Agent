"""Minimal MCP client (Model Context Protocol) — stdlib + httpx, no SDK dep.

Two transports behind one interface:

- **stdio** (``McpStdioClient``): JSON-RPC over stdin/stdout to local MCP
  servers (``npx``, ``uvx``, or a local binary).
- **streamable HTTP** (``McpHttpClient``): JSON-RPC ``POST`` to a remote MCP
  endpoint (e.g. OpenConnector's ``POST /mcp``). Handles both ``application/json``
  and ``text/event-stream`` responses plus ``Mcp-Session-Id`` sessions.

Configure via ``MCP_SERVERS`` JSON (command = stdio, url = HTTP):

    MCP_SERVERS='[{"name": "fs", "command": "npx",
                   "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
                  {"name": "remote", "url": "https://host/mcp",
                   "headers": {"Authorization": "Bearer ..."}}]'

When OpenConnector is configured (``OPENCONNECTOR_BASE_URL``), an implicit
``openconnector`` HTTP MCP server is appended automatically — every OpenConnector
Action becomes an MCP tool. Unconfigured or failing servers are skipped with
recorded errors — never fatal.
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any


class McpError(RuntimeError):
    """MCP transport or protocol failure."""


# Hostile/broken-server containment (security suite: tests/security/test_hostile_mcp.py).
# A remote MCP server controls everything it sends back; these caps bound what a
# single response can do to this process before any of it is trusted or parsed.
_MAX_BODY_CHARS = 4_000_000  # one HTTP response body (JSON or SSE)
_MAX_LINE_CHARS = 4_000_000  # one stdio JSON-RPC line / one SSE data: chunk


@dataclass
class McpServerConfig:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: float = 30.0
    # HTTP transport (streamable MCP):
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    alias: str | None = None  # OpenConnector x-oo-connector-alias

    @staticmethod
    def from_dict(data: dict[str, Any]) -> McpServerConfig:
        url = str(data.get("url", "") or "").strip() or None
        command = str(data.get("command", "") or "")
        if not url and not command:
            raise McpError(f"MCP server entry needs 'command' (stdio) or 'url' (http): {data}")
        return McpServerConfig(
            name=str(data.get("name", "")),
            command=command,
            args=[str(a) for a in (data.get("args") or [])],
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            cwd=str(data["cwd"]) if data.get("cwd") else None,
            timeout=float(data.get("timeout") or 30.0),
            url=url,
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            alias=str(data["alias"]) if data.get("alias") else None,
        )


def parse_servers(settings: Any) -> list[McpServerConfig]:
    """Parse MCP_SERVERS JSON (empty/missing -> [])."""
    raw = str(getattr(settings, "mcp_servers", "") or "").strip()
    if not raw:
        return []
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError as exc:
        raise McpError(f"MCP_SERVERS is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise McpError("MCP_SERVERS must be a JSON list")
    servers = []
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise McpError(f"invalid MCP server entry: {entry}")
        servers.append(McpServerConfig.from_dict(entry))
    return servers


def all_servers(settings: Any) -> list[McpServerConfig]:
    """Configured servers plus the implicit OpenConnector HTTP MCP server.

    An explicit ``openconnector`` entry in MCP_SERVERS wins (it may point at
    a different origin or carry its own headers).
    """
    servers = parse_servers(settings)
    if any(s.name == "openconnector" for s in servers):
        return servers
    try:
        from agent_system.services.openconnector import implicit_mcp_server

        implicit = implicit_mcp_server(settings)
    except Exception:
        implicit = None
    if implicit:
        servers.append(McpServerConfig.from_dict(implicit))
    return servers


def is_configured(settings: Any) -> bool:
    """True when at least one server is declared (reachable or not)."""
    try:
        return bool(all_servers(settings))
    except McpError:
        return False


class McpStdioClient:
    """One short-lived stdio session: initialize -> request -> close."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._proc: subprocess.Popen[str] | None = None
        self._request_id = 0
        self._lock = threading.Lock()

    def _start(self) -> None:
        env = dict(os.environ)
        env.update(self.config.env)
        try:
            self._proc = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=self.config.cwd,
                env=env,
            )
        except FileNotFoundError as exc:
            raise McpError(f"mcp server '{self.config.name}': command not found") from exc
        except OSError as exc:
            raise McpError(f"mcp server '{self.config.name}': cannot start: {exc}") from exc

    def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(_json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(f"mcp server '{self.config.name}': pipe broken") from exc

    def _recv(self) -> dict[str, Any]:
        import concurrent.futures

        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout

        def _readline() -> str:
            return str(stdout.readline())

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_readline)
        try:
            line = future.result(timeout=self.config.timeout)
        except concurrent.futures.TimeoutError as exc:
            # Do NOT join the executor here: the reader thread is parked in
            # readline() and joining would block far beyond the timeout (until
            # the server's process dies). close() kills the process, which
            # unblocks the thread; leaking it briefly is bounded and safe.
            raise McpError(
                f"mcp server '{self.config.name}': response timeout ({self.config.timeout}s)"
            ) from exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if not line:
            raise McpError(f"mcp server '{self.config.name}': server closed stdout")
        if len(line) > _MAX_LINE_CHARS:
            raise McpError(
                f"mcp server '{self.config.name}': response line exceeds "
                f"{_MAX_LINE_CHARS} chars — refused (hostile/buggy server)"
            )
        try:
            data = _json.loads(line)
        except _json.JSONDecodeError as exc:
            raise McpError(f"mcp server '{self.config.name}': bad JSON: {line[:120]}") from exc
        if not isinstance(data, dict):
            raise McpError(f"mcp server '{self.config.name}': bad envelope")
        return data

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            self._request_id += 1
            self._send(
                {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or {}}
            )
            reply = self._recv()
        if reply.get("id") != self._request_id:
            raise McpError(f"mcp server '{self.config.name}': id mismatch")
        if "error" in reply:
            raise McpError(f"mcp server '{self.config.name}': {reply['error']}")
        return reply.get("result")

    def open(self) -> None:
        """Start the process and run the MCP initialize handshake."""
        self._start()
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "bob-agent", "version": "0.1.0"},
                },
            )
            with self._lock:
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.close()
            raise

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            return list(result["tools"])
        return []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict):
            return result
        return {"result": result}

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


class McpHttpClient:
    """Streamable-HTTP MCP session: initialize -> request -> close.

    Speaks JSON-RPC 2.0 over ``POST`` to a remote MCP endpoint (e.g.
    OpenConnector's ``POST /mcp``). Responses may be plain JSON or an SSE
    stream (``text/event-stream``) — both are parsed. The server-assigned
    ``Mcp-Session-Id`` header is replayed on every subsequent request.
    """

    def __init__(self, config: McpServerConfig) -> None:
        assert config.url, "McpHttpClient requires a url"
        self.config = config
        self._session_id: str | None = None
        self._request_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self.config.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _parse_body(self, text: str, content_type: str) -> list[dict[str, Any]]:
        """Parse a JSON or SSE response body into JSON-RPC message dicts.

        The content type is an **allowlist** (``application/json`` or
        ``text/event-stream``) — anything else is refused outright, not merely
        left to fail JSON parsing. A hostile server cannot smuggle a JSON-RPC
        reply through ``text/html`` or any other unexpected type. Malformed
        bodies raise the typed :class:`McpError` — a hostile or broken server
        never leaks a bare parser exception past this seam.
        """
        base_type = content_type.split(";")[0].strip().lower()
        if base_type not in ("application/json", "text/event-stream"):
            raise McpError(
                f"mcp http '{self.config.name}': unexpected content type "
                f"'{content_type or '(none)'}' — refused"
            )
        if len(text) > _MAX_BODY_CHARS:
            raise McpError(
                f"mcp http '{self.config.name}': response body exceeds "
                f"{_MAX_BODY_CHARS} chars — refused (hostile/buggy server)"
            )
        if base_type == "text/event-stream":
            messages: list[dict[str, Any]] = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if not chunk:
                        continue
                    try:
                        parsed = _json.loads(chunk)
                    except _json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        messages.append(parsed)
            return messages
        try:
            parsed = _json.loads(text)
        except _json.JSONDecodeError as exc:
            raise McpError(
                f"mcp http '{self.config.name}': malformed response body ({exc})"
            ) from exc
        return [parsed] if isinstance(parsed, dict) else []

    def _post(self, payload: dict[str, Any]) -> Any:
        import httpx

        assert self.config.url, "McpHttpClient requires a url"
        with httpx.Client(timeout=self.config.timeout) as client:
            resp = client.post(self.config.url, headers=self._headers(), json=payload)
        if len(resp.text) > _MAX_BODY_CHARS:
            raise McpError(
                f"mcp http '{self.config.name}': response body exceeds "
                f"{_MAX_BODY_CHARS} chars — refused (hostile/buggy server)"
            )
        return resp

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self._request_id += 1
        request_id = self._request_id
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        resp = self._post(body)
        if resp.status_code == 202:
            return None  # notification-only acknowledgement
        if resp.status_code >= 400:
            raise McpError(f"mcp http '{self.config.name}': {resp.status_code} {resp.text[:200]}")
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        messages = self._parse_body(resp.text, resp.headers.get("content-type", ""))
        # The client never pipelines, so a *reply* (has id + result/error, no
        # method) whose id is not ours is a spoofed or hijacked response —
        # rejected outright, mirroring the stdio client's id-mismatch guard.
        reply = None
        for message in messages:
            if message.get("id") == request_id:
                reply = message
                break
            if (
                "method" not in message
                and message.get("id") is not None
                and ("result" in message or "error" in message)
            ):
                raise McpError(f"mcp http '{self.config.name}': id mismatch")
        if reply is None:
            return None
        if "error" in reply:
            raise McpError(f"mcp http '{self.config.name}': {reply['error']}")
        return reply.get("result")

    def open(self) -> None:
        """Run the MCP initialize handshake (captures the session id)."""
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "bob-agent", "version": "0.1.0"},
            },
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list", {})
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            return list(result["tools"])
        return []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict):
            return result
        return {"result": result}

    def close(self) -> None:
        """HTTP sessions are per-request; nothing to release."""
        return None


def _open_client(config: McpServerConfig) -> McpStdioClient | McpHttpClient:
    """Create the transport client for a config (stdio vs streamable HTTP)."""
    client: McpStdioClient | McpHttpClient
    if config.url:
        client = McpHttpClient(config)
    else:
        client = McpStdioClient(config)
    try:
        client.open()
    except McpError:
        raise
    except Exception as exc:
        # Connection-level failures (refused, DNS, reset) surface as the typed
        # error — "server disappeared" is never a bare transport exception.
        raise McpError(f"mcp server '{config.name}': cannot connect: {exc}") from exc
    return client


def _with_server(settings: Any, server_name: str, action: str) -> Any:
    """Open a session, run one action, always close (fresh session per call)."""
    for config in all_servers(settings):
        if config.name == server_name:
            client = _open_client(config)
            try:
                if action == "list":
                    return client.list_tools()
                raise McpError(f"unknown action {action}")
            finally:
                client.close()
    raise McpError(f"mcp server '{server_name}' not configured")


def list_server_tools(settings: Any, server_name: str) -> list[dict[str, Any]]:
    """List tools on one configured server."""
    result = _with_server(settings, server_name, "list")
    return result if isinstance(result, list) else []


def call_tool(
    settings: Any, server_name: str, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Call one tool on one configured server (fresh session per call)."""
    for config in all_servers(settings):
        if config.name == server_name:
            client = _open_client(config)
            try:
                return client.call_tool(tool_name, arguments)
            finally:
                client.close()
    raise McpError(f"mcp server '{server_name}' not configured")


__all__ = [
    "McpError",
    "McpHttpClient",
    "McpServerConfig",
    "McpStdioClient",
    "all_servers",
    "call_tool",
    "is_configured",
    "list_server_tools",
    "parse_servers",
]
