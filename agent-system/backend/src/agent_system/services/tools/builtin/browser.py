"""Browser automation capabilities (v3.1 §14) — interactive, not research.

Web *research* (``research_*``) fetches and cites documents. This group drives
a real browser: sessions, navigation, clicking, typing, selecting, scrolling,
extraction and screenshots. They are deliberately different capability groups
with different risk profiles, because a fetch cannot click "Pay now".

Playwright isolation: this module is the only place Playwright is imported, and
it is imported lazily. Without the optional dependency every capability fails
with a clear message instead of silently degrading.

Every action flows through the permission gate (``browser:session`` for
navigation, ``browser:interact`` for anything that changes page state) and
payment/credential flows are refused outright via the default-deny
``browser:transact`` scope.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.paths import scrub
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param

GROUP = "browser"
MAX_SESSIONS = 3
MAX_TEXT_CHARS = 8000

#: Actions/subjects that are never automated, regardless of approvals.
_TRANSACT_PATTERNS = (
    r"\bpay(ment|now|pal)?\b",
    r"\bcheckout\b",
    r"\bbuy\b",
    r"\bcredit[-_ ]?card\b",
    r"\bcard[-_ ]?number\b",
    r"\bcvv\b",
    r"\bbilling\b",
    r"\btransfer\b",
    r"\bwithdraw\b",
    r"\bpassword\b",
    r"\bapi[-_ ]?key\b",
    r"\bsecret\b",
)
_TRANSACT_RE = re.compile("|".join(_TRANSACT_PATTERNS), re.IGNORECASE)


class BrowserSessions:
    """Process-local Playwright sessions, keyed by a caller-chosen name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def ensure(self, ctx: ToolContext, name: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(name)
            if session is not None:
                return session
            if len(self._sessions) >= MAX_SESSIONS:
                raise ToolError(
                    f"browser session limit reached ({MAX_SESSIONS}); close one with "
                    "browser_session action=close"
                )
            playwright = _import_playwright()
            headless = bool(getattr(ctx.settings, "browser_headless", True))
            browser = playwright.chromium.launch(headless=headless)
            page = browser.new_page()
            session = {"playwright": playwright, "browser": browser, "page": page}
            self._sessions[name] = session
            return session

    def get(self, name: str) -> dict[str, Any]:
        session = self._sessions.get(name)
        if session is None:
            raise ToolError(f"no open browser session '{name}' (call browser_open first)")
        return session

    def close(self, name: str) -> bool:
        with self._lock:
            session = self._sessions.pop(name, None)
        if session is None:
            return False
        _safe_close(session)
        return True

    def close_all(self) -> int:
        with self._lock:
            names = list(self._sessions)
        for name in names:
            self.close(name)
        return len(names)

    def names(self) -> list[str]:
        return sorted(self._sessions)


sessions = BrowserSessions()


def _import_playwright() -> Any:
    try:
        from playwright.sync_api import (  # type: ignore[import-not-found]
            sync_playwright,
        )
    except Exception as exc:  # pragma: no cover — optional dependency
        raise ToolError(
            "browser automation requires Playwright: `pip install playwright` then "
            "`playwright install chromium`"
        ) from exc
    try:
        return sync_playwright().start()
    except Exception as exc:  # pragma: no cover — driver missing
        raise ToolError(f"could not start Playwright: {exc}") from exc


def _safe_close(session: dict[str, Any]) -> None:
    for key in ("browser", "playwright"):
        closable = session.get(key)
        if closable is None:
            continue
        try:
            closable.stop() if key == "playwright" else closable.close()
        except Exception:
            pass


def _url(args: dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ToolError("browser: an http(s) URL is required")
    return url


def _assert_not_transaction(subject: str) -> None:
    """Refuse payment/credential flows: default-deny, and not approvable here."""
    if _TRANSACT_RE.search(subject or ""):
        raise ToolError(
            "refusing to automate a payment/credential flow "
            "(scope browser:transact is default-deny)"
        )


def _session_name(args: dict[str, Any]) -> str:
    return str(args.get("session") or "default")


def _browser_open(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = _session_name(args)
    url = _url(args)
    _assert_not_transaction(url)
    session = sessions.ensure(ctx, name)
    page = session["page"]
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=int(args.get("timeout_ms") or 30000))
    except Exception as exc:
        raise ToolError(f"navigation failed: {scrub(str(exc))[:300]}") from exc
    return {"session": name, "url": page.url, "title": page.title()[:200]}


def _browser_navigate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _browser_open(args, ctx)


def _browser_click(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    selector = str(args.get("selector") or "").strip()
    if not selector:
        raise ToolError("browser_click: 'selector' is required")
    _assert_not_transaction(selector)
    session = sessions.get(_session_name(args))
    page = session["page"]
    try:
        page.click(selector, timeout=int(args.get("timeout_ms") or 15000))
    except Exception as exc:
        raise ToolError(f"click failed: {scrub(str(exc))[:300]}") from exc
    return {"session": _session_name(args), "url": page.url, "clicked": selector}


def _browser_type(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    selector = str(args.get("selector") or "").strip()
    text = str(args.get("text") or "")
    if not selector:
        raise ToolError("browser_type: 'selector' is required")
    _assert_not_transaction(selector)
    session = sessions.get(_session_name(args))
    page = session["page"]
    try:
        if bool(args.get("clear", True)):
            page.fill(selector, text, timeout=int(args.get("timeout_ms") or 15000))
        else:
            page.type(selector, text, timeout=int(args.get("timeout_ms") or 15000))
    except Exception as exc:
        raise ToolError(f"type failed: {scrub(str(exc))[:300]}") from exc
    return {"session": _session_name(args), "typed_into": selector, "chars": len(text)}


def _browser_select(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    selector = str(args.get("selector") or "").strip()
    value = str(args.get("value") or "")
    if not selector:
        raise ToolError("browser_select: 'selector' is required")
    session = sessions.get(_session_name(args))
    page = session["page"]
    try:
        selected = page.select_option(selector, value, timeout=int(args.get("timeout_ms") or 15000))
    except Exception as exc:
        raise ToolError(f"select failed: {scrub(str(exc))[:300]}") from exc
    return {"session": _session_name(args), "selected": selected}


def _browser_scroll(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    session = sessions.get(_session_name(args))
    page = session["page"]
    amount = int(args.get("pixels") or 600)
    try:
        page.mouse.wheel(0, amount)
    except Exception as exc:
        raise ToolError(f"scroll failed: {scrub(str(exc))[:300]}") from exc
    return {"session": _session_name(args), "scrolled_pixels": amount}


def _browser_extract(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    session = sessions.get(_session_name(args))
    page = session["page"]
    selector = str(args.get("selector") or "body")
    try:
        text = page.inner_text(selector, timeout=int(args.get("timeout_ms") or 15000))
    except Exception as exc:
        raise ToolError(f"extract failed: {scrub(str(exc))[:300]}") from exc
    return {
        "session": _session_name(args),
        "url": page.url,
        "selector": selector,
        "text": scrub(text or "")[:MAX_TEXT_CHARS],
    }


def _browser_screenshot(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from agent_system.services.tools.paths import allowed_roots

    session = sessions.get(_session_name(args))
    page = session["page"]
    roots = allowed_roots(ctx.settings)
    target = Path(str(args.get("path") or "")).expanduser() if args.get("path") else None
    if target is None:
        base = next((root for root in roots if root.name == "outputs"), roots[0])
        target = base / f"browser-{_session_name(args)}.png"
    elif not target.is_absolute():
        target = Path.cwd() / target
    target = target.resolve()
    if not any(target == root or target.is_relative_to(root) for root in roots):
        raise ToolError("browser_screenshot: path is outside the allowed roots")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(target), full_page=bool(args.get("full_page")))
    except Exception as exc:
        raise ToolError(f"screenshot failed: {scrub(str(exc))[:300]}") from exc
    return {"session": _session_name(args), "path": str(target), "bytes": target.stat().st_size}


def _browser_session(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "list")
    if action == "list":
        details = []
        for name in sessions.names():
            page = sessions.get(name)["page"]
            details.append({"session": name, "url": page.url})
        return {"action": "list", "sessions": details, "limit": MAX_SESSIONS}
    if action == "close":
        name = _session_name(args)
        return {"action": "close", "session": name, "closed": sessions.close(name)}
    if action == "close_all":
        return {"action": "close_all", "closed": sessions.close_all()}
    raise ToolError("browser_session: action must be list | close | close_all")


_SESSION_PARAM = _str_param("Browser session name (default: default)")
_TIMEOUT_PARAM = {"type": "integer", "minimum": 100, "maximum": 120_000}


def register(registry: ToolRegistry, settings: Any = None) -> None:  # noqa: ARG001
    registry.register(
        Tool(
            name="browser_open",
            description="Open a browser session and navigate to a URL.",
            parameters={
                "type": "object",
                "properties": {
                    "url": _str_param("http(s) URL"),
                    "session": _SESSION_PARAM,
                    "timeout_ms": _TIMEOUT_PARAM,
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_browser_open,
            scope="browser:session",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="browser_navigate",
            description="Navigate an existing browser session to a URL.",
            parameters={
                "type": "object",
                "properties": {
                    "url": _str_param("http(s) URL"),
                    "session": _SESSION_PARAM,
                    "timeout_ms": _TIMEOUT_PARAM,
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_browser_navigate,
            scope="browser:session",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="browser_click",
            description="Click an element in a browser session.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": _str_param("CSS selector"),
                    "session": _SESSION_PARAM,
                    "timeout_ms": _TIMEOUT_PARAM,
                },
                "required": ["selector"],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_browser_click,
            scope="browser:interact",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="browser_type",
            description="Type text into an element in a browser session.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": _str_param("CSS selector"),
                    "text": _str_param("Text to enter"),
                    "clear": {"type": "boolean", "description": "Replace existing value"},
                    "session": _SESSION_PARAM,
                    "timeout_ms": _TIMEOUT_PARAM,
                },
                "required": ["selector", "text"],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_browser_type,
            scope="browser:interact",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="browser_select",
            description="Select an option in a <select> element.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": _str_param("CSS selector"),
                    "value": _str_param("Option value"),
                    "session": _SESSION_PARAM,
                    "timeout_ms": _TIMEOUT_PARAM,
                },
                "required": ["selector", "value"],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_browser_select,
            scope="browser:interact",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="browser_scroll",
            description="Scroll the page in a browser session.",
            parameters={
                "type": "object",
                "properties": {
                    "pixels": {"type": "integer", "minimum": -20000, "maximum": 20000},
                    "session": _SESSION_PARAM,
                },
                "required": [],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_browser_scroll,
            scope="browser:interact",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="browser_extract",
            description="Extract readable text of an element from a browser session.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": _str_param("CSS selector (default body)"),
                    "session": _SESSION_PARAM,
                    "timeout_ms": _TIMEOUT_PARAM,
                },
                "required": [],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_browser_extract,
            scope="browser:extract",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="browser_screenshot",
            description="Screenshot a browser session into an allowed root.",
            parameters={
                "type": "object",
                "properties": {
                    "path": _str_param("Output path (default outputs/browser-<session>.png)"),
                    "full_page": {"type": "boolean"},
                    "session": _SESSION_PARAM,
                },
                "required": [],
                "additionalProperties": False,
            },
            risk="execute",
            handler=_browser_screenshot,
            scope="browser:extract",
            group=GROUP,
        )
    )
    registry.register(
        Tool(
            name="browser_session",
            description="Inspect and close browser sessions (list | close | close_all).",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "close", "close_all"],
                        "description": "Session operation",
                    },
                    "session": _SESSION_PARAM,
                },
                "required": [],
                "additionalProperties": False,
            },
            risk="read",
            handler=_browser_session,
            group=GROUP,
        )
    )


__all__ = ["GROUP", "MAX_SESSIONS", "BrowserSessions", "register", "sessions"]
