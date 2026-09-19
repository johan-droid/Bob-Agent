"""Research capabilities (v3.1 §14) — structured, provenance-preserving.

    research_search -> research_fetch -> research_extract -> research_citations
                    -> source_metadata -> compare_sources

Distinct from ``browser_*``: research fetches documents and preserves where
every extracted fact came from. Results always carry source URLs and timestamps
so the agent (and the trace UI) can attribute claims; the capabilities never
present extracted text as authoritative on their own — that is the prompt's job
and the docs say so plainly.

Fetching is read-tier (no approval): reading public documents is the ordinary
research loop. Nothing here can write, submit or transact.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote_plus, urlparse

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.paths import scrub
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param

GROUP = "research"
MAX_RESULTS = 20
MAX_TEXT_CHARS = 12000
SEARCH_URL = "https://html.duckduckgo.com/html/?q="
USER_AGENT = "BobAgent/0.1 (+local research capability)"


def _http_get(url: str, timeout: float = 20.0) -> str:
    import httpx

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except Exception as exc:
        raise ToolError(f"fetch failed for {url}: {scrub(str(exc))[:300]}") from exc


def _require_http(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise ToolError("an http(s) URL is required")
    return url


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _readable_text(html: str) -> str:
    """Extract readable text using the project's own HTML extractor."""
    from agent_system.agents.browser_research import _TextExtractor

    parser = _TextExtractor()
    parser.feed(html)
    collapsed: str = re.sub(r"\n{3,}", "\n\n", parser.text())
    return collapsed.strip()


def _research_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
    from agent_system.config import get_settings
    from agent_system.services.external_services.search import DuckDuckGoSearchProvider
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("research_search: 'query' is required")
    limit = min(int(args.get("limit") or 5), MAX_RESULTS)
    try:
        provider = DuckDuckGoSearchProvider(get_settings())
        search_results = provider.search(query, limit=limit)
        results = [r.to_dict() for r in search_results]
    except Exception as exc:
        results = []
    if not results:
        raise ToolError(
            "research_search returned no results (network blocked or the search endpoint "
            "changed); try research_fetch with a known URL"
        )
    return {"query": query, "retrieved_at": _now(), "count": len(results), "results": results}


