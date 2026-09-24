"""Browser + Research agents (v3.1 Phase 6).

Research agent: fetches pages, extracts readable text with citation capture
(url, title, fetched_at). No LLM calls here — extraction is deterministic;
summarization belongs to the model layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from agent_system.domain.events import utcnow


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

    def fetch(self, url: str) -> Citation:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"blocked non-http scheme: {parsed.scheme}")
        resp = httpx.get(url, timeout=self._timeout, follow_redirects=True)
        resp.raise_for_status()
        extractor = _TextExtractor()
        extractor.feed(resp.text)
        snippet = extractor.text()[:500]
        return Citation(
            url=str(resp.url),
            title=extractor.title or url,
            fetched_at=utcnow().isoformat(),
            snippet=snippet,
        )

    def research(self, urls: list[str]) -> dict[str, Any]:
        citations: list[Citation] = []
        errors: dict[str, str] = {}
        for url in urls:
            try:
                citations.append(self.fetch(url))
            except Exception as exc:
                errors[url] = str(exc)[:200]
        return {
            "citations": [c.__dict__ for c in citations],
            "errors": errors,
            "extract": "\n\n".join(f"[{c.title}]({c.url})\n{c.snippet}" for c in citations),
        }
