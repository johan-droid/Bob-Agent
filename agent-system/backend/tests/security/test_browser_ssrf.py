"""Browser SSRF guards with both unit route mocks and live Playwright verification."""

from __future__ import annotations

import ipaddress
from types import SimpleNamespace
from typing import Any

import pytest

from agent_system.services.tool_errors import ToolError
from agent_system.services.tools.builtin import browser as browser_mod
from agent_system.services.tools.url_security import URLSecurityError, ValidatedURL


class _Route:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.aborted = False
        self.continued = False

    def abort(self, reason: str) -> None:
        assert reason == "blockedbyclient"
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True

    def fallback(self) -> None:
        self.continued = True


class _WebSocketRoute:
    def __init__(self) -> None:
        self.closed = False
        self.code: int | None = None
        self.reason: str | None = None

    def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = True
        self.code = code
        self.reason = reason


class _Context:
    def __init__(self) -> None:
        self.http_guard: Any = None
        self.websocket_guard: Any = None

    def route(self, pattern: str, handler: Any) -> None:
        assert pattern == "**/*"
        self.http_guard = handler

    def route_web_socket(self, pattern: str, handler: Any) -> None:
        assert pattern == "**/*"
        self.websocket_guard = handler


def test_context_request_guard_blocks_private_destinations() -> None:
    context = _Context()
    browser_mod._install_request_guard(context)
    assert context.http_guard is not None
    for target in (
        "http://127.0.0.1:8000/admin",
        "http://localhost:8080/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://metadata.google.internal/",
    ):
        route = _Route(target)
        context.http_guard(route)
        assert route.aborted and not route.continued, f"Target was not aborted: {target}"


def test_context_request_guard_allows_validated_public_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context()
    monkeypatch.setattr(browser_mod, "validate_url", lambda url: object())
    browser_mod._install_request_guard(context)
    assert context.http_guard is not None
    route = _Route("https://public.example/")
    context.http_guard(route)
    assert route.continued and not route.aborted


def test_context_request_guard_fails_closed_on_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context()

    def reject(url: str) -> object:
        raise URLSecurityError("DNS failed")

    monkeypatch.setattr(browser_mod, "validate_url", reject)
    browser_mod._install_request_guard(context)
    assert context.http_guard is not None
    route = _Route("https://public.example/")
    context.http_guard(route)
    assert route.aborted and not route.continued


def test_context_blocks_all_websockets() -> None:
    context = _Context()
    browser_mod._install_request_guard(context)
    assert context.websocket_guard is not None
    route = _WebSocketRoute()
    context.websocket_guard(route)
    assert route.closed
    assert route.code == 1008


@pytest.mark.parametrize("proxy_url", ["", "https://proxy.example", "http://user@proxy.example"])
def test_browser_requires_a_plain_http_proxy(proxy_url: str) -> None:
    with pytest.raises(ToolError, match="proxy"):
        browser_mod._required_proxy_url(SimpleNamespace(browser_proxy_url=proxy_url))


def test_browser_normalises_the_navigation_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        browser_mod,
        "validate_url",
        lambda url: SimpleNamespace(url="https://public.example/normalised"),
    )
    assert (
        browser_mod._url({"url": "https://PUBLIC.example"}) == "https://public.example/normalised"
    )


@pytest.mark.parametrize(
    "invalid_url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://metadata.google.internal/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://example.com:8443/",
    ],
)
def test_browser_url_rejects_unsafe_and_invalid_inputs(invalid_url: str) -> None:
    with pytest.raises(ToolError, match="browser: unsafe URL"):
        browser_mod._url({"url": invalid_url})


# ==============================================================================
# Live Playwright integration tests (headless Chromium)
# ==============================================================================


@pytest.fixture(scope="module")
def live_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright

        p = sync_playwright().start()
        yield p
        p.stop()
    except Exception:
        pytest.skip("Playwright unavailable in environment")


def test_live_browser_blocks_localhost_navigation(live_playwright: Any) -> None:
    browser = live_playwright.chromium.launch(headless=True)
    context = browser.new_context(service_workers="block")
    browser_mod._install_request_guard(context)
    page = context.new_page()

    aborted = False
    try:
        page.goto("http://127.0.0.1:9999/admin", timeout=3000)
    except Exception as exc:
        aborted = True
        assert "ERR_BLOCKED_BY_CLIENT" in str(exc)
    assert aborted, "Localhost navigation was not blocked"
    browser.close()


