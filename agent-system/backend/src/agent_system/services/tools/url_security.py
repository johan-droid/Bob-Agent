"""Fail-closed URL validation for tools that fetch untrusted URLs.

The validator deliberately resolves *all* A and AAAA results before allowing a
request. Callers that open a connection must use the returned addresses at
the connection boundary too; validation by itself cannot prevent a later DNS
rebinding response.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpcore


class URLSecurityError(ValueError):
    """Raised when a URL is malformed or points at a prohibited destination."""


Resolver = Callable[[str, int], Iterable[str]]

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_PORTS = frozenset({80, 443})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")

_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google",
        "metadata.google.internal",
        "instance-data",
        "metadata.azure.com",
    }
)
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS, Azure and GCP link-local metadata
        ipaddress.ip_address("169.254.170.2"),  # ECS task metadata
        ipaddress.ip_address("100.100.100.200"),  # Alibaba metadata (CGNAT)
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 metadata
    }
)


@dataclass(frozen=True)
class ValidatedURL:
    """A normalized URL and the complete set of DNS answers it was checked against."""

    url: str
    hostname: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]


class PinnedBackend(httpcore.SyncBackend):
    """Network backend that dials a validated pinned IP while preserving the original host."""

    def __init__(self, pinned_host: str, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_host = pinned_host
        self._pinned_ip = pinned_ip

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        dest_ip = self._pinned_ip if host == self._pinned_host else host
        return super().connect_tcp(
            dest_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )



def _normalise_hostname(hostname: str) -> str:
    """Return the canonical DNS hostname or raise for ambiguous input."""
    host = hostname.rstrip(".").lower()
    if not host:
        raise URLSecurityError("URL host is empty")
    if any(ord(char) <= 0x20 for char in host):
        raise URLSecurityError("URL host contains control characters")
    try:
        # The built-in codec implements IDNA2003 consistently across supported
        # Python versions and gives us an ASCII name for policy comparisons and DNS.
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise URLSecurityError("URL host cannot be IDNA-normalized") from exc
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or any(not label or len(label) > 63 for label in labels):
        raise URLSecurityError("URL host is malformed")
    # Python's resolver accepts legacy integer, octal and hexadecimal IPv4
    # spellings. They are not RFC-compliant hostnames and may bypass textual
    # checks, so refuse them rather than relying on platform-specific parsing.
    lowered = ascii_host.lower()
    if (
        lowered.isdecimal()
        or lowered.startswith("0x")
        or any(
            label.startswith("0x") or (len(label) > 1 and label.startswith("0"))
            for label in lowered.split(".")
        )
    ):
        raise URLSecurityError("URL host uses an ambiguous numeric address representation")
    return ascii_host


def _blocked_host(hostname: str) -> bool:
    return (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname in _METADATA_HOSTS
        or hostname.endswith(".metadata.google.internal")
        or hostname.endswith(".instance-data")
        or hostname.endswith(".internal")
        or hostname.endswith(".local")
    )


def is_prohibited_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address can never be a public fetch destination."""
    if isinstance(address, ipaddress.IPv6Address):
        # IPv4-mapped IPv6 (::ffff:x.x.x.x)
        if address.ipv4_mapped is not None:
            return is_prohibited_ip(address.ipv4_mapped)
        # 6to4 encapsulation (2002::/16)
        if address.sixtofour is not None:
            return is_prohibited_ip(address.sixtofour)
        # Deprecated IPv4-compatible IPv6 (::x.x.x.x)
        if (int(address) >> 32) == 0 and int(address) > 1:
            return is_prohibited_ip(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        # NAT64 well-known prefix (64:ff9b::/96)
        if address in _NAT64:
            return is_prohibited_ip(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        # IPv4-translated (::ffff:0:0/96)
        if address in _IPV4_TRANSLATED:
            return is_prohibited_ip(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))

    if address in _METADATA_ADDRESSES or address in _CGNAT:
        return True
    # ``is_global`` is deliberately the final broad check: it covers private,
    # loopback, link-local, documentation, multicast, unspecified, and reserved
    # ranges as classified by the running Python's maintained IANA tables.
    return not address.is_global


def _resolve_all(
    hostname: str, port: int
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        infos = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise URLSecurityError("URL host could not be resolved") from exc
    answers: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for info in infos:
        try:
            answers.add(ipaddress.ip_address(info[4][0]))
        except (IndexError, ValueError) as exc:
            raise URLSecurityError("DNS returned an invalid address") from exc
    if not answers:
        raise URLSecurityError("URL host returned no addresses")
    return tuple(sorted(answers, key=lambda address: (address.version, int(address))))


def validate_url(url: str, *, resolver: Resolver | None = None) -> ValidatedURL:
    """Validate an HTTP(S) URL and every address currently returned for its host.

    ``resolver`` exists for controlled tests and for connection-pinning clients.
    It must return literal IP addresses only; malformed or empty output fails closed.
    """
    if not isinstance(url, str) or not url or any(ord(char) <= 0x20 for char in url):
        raise URLSecurityError("URL is empty or contains control characters")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise URLSecurityError("URL is malformed") from exc
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise URLSecurityError("only http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise URLSecurityError("URLs with embedded credentials are not allowed")
    if not parsed.hostname:
        raise URLSecurityError("URL host is required")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise URLSecurityError("URL port is malformed") from exc
    if port not in _ALLOWED_PORTS:
        raise URLSecurityError("only ports 80 and 443 are allowed")

    raw_host = parsed.hostname
    try:
        literal_address = ipaddress.ip_address(raw_host)
    except ValueError:
        literal_address = None
    hostname = (
        str(literal_address) if literal_address is not None else _normalise_hostname(raw_host)
    )
    if _blocked_host(hostname):
        raise URLSecurityError("URL host is prohibited")

    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]
    if literal_address is not None:
        addresses = (literal_address,)
    elif resolver is None:
        addresses = _resolve_all(hostname, port)
    else:
        try:
            raw_answers = tuple(resolver(hostname, port))
            addresses = tuple(
                sorted(
                    {ipaddress.ip_address(answer) for answer in raw_answers},
                    key=lambda address: (address.version, int(address)),
                )
            )
        except (OSError, TypeError, ValueError) as exc:
            raise URLSecurityError("URL host could not be resolved safely") from exc
        if not addresses:
            raise URLSecurityError("URL host returned no addresses")
    if any(is_prohibited_ip(address) for address in addresses):
        raise URLSecurityError("URL resolves to a prohibited address")

    host_for_url = f"[{hostname}]" if literal_address and literal_address.version == 6 else hostname
    netloc = host_for_url if parsed.port is None else f"{host_for_url}:{parsed.port}"
    normalised = urlunsplit(
        SplitResult(
            parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, parsed.fragment
        )
    )
    return ValidatedURL(normalised, hostname, port, addresses)


__all__ = [
    "PinnedBackend",
    "Resolver",
    "URLSecurityError",
    "ValidatedURL",
    "is_prohibited_ip",
    "validate_url",
]
