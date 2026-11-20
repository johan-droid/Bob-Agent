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
from urllib.parse import urlsplit

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.paths import scrub
from agent_system.services.tools.registry import Tool, ToolContext, ToolRegistry, _str_param
from agent_system.services.tools.url_security import URLSecurityError, validate_url

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

    SESSION_TTL_SECONDS = 1800.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def _evict_expired(self) -> None:
        import time as _time

        now = _time.monotonic()
        expired = [
            n
            for n, s in self._sessions.items()
            if now - float(s.get("created_at", now)) > self.SESSION_TTL_SECONDS
        ]
        for n in expired:
            sess = self._sessions.pop(n, None)
            if sess is not None:
                try:
                    _safe_close(sess)
                except Exception:
                    pass

    def ensure(self, ctx: ToolContext, name: str) -> dict[str, Any]:
        import time as _time

        with self._lock:
            self._evict_expired()
            session = self._sessions.get(name)
            if session is not None:
                session["last_used"] = _time.monotonic()
                return session
            if len(self._sessions) >= MAX_SESSIONS:
                raise ToolError(
                    f"browser session limit reached ({MAX_SESSIONS}); close one with "
                    "browser_session action=close"
                )
            playwright = _import_playwright()
            headless = bool(getattr(ctx.settings, "browser_headless", True))
            proxy_url = _required_proxy_url(ctx.settings)
            proxy_token = str(getattr(ctx.settings, "browser_proxy_token", "") or "").strip()
            proxy_config: dict[str, str] = {"server": proxy_url}
            if proxy_token:
                proxy_config["username"] = "bob"
                proxy_config["password"] = proxy_token

            # Chromium receives no user data directory and therefore no personal
            # profile. QUIC is disabled because it can bypass an HTTP CONNECT
            # proxy over UDP; WebRTC non-proxied UDP is disabled to prevent
            # proxy evasion and local IP leakage; loopback bypass is disabled.
            browser = playwright.chromium.launch(
                headless=headless,
                proxy=proxy_config,
                args=[
                    "--disable-quic",
                    "--proxy-bypass-list=<-loopback>",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            try:
                # Context-level routes apply to every page, popup, iframe and
                # subresource in this isolated session. Service workers are
                # blocked because they otherwise evade Playwright routing.
                context = browser.new_context(service_workers="block")
                _install_request_guard(context)
                page = context.new_page()
            except Exception:
                try:
                    browser.close()
                finally:
                    playwright.stop()
                raise
            now = _time.monotonic()
            session = {
                "playwright": playwright,
                "browser": browser,
                "context": context,
                "page": page,
                "created_at": now,
                "last_used": now,
            }
            self._sessions[name] = session
            return session

    def get(self, name: str) -> dict[str, Any]:
        import time as _time

        session = self._sessions.get(name)
        if session is None:
            raise ToolError(f"no open browser session '{name}' (call browser_open first)")
        try:
            age = _time.monotonic() - float(session.get("created_at", _time.monotonic()))
            if age > self.SESSION_TTL_SECONDS:
                self.close(name)
                raise ToolError(f"browser session '{name}' expired; reopen it")
            session["last_used"] = _time.monotonic()
        except ToolError:
            raise
        except Exception:
            pass
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
        from playwright.sync_api import sync_playwright
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
    for key in ("context", "browser", "playwright"):
        closable = session.get(key)
        if closable is None:
            continue
        try:
            closable.stop() if key == "playwright" else closable.close()
        except Exception:
            pass


def _required_proxy_url(settings: Any) -> str:
    """Return the deployment-provided proxy endpoint or fail before Chromium starts."""
    proxy_url = str(getattr(settings, "browser_proxy_url", "") or "").strip()
    if not proxy_url:
        raise ToolError(
            "BROWSER_PROXY_URL is not configured; browser egress proxy is required "
            "for secure operation. Set BROWSER_PROXY_URL in environment or .env. "
            "See docs/OPERATIONS.md."
        )
    try:
        parsed = urlsplit(proxy_url)
    except ValueError as exc:
        raise ToolError("browser proxy URL is malformed") from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ToolError("browser requires a configured HTTP outbound proxy")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise ToolError("browser proxy URL has an invalid port") from exc
    if not 1 <= port <= 65535:
        raise ToolError("browser proxy URL has an invalid port")
    return proxy_url


def _install_request_guard(context: Any) -> None:
    """Install fail-closed guards before a context can open a network page."""
    import time as _time

    validated_host_cache: dict[str, float] = {}

    def guard(route: Any) -> None:
        request_url = str(route.request.url)
        # Safe browser-internal schemes
        if request_url.startswith(("data:", "about:")):
            if hasattr(route, "fallback"):
                route.fallback()
            else:
                route.continue_()
            return

        try:
            parsed = urlsplit(request_url)
            scheme = parsed.scheme.lower()
            if scheme not in {"http", "https"}:
                route.abort("blockedbyclient")
                return
            if parsed.username or parsed.password:
                route.abort("blockedbyclient")
                return
            host = (parsed.hostname or "").rstrip(".").lower()
            if not host:
                route.abort("blockedbyclient")
                return
            if (
                host in {"localhost", "metadata.google.internal"}
                or host.endswith(".localhost")
                or host.endswith(".internal")
            ):
                route.abort("blockedbyclient")
                return
            port = parsed.port or (443 if scheme == "https" else 80)
            if port not in {80, 443}:
                route.abort("blockedbyclient")
                return

            now = _time.monotonic()
            cached_time = validated_host_cache.get(host)
            if cached_time is not None and (now - cached_time) < 60.0:
                if hasattr(route, "fallback"):
                    route.fallback()
                else:
                    route.continue_()
                return

            validate_url(request_url)
            validated_host_cache[host] = now
        except Exception:
            route.abort("blockedbyclient")
            return

        if hasattr(route, "fallback"):
            route.fallback()
        else:
            route.continue_()

    context.route("**/*", guard)

    # WebSocket handshakes are not HTTP document requests and are not covered by
    # ``route``. This product has no browser-WebSocket use case, so close all
    # WebSockets cleanly with Policy Violation (1008) instead of hanging.
    route_web_socket = getattr(context, "route_web_socket", None)
    if not callable(route_web_socket):
        raise ToolError("installed Playwright lacks required WebSocket interception support")

    def guard_web_socket(web_socket_route: Any) -> None:
        if hasattr(web_socket_route, "close"):
            web_socket_route.close(
                code=1008, reason="Policy Violation: WebSockets disabled by SSRF guard"
            )
        elif hasattr(web_socket_route, "abort"):
            web_socket_route.abort()

    route_web_socket("**/*", guard_web_socket)


def _url(args: dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    try:
        return validate_url(url).url
    except URLSecurityError as exc:
        raise ToolError(f"browser: unsafe URL: {exc}") from exc


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
