"""Web / Search Provider Abstraction for Bob Agent.

Defines `SearchProvider` returning normalized search results:
- source_url
- title
- snippet
- timestamp
- metadata

Includes DuckDuckGo search provider as default,
plus provider fallback, timeout, and duplicate removal.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

import httpx

from agent_system.config import Settings
from agent_system.services.external_services.base import (
    ExternalService,
    ServiceHealth,
    ServiceHealthStatus,
    ServiceUnavailableError,
    redact_secrets,
)

logger = logging.getLogger(__name__)

USER_AGENT = "BobAgent/0.1 (+local search capability)"


@dataclass
class SearchResult:
    source_url: str
    title: str
    snippet: str
    timestamp: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "title": self.title,
            "snippet": self.snippet,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class SearchProvider(ExternalService, ABC):
    """Abstract base class for web search providers."""

    @abstractmethod
    def search(self, query: str, limit: int = 10, timeout: float = 15.0) -> list[SearchResult]:
        """Perform web search and return normalized SearchResult objects."""


class DuckDuckGoSearchProvider(SearchProvider):
    """HTML DuckDuckGo search provider (zero cost, no API key required)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.endpoint = "https://html.duckduckgo.com/html/?q="

    @property
    def name(self) -> str:
        return "search_duckduckgo"

    @property
    def is_configured(self) -> bool:
        return True

    @property
    def is_enabled(self) -> bool:
        return True

    def check_health(self, timeout: float = 5.0) -> ServiceHealth:
        start = time.monotonic()
        try:
            results = self.search("test", limit=1, timeout=timeout)
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=True,
                authenticated=True,
                status=ServiceHealthStatus.OK if results else ServiceHealthStatus.DEGRADED,
                latency_ms=latency,
                last_success=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                details={"endpoint": self.endpoint},
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000.0
            return ServiceHealth(
                name=self.name,
                configured=True,
                enabled=True,
                reachable=False,
                authenticated=False,
                status=ServiceHealthStatus.UNAVAILABLE,
                latency_ms=latency,
                last_error=redact_secrets(str(exc)),
                details={"endpoint": self.endpoint},
            )

    def search(self, query: str, limit: int = 10, timeout: float = 15.0) -> list[SearchResult]:
        if not query or not query.strip():
            return []

        url = f"{self.endpoint}{quote_plus(query.strip())}"
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                res = client.get(url)
                res.raise_for_status()
                html = res.text
        except Exception as exc:
            raise ServiceUnavailableError(
                f"Search fetch failed for '{query}': {exc}", service=self.name
            ) from exc

        return _parse_duckduckgo_html(html, limit)


def _parse_duckduckgo_html(html: str, limit: int) -> list[SearchResult]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _regex_parse_duckduckgo_html(html, limit)

    soup = BeautifulSoup(html, "html.parser")
    results: list[SearchResult] = []
    seen_urls: set[str] = set()

    for result in soup.find_all("div", class_=re.compile(r"result|links_main")):
        if len(results) >= limit:
            break
        a_tag = result.find("a", class_=re.compile(r"result__a|result-title"))
        if not a_tag:
            continue

        raw_href = a_tag.get("href", "")
        clean_url = _unwrap_ddg_url(str(raw_href))
        if not clean_url or clean_url in seen_urls or not clean_url.startswith("http"):
            continue

        title = a_tag.get_text(strip=True)
        snippet_tag = result.find("a", class_=re.compile(r"result__snippet")) or result.find(
            "div", class_=re.compile(r"result__snippet")
        )
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        seen_urls.add(clean_url)
        results.append(
            SearchResult(
                source_url=clean_url,
                title=title,
                snippet=snippet,
                metadata={"provider": "duckduckgo"},
            )
        )

    return results


def _regex_parse_duckduckgo_html(html: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        if len(results) >= limit:
            break
        raw_href, raw_title = match.groups()
        clean_url = _unwrap_ddg_url(raw_href)
        if not clean_url or clean_url in seen_urls or not clean_url.startswith("http"):
            continue
        clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
        seen_urls.add(clean_url)
        results.append(SearchResult(source_url=clean_url, title=clean_title, snippet=""))
    return results


def _unwrap_ddg_url(href: str) -> str:
    if "uddg=" in href:
        match = re.search(r"uddg=([^&]+)", href)
        if match:
            from urllib.parse import unquote

            return unquote(match.group(1))
    return href
