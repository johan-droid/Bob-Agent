"""Browser + Research agents (v3.1 Phase 6).

Research agent: fetches pages, extracts readable text with citation capture
(url, title, fetched_at). No LLM calls here — extraction is deterministic;
summarization belongs to the model layer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx

from agent_system.domain.events import utcnow
from agent_system.services.tools.url_security import PinnedBackend, ValidatedURL


@dataclass
class Citation:
    url: str
    title: str
    fetched_at: str
    snippet: str = ""


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0
        self.title = ""

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "title" and not self.title:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if getattr(self, "_in_title", False):
            self.title = data.strip()
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self._parts))


class ResearchAgent:
    """Deterministic page fetch + text extraction with citations."""

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout

    def fetch(self, url: str, *, guard: Callable[[str], Any] | None = None) -> Citation:
        """Fetch one URL and extract a citation.

        ``guard`` is an optional SSRF validator applied to the initial URL and
        to every redirect hop. It is a parameter, not a hard import, so an
        in-process caller (e.g. a test serving documents on loopback) is not
        forced through the public-internet policy that the tool callers use.
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"blocked non-http scheme: {parsed.scheme}")
        current_url = url
        hops = 0
        final_url = url
        body = ""
        while True:
            validated = guard(current_url) if guard is not None else None
            transport: httpx.HTTPTransport | None = None
            if isinstance(validated, ValidatedURL) and validated.addresses:
                try:
                    pool = httpcore.ConnectionPool(
                        network_backend=PinnedBackend(
                            validated.hostname, str(validated.addresses[0])
                        )
                    )
                    transport = httpx.HTTPTransport()
                    transport._pool = pool
                except Exception:
                    pass
            with httpx.Client(
                transport=transport, timeout=self._timeout, follow_redirects=False
            ) as client:
                resp = client.get(current_url)
                if resp.is_redirect:
                    hops += 1
                    if hops > 5:
                        raise ValueError("too many redirects")
                    nxt = resp.headers.get("location")
                    if not nxt:
                        final_url, body = str(resp.url), resp.text
                        break
                    nxt = str(httpx.URL(current_url).join(nxt))
                    current_url = nxt
                    continue
                resp.raise_for_status()
                final_url, body = str(resp.url), resp.text
                break
        extractor = _TextExtractor()
        extractor.feed(body)
        snippet = extractor.text()[:500]
        return Citation(
            url=final_url,
            title=extractor.title or final_url,
            fetched_at=utcnow().isoformat(),
            snippet=snippet,
        )

    def research(
        self, urls: list[str], *, guard: Callable[[str], Any] | None = None
    ) -> dict[str, Any]:
        citations: list[Citation] = []
        errors: dict[str, str] = {}
        for url in urls:
            try:
                citations.append(self.fetch(url, guard=guard))
            except Exception as exc:
                errors[url] = str(exc)[:200]
        return {
            "citations": [c.__dict__ for c in citations],
            "errors": errors,
            "extract": "\n\n".join(f"[{c.title}]({c.url})\n{c.snippet}" for c in citations),
        }
