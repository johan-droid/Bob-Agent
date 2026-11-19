"""Proxy and browser-egress security tests with mocked and live network boundaries."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any

import pytest

from agent_system.services.browser_proxy import (
    OutboundProxy,
    ProxyError,
    _parse_authority,
    _validate_destination,
)
from agent_system.services.tools.url_security import URLSecurityError, ValidatedURL


class _Writer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def test_proxy_rejects_private_and_metadata_connect_targets() -> None:
    for authority in (
        "127.0.0.1:443",
        "10.0.0.1:443",
        "172.16.0.1:443",
        "192.168.1.1:443",
        "[::1]:443",
        "[fd00::1]:443",
        "[fe80::1]:443",
        "[::ffff:127.0.0.1]:443",
        "169.254.169.254:80",
        "100.100.100.200:80",
        "[fd00:ec2::254]:443",
        "metadata.google.internal:80",
        "instance-data:80",
    ):
        host, port = _parse_authority(authority)
        with pytest.raises(URLSecurityError):
            _validate_destination(host, port, scheme="https")


def test_proxy_rejects_malformed_or_restricted_connect_ports() -> None:
    for authority in (
        "example.com",
        "example.com:not-a-port",
        "example.com:8443",
        "example.com:8080",
        "example.com:22",
        "::1:443",
    ):
        with pytest.raises((ProxyError, URLSecurityError, ValueError)):
            host, port = _parse_authority(authority)
            _validate_destination(host, port, scheme="https")


def test_proxy_pins_the_validated_literal_address(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[str, int]] = []

    async def open_connection(host: str, port: int) -> tuple[asyncio.StreamReader, _Writer]:
        opened.append((host, port))
        return asyncio.StreamReader(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    destination = ValidatedURL(
        "https://public.example/",
        "public.example",
        443,
        (ipaddress.ip_address("93.184.216.34"),),
    )
    asyncio.run(OutboundProxy()._open_pinned(destination))
    assert opened == [("93.184.216.34", 443)]


def test_proxy_prevents_dns_rebinding_by_never_redialing_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_hosts: list[str] = []

    async def open_connection(host: str, port: int) -> tuple[asyncio.StreamReader, _Writer]:
        opened_hosts.append(host)
        return asyncio.StreamReader(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    # Even if attacker's domain is rebind.attacker.com,
    # connection is dialed to literal pre-validated IP
    destination = ValidatedURL(
        "https://rebind.attacker.com/",
        "rebind.attacker.com",
        443,
        (ipaddress.ip_address("93.184.216.34"),),
    )
    asyncio.run(OutboundProxy()._open_pinned(destination))
    # Must NOT dial 'rebind.attacker.com'
    assert "rebind.attacker.com" not in opened_hosts
    assert opened_hosts == ["93.184.216.34"]


def test_proxy_rejects_dns_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_dns(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        import socket

        raise socket.gaierror("NXDOMAIN")

    import socket

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)
    with pytest.raises(URLSecurityError):
        _validate_destination("nonexistent.example", 443, scheme="https")


def test_proxy_rejects_mixed_public_and_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def mixed_dns(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        import socket

        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    import socket

    monkeypatch.setattr(socket, "getaddrinfo", mixed_dns)
    with pytest.raises(URLSecurityError):
        _validate_destination("rebind.example", 443, scheme="https")


def test_proxy_e2e_connect_tunnel_and_blocking() -> None:
    async def run_e2e() -> None:
        proxy = OutboundProxy()
        await proxy.start(host="127.0.0.1", port=0)
        proxy_port = proxy.port

        # Connect to proxy and send CONNECT to private IP -> must receive 403 Forbidden
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n")
        await writer.drain()

        response = await reader.read(1024)
        assert b"403 Forbidden" in response
        writer.close()
        await writer.wait_closed()

        # Connect to proxy and send invalid scheme or restricted port -> 403 Forbidden
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer2.write(b"CONNECT 169.254.169.254:80 HTTP/1.1\r\nHost: 169.254.169.254:80\r\n\r\n")
        await writer2.drain()

        response2 = await reader2.read(1024)
        assert b"403 Forbidden" in response2
        writer2.close()
        await writer2.wait_closed()

        await proxy.close()

    asyncio.run(run_e2e())


def test_proxy_failure_causes_browser_network_failure() -> None:
    """When proxy is stopped, Chromium fails closed with ERR_PROXY_CONNECTION_FAILED."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("Playwright not available")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": "http://127.0.0.1:59999"},  # Dead port
            args=["--disable-quic", "--proxy-bypass-list=<-loopback>"],
        )
        context = browser.new_context()
        page = context.new_page()
        failed = False
        try:
            page.goto("https://example.com", timeout=3000)
        except Exception as exc:
            failed = True
            assert "ERR_PROXY_CONNECTION_FAILED" in str(exc)
        assert failed, "Browser did not fail closed when proxy was unavailable"
        browser.close()
