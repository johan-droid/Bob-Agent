"""A minimal, pinned-destination HTTP CONNECT proxy for browser traffic.

The proxy is intentionally not a general purpose forward proxy.  It accepts
only HTTP requests and HTTPS CONNECT tunnels to public port 80/443 targets.
Before opening a socket it resolves and validates all DNS answers, then dials
one of those literal addresses.  HTTPS remains end-to-end: the browser sends
the original hostname as TLS SNI and verifies the remote certificate itself.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from agent_system.services.tools.url_security import URLSecurityError, ValidatedURL, validate_url

_MAX_HEADER_BYTES: Final = 32 * 1024
_MAX_BODY_BYTES: Final = 8 * 1024 * 1024
_PROXY_TIMEOUT: Final = 20.0


class ProxyError(ValueError):
    """An invalid or unsafe proxy request."""


class ProxyAuthError(ProxyError):
    """Proxy authentication failed or missing."""


class ProxyMethodNotAllowedError(ProxyError):
    """HTTP method is not allowed by the proxy policy."""


class ProxyUpstreamError(ProxyError):
    """Failed to reach upstream destination."""


@dataclass(frozen=True)
class _Request:
    method: str
    target: str
    version: str
    headers: tuple[tuple[str, str], ...]


class OutboundProxy:
    """Fail-closed HTTP proxy that pins every browser destination connection."""

    def __init__(
        self,
        *,
        auth_token: str | None = None,
        require_token: bool = False,
        timeout: float = _PROXY_TIMEOUT,
    ) -> None:
        if require_token and not auth_token:
            raise ValueError("Proxy authentication token is required when require_token=True")
        self._auth_token = auth_token
        self._require_token = require_token
        self._timeout = timeout
        self._server: asyncio.AbstractServer | None = None

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self._server = await asyncio.start_server(self._handle_client, host, port)

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("proxy is not running")
        sockets = getattr(self._server, "sockets", None)
        if not sockets:
            raise RuntimeError("proxy is not running")
        return int(sockets[0].getsockname()[1])

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("proxy has not been started")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await self._read_request(client_reader)
            self._check_auth(request)
            if request.method == "CONNECT":
                await self._handle_connect(request, client_reader, client_writer)
            elif request.method in {"GET", "HEAD"}:
                await self._handle_http(request, client_reader, client_writer)
            else:
                raise ProxyMethodNotAllowedError(f"method {request.method} not allowed")
        except ProxyAuthError:
            await self._send_response(
                client_writer,
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="Bob-Browser-Proxy"\r\n'
                b"Content-Length: 0\r\nConnection: close\r\n\r\n",
            )
        except ProxyMethodNotAllowedError:
            await self._send_response(
                client_writer,
                b"HTTP/1.1 405 Method Not Allowed\r\n"
                b"Allow: GET, HEAD, CONNECT\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n",
            )
        except URLSecurityError:
            await self._send_response(
                client_writer,
                b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            )
        except (TimeoutError, ProxyUpstreamError, OSError):
            await self._send_response(
                client_writer,
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            )
        except Exception:
            await self._send_response(
                client_writer,
                b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            )
        finally:
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except OSError:
                pass

    def _check_auth(self, request: _Request) -> None:
        if not self._auth_token:
            return
        auth_headers = [v for name, v in request.headers if name.lower() == "proxy-authorization"]
        if not auth_headers:
            raise ProxyAuthError("missing Proxy-Authorization")
        header_val = auth_headers[0].strip()
        if header_val.startswith("Bearer "):
            token = header_val[7:].strip()
            if token != self._auth_token:
                raise ProxyAuthError("invalid bearer token")
            return
        if header_val.startswith("Basic "):
            raw = header_val[6:].strip()
            try:
                decoded = base64.b64decode(raw).decode("utf-8")
                parts = decoded.split(":", 1)
                secret = parts[1] if len(parts) == 2 else parts[0]
                if secret != self._auth_token:
                    raise ProxyAuthError("invalid credentials")
                return
            except Exception as exc:
                raise ProxyAuthError("malformed basic auth") from exc
        raise ProxyAuthError("unsupported auth scheme")

    async def _handle_connect(
        self,
        request: _Request,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        hostname, port = _parse_authority(request.target)
        destination = _validate_destination(hostname, port, scheme="https")
        remote_reader, remote_writer = await self._open_pinned(destination)
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()
        await self._tunnel(client_reader, client_writer, remote_reader, remote_writer)

    async def _handle_http(
        self,
        request: _Request,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        parsed = urlsplit(request.target)
        if parsed.scheme.lower() != "http" or not parsed.netloc:
            raise ProxyError("only absolute HTTP proxy requests are supported")
        destination = validate_url(request.target)
        remote_reader, remote_writer = await self._open_pinned(destination)
        try:
            body = await self._read_body(request.headers, client_reader)
            remote_writer.write(_serialise_http_request(request, destination, body))
            await remote_writer.drain()
            await _copy(remote_reader, client_writer)
        finally:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except OSError:
                pass

    async def _open_pinned(
        self, destination: ValidatedURL
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        last_error: OSError | None = None
        for address in destination.addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(host=str(address), port=destination.port),
                    timeout=self._timeout,
                )
            except OSError as exc:
                last_error = exc
        raise ProxyUpstreamError(
            "no validated destination address could be reached"
        ) from last_error

    async def _read_request(self, reader: asyncio.StreamReader) -> _Request:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self._timeout)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise ProxyError("malformed proxy request") from exc
        if len(raw) > _MAX_HEADER_BYTES:
            raise ProxyError("proxy request headers are too large")
        try:
            lines = raw[:-4].decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ")
        except (UnicodeDecodeError, IndexError, ValueError) as exc:
            raise ProxyError("malformed proxy request line") from exc
        if not method.isalpha() or version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ProxyError("unsupported proxy request")
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line or line[0] in " \t" or ":" not in line:
                raise ProxyError("malformed proxy header")
            name, value = line.split(":", 1)
            if not name or any(ord(char) < 33 or ord(char) > 126 for char in name):
                raise ProxyError("malformed proxy header name")
            headers.append((name, value.strip()))
        return _Request(method.upper(), target, version, tuple(headers))

    async def _read_body(
        self, headers: Iterable[tuple[str, str]], reader: asyncio.StreamReader
    ) -> bytes:
        content_lengths = [value for name, value in headers if name.lower() == "content-length"]
        if any(name.lower() == "transfer-encoding" for name, _ in headers):
            raise ProxyError("chunked proxy requests are not supported")
        if len(content_lengths) > 1:
            raise ProxyError("ambiguous request body length")
        if not content_lengths:
            return b""
        try:
            length = int(content_lengths[0])
        except ValueError as exc:
            raise ProxyError("invalid request body length") from exc
        if not 0 <= length <= _MAX_BODY_BYTES:
            raise ProxyError("request body is too large")
        try:
            return await asyncio.wait_for(reader.readexactly(length), timeout=self._timeout)
        except asyncio.IncompleteReadError as exc:
            raise ProxyError("incomplete request body") from exc

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        remote_reader: asyncio.StreamReader,
        remote_writer: asyncio.StreamWriter,
    ) -> None:
        to_remote = asyncio.create_task(_copy(client_reader, remote_writer))
        to_client = asyncio.create_task(_copy(remote_reader, client_writer))
        done, pending = await asyncio.wait(
            {to_remote, to_client}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
        remote_writer.close()
        try:
            await remote_writer.wait_closed()
        except OSError:
            pass

    async def _send_response(self, writer: asyncio.StreamWriter, response: bytes) -> None:
        if writer.is_closing():
            return
        # Do not reflect target URLs, credentials, query strings or resolver errors.
        writer.write(response)
        try:
            await writer.drain()
        except OSError:
            pass


def _parse_authority(authority: str) -> tuple[str, int]:
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 1 or authority[closing + 1 : closing + 2] != ":":
            raise ProxyError("malformed CONNECT authority")
        host, raw_port = authority[1:closing], authority[closing + 2 :]
    else:
        if authority.count(":") != 1:
            raise ProxyError("CONNECT requires host:port")
        host, raw_port = authority.rsplit(":", 1)
    if not host:
        raise ProxyError("CONNECT host is missing")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ProxyError("CONNECT port is invalid") from exc
    return host, port


def _validate_destination(hostname: str, port: int, *, scheme: str) -> ValidatedURL:
    host = f"[{hostname}]" if ":" in hostname else hostname
    return validate_url(f"{scheme}://{host}:{port}/")


def _serialise_http_request(request: _Request, destination: ValidatedURL, body: bytes) -> bytes:
    parsed = urlsplit(destination.url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    default_port = 443 if parsed.scheme == "https" else 80
    host = destination.hostname
    host_header = f"[{host}]" if ":" in host else host
    if destination.port != default_port:
        host_header = f"{host_header}:{destination.port}"
    blocked_headers = {"connection", "host", "proxy-connection", "proxy-authorization"}
    lines = [f"{request.method} {path} {request.version}", f"Host: {host_header}"]
    lines.extend(
        f"{name}: {value}" for name, value in request.headers if name.lower() not in blocked_headers
    )
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1") + body


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await writer.drain()


async def _run_proxy(
    host: str, port: int, token: str | None = None, require_token: bool = False
) -> None:
    proxy = OutboundProxy(auth_token=token, require_token=require_token)
    await proxy.start(host, port)
    await proxy.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bob Agent pinned-destination browser proxy")
    default_host = os.environ.get("BROWSER_PROXY_HOST", "127.0.0.1")
    default_port = int(os.environ.get("BROWSER_PROXY_PORT", "8080"))
    default_token = os.environ.get("BROWSER_PROXY_TOKEN")
    default_require = os.environ.get("BROWSER_PROXY_REQUIRE_TOKEN", "false").lower() in {
        "1",
        "true",
        "yes",
    }

    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument(
        "--token",
        default=default_token,
        help="Proxy authentication token (required in production)",
    )
    parser.add_argument(
        "--require-token",
        action="store_true",
        default=default_require,
        help="Require proxy authentication token on startup",
    )
    args = parser.parse_args()
    if args.require_token and not args.token:
        parser.error("Proxy authentication token is required (--token or BROWSER_PROXY_TOKEN).")
    asyncio.run(
        _run_proxy(args.host, args.port, token=args.token, require_token=args.require_token)
    )


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()

__all__ = [
    "OutboundProxy",
    "ProxyAuthError",
    "ProxyError",
    "ProxyMethodNotAllowedError",
    "ProxyUpstreamError",
    "main",
]
