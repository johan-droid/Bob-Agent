"""Browser + Research agents (v3.1 Phase 6).

Playwright integration is optional: when the package or browsers are not
installed, every operation raises `BrowserUnavailableError` — never a fake
result. All actions pass through permission scopes `browser:navigate`,
`browser:read`, `browser:download`, `browser:form`; `browser:transact` is
default-deny upstream (DangerousScopes).

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
from agent_system.services.permissions import ApprovalRequest, PermissionGate, Risk


class BrowserUnavailableError(RuntimeError):
    pass


class BrowserPermissionError(PermissionError):
    pass


BROWSER_SCOPES = {
    "navigate": "browser:navigate",
    "read": "browser:read",
    "download": "browser:download",
    "form": "browser:form",
}


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


class BrowserAgent:
    """Playwright-backed browser sessions; httpx fallback for plain reads."""

    def __init__(self, gate: PermissionGate, session_id: str | None = None) -> None:
        self._gate = gate
        self._session_id = session_id
        self._playwright: Any = None

    def _ensure_playwright(self) -> Any:
        if self._playwright is not None:
            return self._playwright
        try:
            from playwright.sync_api import (  # type: ignore[import-not-found]
                sync_playwright,
            )

            pw = sync_playwright().start()
            self._playwright = pw.chromium.launch(headless=True)
            return self._playwright
        except ImportError as exc:
            msg = (
                "playwright not installed — install with "
                "`uv add playwright && playwright install chromium`"
            )
            raise BrowserUnavailableError(msg) from exc
        except Exception as exc:
            raise BrowserUnavailableError(f"browser launch failed: {exc}") from exc

    def _authorize(self, action: str) -> None:
        scope = BROWSER_SCOPES[action]
        granted, _ = self._gate.check(
            ApprovalRequest(
                requested_action=f"browser:{action}",
                risk=Risk.LOW if action in {"navigate", "read"} else Risk.MEDIUM,
                scope=scope,
                requester="browser-agent",
                session_id=self._session_id,
            )
        )
        if not granted:
            raise BrowserPermissionError(f"no grant for {scope}")

    def navigate_and_read(self, url: str) -> dict[str, Any]:
        self._authorize("navigate")
        self._authorize("read")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"blocked non-http scheme: {parsed.scheme}")
        try:
            browser = self._ensure_playwright()
            page = browser.new_page()
            page.goto(url, timeout=30_000)
            title = page.title()
            text = page.inner_text("body")
            page.close()
        except BrowserUnavailableError:
            raise
        except Exception as exc:
            raise BrowserUnavailableError(f"navigation failed: {exc}") from exc
        return {"url": url, "title": title, "text": text[:200_000]}

    def close(self) -> None:
        if self._playwright is not None:
            try:
                self._playwright.close()
            except Exception:
                pass


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
