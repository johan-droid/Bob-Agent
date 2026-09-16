"""Stub /api/v1 HTTP backend for CLI contract tests.

Serves canned-but-realistic responses on the same routes the CLI uses so
tests verify exact request paths/payloads and JSON schema stability without
booting the full application stack.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class StubBackend:
    """Record requests, serve canned responses, support forced status codes."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.next_status: int | None = None
        self._routes: dict[tuple[str, str], tuple[int, Any]] = {
            ("GET", "/api/v1/health"): (200, {"status": "ok"}),
            ("GET", "/api/v1/ready"): (
                200,
                {"status": "ok", "checks": {"database": True}},
            ),
            ("GET", "/api/v1/sessions"): (200, [{"id": "ses_01", "goal": "g", "status": "ACTIVE"}]),
            ("GET", "/api/v1/tasks"): (
                200,
                [
                    {
                        "id": "task_01",
                        "session_id": "ses_01",
                        "task_type": "code",
                        "title": "t",
                        "state": "QUEUED",
                        "agent_type": "code",
                        "depends_on": [],
                        "attempt": 0,
                        "last_error": None,
                    }
                ],
            ),
            ("GET", "/api/v1/workspaces"): (
                200,
                [
                    {
                        "id": "ws_01",
                        "name": "n",
                        "status": "CREATED",
                        "size_bytes": 0,
                        "file_count": 0,
                    }
                ],
            ),
            ("GET", "/api/v1/approvals"): (200, []),
            ("GET", "/api/v1/events"): (200, []),
        }

    def _handle(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        params: dict[str, Any],
    ) -> tuple[int, Any]:
        self.requests.append({"method": method, "path": path, "json": body, "params": params})
        if self.next_status is not None:
            status, payload = self.next_status, {"detail": "forced"}
            self.next_status = None
            return status, payload
        if (method, path) in self._routes:
            return self._routes[(method, path)]
        if method == "POST" and path == "/api/v1/sessions":
            return 201, {"id": "ses_new", "goal": (body or {}).get("goal", ""), "status": "ACTIVE"}
        if method == "POST" and path.startswith("/api/v1/tasks/") and path.endswith("/retry"):
            return 202, {"id": path.split("/")[-2], "state": "QUEUED", "attempt": 1}
        if method == "POST" and path.startswith("/api/v1/tasks/") and path.endswith("/transition"):
            return 200, {"id": path.split("/")[-2], "state": (body or {}).get("target", "")}
        if method == "POST" and path == "/api/v1/approvals":
            return 202, {"approval_id": "approval_01", "decision": "PENDING"}
        if method == "POST" and "/decision" in path:
            return 200, {"approval_id": path.split("/")[-2], "decision": "APPROVED"}
        if method == "POST" and path == "/api/v1/workspaces":
            return 201, {"id": "ws_new", "name": (body or {}).get("name", ""), "status": "CREATED"}
        if method == "DELETE" and path.startswith("/api/v1/workspaces/"):
            return 204, None
        if method == "GET" and path.startswith("/api/v1/tasks/"):
            return 200, {"id": path.split("/")[-1], "state": "PENDING"}
        return 404, {"detail": f"no route: {method} {path}"}

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _dispatch(self) -> None:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                status, payload = outer._handle(self.command, parsed.path, body, params)
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.end_headers()
                if data:
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _dispatch

            def log_message(self, *args: Any) -> None:
                pass

        return Handler

    def __init_subclass__(cls) -> None:  # pragma: no cover
        super().__init_subclass__()

    @property
    def base_url(self) -> str:
        if not hasattr(self, "_server"):
            self._server = HTTPServer(("127.0.0.1", 0), self._make_handler())
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def close(self) -> None:
        if hasattr(self, "_server"):
            self._server.shutdown()
            self._server.server_close()