def test_live_browser_blocks_redirect_to_localhost(live_playwright: Any) -> None:
    """Proves end-to-end that Chromium redirects to localhost are blocked by the outbound proxy."""
    import asyncio
    import threading
    import time

    from agent_system.services.browser_proxy import OutboundProxy

    proxy = OutboundProxy(auth_token="testtoken")
    loop = asyncio.new_event_loop()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(proxy.start(host="127.0.0.1", port=0))
        loop.run_forever()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    time.sleep(0.3)
    proxy_port = proxy.port

    try:
        browser = live_playwright.chromium.launch(
            headless=True,
            proxy={
                "server": f"http://127.0.0.1:{proxy_port}",
                "username": "bob",
                "password": "testtoken",
            },
            args=["--disable-quic", "--proxy-bypass-list=<-loopback>"],
        )
        context = browser.new_context()

        def handle_redirect(route: Any) -> None:
            route.fulfill(status=302, headers={"Location": "http://127.0.0.1:8000/internal"})

        context.route("https://mock-public.example/redirect", handle_redirect)
        page = context.new_page()

        resp = page.goto("https://mock-public.example/redirect", timeout=5000)
        assert resp is not None
        assert resp.status == 403, (
            "Outbound proxy must reject loopback redirect target with 403 Forbidden"
        )
        browser.close()
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_live_browser_blocks_redirect_to_private_ip(live_playwright: Any) -> None:
    """Proves end-to-end that Chromium redirects to RFC 1918 targets are blocked by proxy."""
    import asyncio
    import threading
    import time

    from agent_system.services.browser_proxy import OutboundProxy

    proxy = OutboundProxy(auth_token="testtoken")
    loop = asyncio.new_event_loop()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(proxy.start(host="127.0.0.1", port=0))
        loop.run_forever()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    time.sleep(0.3)
    proxy_port = proxy.port

    try:
        browser = live_playwright.chromium.launch(
            headless=True,
            proxy={
                "server": f"http://127.0.0.1:{proxy_port}",
                "username": "bob",
                "password": "testtoken",
            },
            args=["--disable-quic", "--proxy-bypass-list=<-loopback>"],
        )
        context = browser.new_context()

        def handle_redirect(route: Any) -> None:
            route.fulfill(status=302, headers={"Location": "http://10.0.0.1/admin"})

        context.route("https://mock-public.example/redirect-private", handle_redirect)
        page = context.new_page()

        resp = page.goto("https://mock-public.example/redirect-private", timeout=5000)
        assert resp is not None
        assert resp.status == 403, (
            "Outbound proxy must reject RFC 1918 redirect target with 403 Forbidden"
        )
        browser.close()
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_live_browser_blocks_client_side_redirect(
    live_playwright: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer 2 test: verifies request guard blocks client-side JS redirects to loopback."""
    browser = live_playwright.chromium.launch(headless=True)
    context = browser.new_context(service_workers="block")

    redirect_served = False

    def handle_page(route: Any) -> None:
        nonlocal redirect_served
        redirect_served = True
        html = "<html><body><script>window.location.href='http://127.0.0.1/internal';</script></body></html>"
        route.fulfill(status=200, body=html, headers={"content-type": "text/html"})

    context.route("https://public.example/*", handle_page)

    validated_urls: list[str] = []

    def selective_validate(url: str) -> ValidatedURL:
        validated_urls.append(url)
        if "127.0.0.1" in url or "localhost" in url:
            raise URLSecurityError("blocked loopback target")
        return ValidatedURL(url, "public.example", 443, (ipaddress.ip_address("93.184.216.34"),))

    monkeypatch.setattr(browser_mod, "validate_url", selective_validate)
    browser_mod._install_request_guard(context)
    page = context.new_page()

    page.goto("https://public.example/redirect", timeout=3000)
    page.wait_for_timeout(500)

    assert redirect_served, "Redirect page was never served"
    assert any("127.0.0.1" in u for u in validated_urls), (
        "Loopback redirect target was not validated and guarded"
    )
    assert page.url != "http://127.0.0.1/internal"
    browser.close()


def test_live_browser_blocks_subresources_iframes_and_fetch(live_playwright: Any) -> None:
    browser = live_playwright.chromium.launch(headless=True)
    context = browser.new_context(service_workers="block")
    browser_mod._install_request_guard(context)
    page = context.new_page()

    page.set_content("""
        <html>
        <body>
            <h1>Subresource Test</h1>
            <iframe id='test-frame' src='http://127.0.0.1:8000/iframe'></iframe>
            <img id='test-img' src='http://10.0.0.1/test.png'>
            <script>
                window.fetchError = null;
                fetch('http://169.254.169.254/latest/meta-data/').catch(e => {
                    window.fetchError = e.message;
                });
            </script>
        </body>
        </html>
    """)
    page.wait_for_timeout(1000)
    fetch_error = page.evaluate("window.fetchError")
    assert fetch_error is not None, "Internal fetch was not blocked"
    browser.close()


def test_live_browser_blocks_popup_bypass(live_playwright: Any) -> None:
    browser = live_playwright.chromium.launch(headless=True)
    context = browser.new_context(service_workers="block")
    browser_mod._install_request_guard(context)
    page = context.new_page()

    page.set_content("""
        <html>
        <body>
            <a id='popup-link' href='http://127.0.0.1:8000/admin' target='_blank'>Popup</a>
        </body>
        </html>
    """)
    with context.expect_page() as new_page_info:
        page.click("#popup-link")
    popup = new_page_info.value
    popup.wait_for_load_state("domcontentloaded")
    assert popup.url in {"about:blank", "chrome-error://chromewebdata/"}
    browser.close()


def test_live_browser_allows_approved_public_navigation(
    live_playwright: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = live_playwright.chromium.launch(headless=True)
    context = browser.new_context(service_workers="block")

    # Mock server response for public site so no real external DNS is needed in tests
    def handle_public(route: Any) -> None:
        html = "<html><head><title>Public Site</title></head><body>Public OK</body></html>"
        route.fulfill(status=200, body=html)

    context.route("https://public.example/*", handle_public)

    validated_urls: list[str] = []

    def custom_validate(url: str) -> ValidatedURL:
        validated_urls.append(url)
        return ValidatedURL(url, "public.example", 443, (ipaddress.ip_address("93.184.216.34"),))

    monkeypatch.setattr(browser_mod, "validate_url", custom_validate)
    browser_mod._install_request_guard(context)
    page = context.new_page()

    page.goto("https://public.example/page")
    assert page.title() == "Public Site"
    assert "Public OK" in page.content()
    assert any("public.example" in u for u in validated_urls)
    browser.close()


def test_context_request_guard_reuses_host_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _Context()
    call_count = 0

    def mock_validate(url: str) -> ValidatedURL:
        nonlocal call_count
        call_count += 1
        return ValidatedURL(url, "public.example", 443, (ipaddress.ip_address("93.184.216.34"),))

    monkeypatch.setattr(browser_mod, "validate_url", mock_validate)
    browser_mod._install_request_guard(context)
    assert context.http_guard is not None

    route1 = _Route("https://public.example/page.html")
    context.http_guard(route1)
    assert route1.continued and not route1.aborted
    assert call_count == 1

    route2 = _Route("https://public.example/style.css")
    context.http_guard(route2)
    assert route2.continued and not route2.aborted
    assert call_count == 1


def test_live_browser_blocks_websockets_without_hanging(live_playwright: Any) -> None:
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def accept_loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
                conn.close()
            except Exception:
                break

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()

    try:
        browser = live_playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        browser_mod._install_request_guard(context)
        page = context.new_page()
        page.set_content("<html><body><h1>WS Test</h1></body></html>")
        result = page.evaluate(f"""
            new Promise((resolve) => {{
                try {{
                    const ws = new WebSocket("ws://127.0.0.1:{port}");
                    ws.onclose = (event) => resolve({{
                        status: "closed",
                        code: event.code,
                        reason: event.reason
                    }});
                    ws.onerror = () => resolve({{status: "error"}});
                    setTimeout(() => resolve({{status: "timeout"}}), 1500);
                }} catch (e) {{
                    resolve({{status: "exception", error: e.message}});
                }}
            }})
        """)
        assert result["status"] == "closed"
        assert result.get("code") == 1008
        assert "Policy Violation" in result.get("reason", "")
        assert result["status"] != "timeout"
        browser.close()
    finally:
        srv.close()


def test_research_safe_request_blocks_private_targets() -> None:
    from agent_system.services.tools.builtin import research as research_mod

    for bad_url in (
        "http://127.0.0.1:8000/secret",
        "http://localhost:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://[::1]/",
    ):
        with pytest.raises(ToolError, match="unsafe fetch target"):
            research_mod._safe_request(bad_url)


def test_research_agent_fetch_applies_guard_and_pins_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_system.agents.browser_research import ResearchAgent

    agent = ResearchAgent()
    guarded_urls: list[str] = []

    def mock_guard(url: str) -> ValidatedURL:
        guarded_urls.append(url)
        if "169.254" in url or "127.0.0.1" in url:
            raise ValueError(f"blocked SSRF target: {url}")
        return ValidatedURL(url, "public.example", 443, (ipaddress.ip_address("93.184.216.34"),))

    with pytest.raises(ValueError, match="blocked SSRF target"):
        agent.fetch("http://127.0.0.1:8000/info", guard=mock_guard)

    class MockRedirectClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> MockRedirectClient:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def build_request(self, method: str, url: str) -> Any:
            return httpx.Request(method, url)

        def send(self, request: Any, stream: bool = False) -> Any:
            return httpx.Response(
                302,
                headers={"location": "http://169.254.169.254/latest/meta-data/"},
                request=request,
            )

    import httpx

    monkeypatch.setattr(httpx, "Client", MockRedirectClient)

    with pytest.raises(ValueError, match="blocked SSRF target"):
        agent.fetch("https://public.example/redirect", guard=mock_guard)
