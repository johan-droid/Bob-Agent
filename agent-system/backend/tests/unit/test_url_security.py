"""Unit tests for the shared, fail-closed outbound URL policy."""

import socket
from typing import Any

import httpcore
import httpx
import pytest

from agent_system.services.tools.url_security import (
    PinnedBackend,
    URLSecurityError,
    ValidatedURL,
    create_pinned_transport,
    is_prohibited_ip,
    validate_url,
)

PUBLIC = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _resolve(*answers: str) -> Any:
    def resolver(host: str, port: int) -> tuple[str, ...]:
        assert host
        assert port in {80, 443}
        return answers

    return resolver


# Rule 1 & 4 & 15 & 19: Allow public HTTP and HTTPS on ports 80/443, normalise IDN and trailing dot
def test_allows_public_https_and_normalises_idn_and_trailing_dot() -> None:
    result = validate_url("https://ExAmPlE.com./path", resolver=_resolve(PUBLIC))
    assert result.url == "https://example.com/path"
    assert result.hostname == "example.com"
    assert result.port == 443
    assert [str(address) for address in result.addresses] == [PUBLIC]


def test_allows_public_http_port_80() -> None:
    result = validate_url("http://example.com:80/", resolver=_resolve(PUBLIC))
    assert result.url == "http://example.com:80/"
    assert result.hostname == "example.com"
    assert result.port == 80


def test_allows_public_ipv6_literal() -> None:
    result = validate_url(f"https://[{PUBLIC_V6}]/test")
    assert result.hostname == PUBLIC_V6
    assert result.port == 443
    assert [str(a) for a in result.addresses] == [PUBLIC_V6]


# Rule 1: Accept only HTTP and HTTPS
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/",
        "gopher://example.com/",
        "ws://example.com/",
        "wss://example.com/",
        "javascript:alert(1)",
        "data:text/html,hello",
        "ssh://user@example.com/",
    ],
)
def test_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(URLSecurityError, match="only http and https"):
        validate_url(url, resolver=_resolve(PUBLIC))


# Rule 2: Reject malformed URLs
@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https:///path",
        "http://",
        "https://",
        "https://example.com\n",
        "https://example.com\r",
        "https://example\x00.com/",
        "http://[not-an-ip]/",
    ],
)
def test_rejects_malformed_urls(url: str) -> None:
    with pytest.raises(URLSecurityError):
        validate_url(url, resolver=_resolve(PUBLIC))


# Rule 3: Reject URLs containing embedded usernames or passwords
@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.com/",
        "https://user:password@example.com/",
        "http://:password@example.com/",
        "https://admin:admin@93.184.216.34/",
    ],
)
def test_rejects_embedded_credentials(url: str) -> None:
    with pytest.raises(URLSecurityError, match="embedded credentials"):
        validate_url(url, resolver=_resolve(PUBLIC))


# Rule 4: Allow only ports 80 and 443; reject restricted ports
@pytest.mark.parametrize("port", [21, 22, 25, 8080, 8443, 8000, 3000, 9200, 6379, 27017])
def test_rejects_non_standard_ports(port: int) -> None:
    with pytest.raises(URLSecurityError, match="only ports 80 and 443"):
        validate_url(f"https://example.com:{port}/", resolver=_resolve(PUBLIC))


# Rule 5: Reject localhost and localhost subdomains
@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost.",
        "foo.localhost",
        "bar.sub.localhost",
        "api.localhost.",
    ],
)
def test_rejects_localhost_and_subdomains(host: str) -> None:
    with pytest.raises(URLSecurityError, match="prohibited"):
        validate_url(f"http://{host}/", resolver=_resolve(PUBLIC))