def _parse_search_results(html: str, limit: int) -> list[dict[str, Any]]:
    """Parse result links out of the HTML search endpoint."""
    results: list[dict[str, Any]] = []
    pattern = re.compile(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
    for match in pattern.finditer(html):
        url = _clean_result_url(match.group(1))
        title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if not url or not title:
            continue
        results.append({"title": title[:200], "url": url, "source": urlparse(url).netloc})
        if len(results) >= limit:
            break
    return results


def _clean_result_url(url: str) -> str:
    """Unwrap the search endpoint's redirect wrapper when present."""
    from urllib.parse import parse_qs, unquote
    from urllib.parse import urlparse as _urlparse

    if "uddg=" in url:
        query = parse_qs(_urlparse(url).query)
        target = query.get("uddg")
        if target:
            return unquote(target[0])
    return url


def _research_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
    url = _require_http(str(args.get("url") or ""))
    html = _http_get(url, timeout=float(args.get("timeout") or 20.0))
    text = _readable_text(html)
    return {
        "url": url,
        "source": urlparse(url).netloc,
        "retrieved_at": _now(),
        "chars": len(text),
        "text": text[:MAX_TEXT_CHARS],
        "truncated": len(text) > MAX_TEXT_CHARS,
    }


def _research_extract(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
    url = _require_http(str(args.get("url") or ""))
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("research_extract: 'query' is required")
    html = _http_get(url, timeout=float(args.get("timeout") or 20.0))
    text = _readable_text(html)
    try:
        needle = re.compile(query, re.IGNORECASE)
    except re.error as exc:
        raise ToolError(f"invalid extraction pattern: {exc}") from exc
    snippets = [
        " ".join(line.split()) for line in text.splitlines() if needle.search(line) and line.strip()
    ][: int(args.get("max_snippets") or 10)]
    return {
        "url": url,
        "source": urlparse(url).netloc,
        "retrieved_at": _now(),
        "count": len(snippets),
        "snippets": snippets,
        "provenance": {"url": url, "extracted_at": _now(), "pattern": query},
    }


def _research_citations(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    urls = args.get("urls")
    if not isinstance(urls, list) or not urls:
        raise ToolError("research_citations: 'urls' must be a non-empty list")
    from agent_system.agents.browser_research import ResearchAgent

    report = ResearchAgent().research([_require_http(str(u)) for u in urls])
    citations = report.get("citations", []) if isinstance(report, dict) else []
    errors = report.get("errors", {}) if isinstance(report, dict) else {}
    return {
        "retrieved_at": _now(),
        "count": len(citations),
        "citations": citations,
        "errors": errors,
        "note": "every finding must be attributed to one of these sources",
    }


def _source_metadata(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
    url = _require_http(str(args.get("url") or ""))
    import httpx

    try:
        with httpx.Client(
            timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            response = client.head(url)
            if response.status_code >= 400:
                response = client.get(url)
    except Exception as exc:
        raise ToolError(f"metadata request failed: {scrub(str(exc))[:300]}") from exc
    parsed = urlparse(url)
    return {
        "url": url,
        "scheme": parsed.scheme,
        "host": parsed.netloc,
        "path": parsed.path,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "last_modified": response.headers.get("last-modified"),
        "retrieved_at": _now(),
    }


def _compare_sources(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
    urls = args.get("urls")
    if not isinstance(urls, list) or len(urls) < 2:
        raise ToolError("compare_sources: 'urls' must contain at least two URLs")
    topic = str(args.get("topic") or "").strip()
    sources: list[dict[str, Any]] = []
    for raw in urls[:8]:
        url = _require_http(str(raw))
        html = _http_get(url)
        text = _readable_text(html)
        entry: dict[str, Any] = {
            "url": url,
            "source": urlparse(url).netloc,
            "chars": len(text),
            "retrieved_at": _now(),
        }
        if topic:
            try:
                needle = re.compile(topic, re.IGNORECASE)
            except re.error as exc:
                raise ToolError(f"invalid topic pattern: {exc}") from exc
            entry["mentions"] = sum(1 for match in needle.finditer(text))
        sources.append(entry)
    return {
        "topic": topic or None,
        "compared_at": _now(),
        "sources": sources,
        "note": (
            "counts only; run research_extract on each source for the supporting text — "
            "never present a count as a conclusion"
        ),
    }


def register(registry: ToolRegistry, settings: Any = None) -> None:  # noqa: ARG001
    registry.register(
        Tool(
            name="research_search",
            description="Search the web and return titled results with their source hosts.",
            parameters={
                "type": "object",
                "properties": {
                    "query": _str_param("Search query"),
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_research_search,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="research_fetch",
            description="Fetch a URL and return its readable text with provenance metadata.",
            parameters={
                "type": "object",
                "properties": {
                    "url": _str_param("http(s) URL"),
                    "timeout": {"type": "number", "minimum": 1, "maximum": 120},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_research_fetch,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="research_extract",
            description="Extract the snippets matching a pattern from a URL, with provenance.",
            parameters={
                "type": "object",
                "properties": {
                    "url": _str_param("http(s) URL"),
                    "query": _str_param("Regular expression to extract"),
                    "max_snippets": {"type": "integer", "minimum": 1, "maximum": 50},
                    "timeout": {"type": "number", "minimum": 1, "maximum": 120},
                },
                "required": ["url", "query"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_research_extract,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="research_citations",
            description="Fetch several URLs and return citable snippets for each.",
            parameters={
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 8,
                    }
                },
                "required": ["urls"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_research_citations,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="source_metadata",
            description="Report status, content type and retrieval time for a source URL.",
            parameters={
                "type": "object",
                "properties": {"url": _str_param("http(s) URL")},
                "required": ["url"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_source_metadata,
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="compare_sources",
            description=(
                "Compare several sources for a topic (counts + provenance, not conclusions)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 8,
                    },
                    "topic": _str_param("Optional pattern to count across sources"),
                },
                "required": ["urls"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_compare_sources,
            group=GROUP,
        )
    )
    # ``web_fetch`` stays available for backward compatibility with recorded
    # transcripts and existing prompts; it is research_fetch by another name.
    registry.register(
        Tool(
            name="web_fetch",
            description="Fetch a URL and return title + readable text (alias of research_fetch).",
            parameters={
                "type": "object",
                "properties": {"url": _str_param("http(s) URL")},
                "required": ["url"],
                "additionalProperties": False,
            },
            risk="read",
            handler=_web_fetch,
            group=GROUP,
        )
    )


def _web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    url = _require_http(str(args.get("url") or ""))
    try:
        from agent_system.agents.browser_research import ResearchAgent

        report = ResearchAgent().research([url])
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"fetch failed: {scrub(str(exc))[:300]}") from exc
    citations = report.get("citations", []) if isinstance(report, dict) else []
    errors = report.get("errors", {}) if isinstance(report, dict) else {}
    if errors:
        raise ToolError(f"fetch failed: {list(errors.values())[0]}")
    if not citations:
        raise ToolError("no content extracted")
    cite = citations[0] if isinstance(citations[0], dict) else {}
    return {
        "url": url,
        "title": str(cite.get("title", "")),
        "text": str(cite.get("snippet", ""))[:8000],
        "retrieved_at": _now(),
    }


__all__ = ["GROUP", "register"]