# Rule 6, 7, 8, 9, 10, 11, 12, 13, 14: Reject prohibited IP addresses & metadata
@pytest.mark.parametrize(
    ("url", "answers"),
    [
        # Loopback IPv4
        ("http://127.0.0.1/", ()),
        ("http://127.0.0.2/", ()),
        ("http://127.255.255.254/", ()),
        # Private IPv4: 10.0.0.0/8
        ("http://10.0.0.1/", ()),
        ("http://10.254.1.2/", ()),
        # Private IPv4: 172.16.0.0/12
        ("http://172.16.0.1/", ()),
        ("http://172.31.255.255/", ()),
        # Private IPv4: 192.168.0.0/16
        ("http://192.168.0.1/", ()),
        ("http://192.168.1.100/", ()),
        # Link-local IPv4: 169.254.0.0/16
        ("http://169.254.1.1/", ()),
        ("http://169.254.169.254/", ()),
        ("http://169.254.170.2/", ()),
        # CGNAT: 100.64.0.0/10
        ("http://100.64.0.1/", ()),
        ("http://100.100.100.200/", ()),  # Alibaba metadata in CGNAT
        ("http://100.127.255.254/", ()),
        # Reserved IPv4
        ("http://0.0.0.0/", ()),
        ("http://240.0.0.1/", ()),
        ("http://255.255.255.255/", ()),
        # IPv6 loopback & link-local
        ("http://[::1]/", ()),
        ("http://[fe80::1]/", ()),
        ("http://[fe80::1%eth0]/", ()),
        # Private IPv6 (Unique Local fc00::/7)
        ("http://[fd00::1]/", ()),
        ("http://[fc00::1]/", ()),
        # IPv4-mapped IPv6
        ("http://[::ffff:127.0.0.1]/", ()),
        ("http://[::ffff:10.0.0.1]/", ()),
        ("http://[::ffff:169.254.169.254]/", ()),
        ("http://[::ffff:100.100.100.200]/", ()),
        # IPv4-compatible IPv6
        ("http://[::127.0.0.1]/", ()),
        ("http://[::10.0.0.1]/", ()),
        # 6to4 with prohibited IPv4
        ("http://[2002:7f00:0001::]/", ()),
        ("http://[2002:0a00:0001::]/", ()),
        # NAT64 well-known prefix (64:ff9b::/96)
        ("http://[64:ff9b::127.0.0.1]/", ()),
        ("http://[64:ff9b::169.254.169.254]/", ()),
        # IPv4-translated (::ffff:0:0/96)
        ("http://[::ffff:0:127.0.0.1]/", ()),
        # Cloud metadata IPv6
        ("http://[fd00:ec2::254]/", ()),
        # Cloud metadata hostnames
        ("http://metadata.google.internal/", (PUBLIC,)),
        ("http://instance-data/", (PUBLIC,)),
        ("http://instance-data.ec2.internal/", (PUBLIC,)),
        ("http://metadata.azure.com/", (PUBLIC,)),
        ("http://test.internal/", (PUBLIC,)),
        ("http://service.local/", (PUBLIC,)),
        # Unusual numeric representations of loopback/private
        ("http://2130706433/", (PUBLIC,)),  # 127.0.0.1 decimal
        ("http://0x7f000001/", (PUBLIC,)),  # 127.0.0.1 hex
        ("http://017700000001/", (PUBLIC,)),  # 127.0.0.1 octal
        ("http://0177.0.0.1/", (PUBLIC,)),  # dotted octal
        ("http://0x7f.0.0.1/", (PUBLIC,)),  # dotted hex
        ("http://0.0.0.0/", (PUBLIC,)),
    ],
)
def test_rejects_prohibited_destinations(url: str, answers: tuple[str, ...]) -> None:
    with pytest.raises(URLSecurityError):
        validate_url(url, resolver=_resolve(*answers))


# Rule 16: Resolves both IPv4 and IPv6 DNS records
def test_resolves_both_ipv4_and_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int, int]] = []

    def mock_getaddrinfo(host: str, port: int, family: int = 0, type: int = 0) -> list[Any]:
        calls.append((host, port, family))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (PUBLIC_V6, port, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    result = validate_url("https://example.com/")
    assert len(calls) == 1
    assert calls[0][2] == socket.AF_UNSPEC  # Both IPv4 and IPv6 requested
    assert len(result.addresses) == 2


# Rule 17: Reject DNS resolution errors
def test_rejects_dns_failure() -> None:
    def failing_resolver(host: str, port: int) -> tuple[str, ...]:
        raise OSError("NXDOMAIN")

    with pytest.raises(URLSecurityError, match="could not be resolved"):
        validate_url("https://example.com/", resolver=failing_resolver)


def test_rejects_empty_dns_response() -> None:
    with pytest.raises(URLSecurityError, match="returned no addresses"):
        validate_url("https://example.com/", resolver=_resolve())


# Rule 18: Reject a hostname if ANY returned IP address is prohibited
def test_rejects_mixed_public_and_private_dns_answers() -> None:
    with pytest.raises(URLSecurityError, match="prohibited address"):
        validate_url("https://example.com/", resolver=_resolve(PUBLIC, "10.0.0.1"))


def test_rejects_mixed_public_and_loopback_dns_answers() -> None:
    with pytest.raises(URLSecurityError, match="prohibited address"):
        validate_url("https://example.com/", resolver=_resolve(PUBLIC, "127.0.0.1"))


def test_rejects_mixed_public_and_metadata_dns_answers() -> None:
    with pytest.raises(URLSecurityError, match="prohibited address"):
        validate_url("https://example.com/", resolver=_resolve(PUBLIC, "169.254.169.254"))


# Rule 20: Fail closed on uncertain validation
def test_fails_closed_on_resolver_exception() -> None:
    def crashing_resolver(host: str, port: int) -> tuple[str, ...]:
        raise ValueError("corrupted state")

    with pytest.raises(URLSecurityError, match="safely"):
        validate_url("https://example.com/", resolver=crashing_resolver)


def test_is_prohibited_ip_direct() -> None:
    import ipaddress

    assert is_prohibited_ip(ipaddress.ip_address("127.0.0.1"))
    assert is_prohibited_ip(ipaddress.ip_address("10.0.0.1"))
    assert is_prohibited_ip(ipaddress.ip_address("169.254.169.254"))
    assert is_prohibited_ip(ipaddress.ip_address("100.64.0.1"))
    assert is_prohibited_ip(ipaddress.ip_address("100.100.100.200"))
    assert is_prohibited_ip(ipaddress.ip_address("::1"))
    assert is_prohibited_ip(ipaddress.ip_address("::ffff:127.0.0.1"))
    assert is_prohibited_ip(ipaddress.ip_address("::127.0.0.1"))
    assert is_prohibited_ip(ipaddress.ip_address("64:ff9b::127.0.0.1"))
    assert is_prohibited_ip(ipaddress.ip_address("::ffff:0:127.0.0.1"))
    assert not is_prohibited_ip(ipaddress.ip_address(PUBLIC))
    assert not is_prohibited_ip(ipaddress.ip_address(PUBLIC_V6))


def test_pinned_backend_dials_pinned_ip_and_raises_on_host_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PinnedBackend("safe.example", "93.184.216.34")
    dialed_args: list[Any] = []

    def mock_super_connect(
        self: Any,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        dialed_args.append((host, port))
        return "stream"

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", mock_super_connect)

    res = backend.connect_tcp("safe.example", 443)
    assert res is not None
    assert dialed_args == [("93.184.216.34", 443)]

    with pytest.raises(URLSecurityError, match="host mismatch"):
        backend.connect_tcp("evil.example", 443)


def test_create_pinned_transport_builds_transport_or_raises() -> None:
    import ipaddress

    valid = ValidatedURL(
        "https://safe.example/",
        "safe.example",
        443,
        (ipaddress.ip_address("93.184.216.34"),),
    )
    transport = create_pinned_transport(valid)
    assert isinstance(transport, httpx.HTTPTransport)

    no_addrs = ValidatedURL("https://safe.example/", "safe.example", 443, ())
    with pytest.raises(URLSecurityError, match="No validated IP addresses"):
        create_pinned_transport(no_addrs)

    with pytest.raises(URLSecurityError, match="out of range"):
        create_pinned_transport(valid, address_index=5)
