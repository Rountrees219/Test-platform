#!/usr/bin/env python3
"""
Browser Interface — Playwright-based browser automation for agents.

Python API:
    from browser.browser_interface import BrowserInterface
    with BrowserInterface() as b:
        b.goto("https://example.com")
        print(b.title)
        b.screenshot("page.png")
        b.fill("input#q", "hello")
        b.click("button[type=submit]")
        b.wait_for("div.results")

CLI:
    python browser_interface.py goto "https://example.com"
    python browser_interface.py screenshot out.png --url "https://example.com"
    python browser_interface.py text "h1" --url "https://example.com"
    python browser_interface.py pdf out.pdf --url "https://example.com"

VNC (human takeover):
    Headed browsers display on Xvfb :99 → x11vnc :5901 → noVNC :6080
"""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Dict, List, Optional


# Stealth: anti-bot detection evasion (applied automatically)
@cache
def _get_stealth_js() -> str:
    """Load stealth JS once, cached. Returns empty string if unavailable."""
    try:
        from browser.stealth import STEALTH_JS

        return STEALTH_JS
    except ImportError:
        return ""


if not os.environ.get("DISPLAY"):
    os.environ["DISPLAY"] = ":99"

try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
except ImportError:
    sys.exit("Error: pip install playwright && playwright install chromium")


@dataclass
class ConsoleEntry:
    """A captured browser console message."""

    type: str  # "log", "error", "warning", "info", "debug", "trace"
    text: str  # message text
    url: str = ""  # source URL
    line: int = 0  # source line number


@dataclass
class NetworkError:
    """A failed or errored network request."""

    url: str
    method: str = "GET"
    status: int = 0  # HTTP status (0 = connection failed)
    status_text: str = ""
    resource_type: str = ""
    failure: str = ""  # failure reason (e.g. "net::ERR_CONNECTION_REFUSED")


@dataclass
class PageError:
    """An uncaught JavaScript error on the page."""

    message: str
    name: str = ""
    stack: str = ""


@dataclass
class DevTools:
    """Collected developer tools data from the browser session.

    Access via browser.devtools after navigating to a page.
    All lists accumulate across the session — call browser.clear_devtools() to reset.
    """

    console: List[ConsoleEntry] = field(default_factory=list)
    errors: List[PageError] = field(default_factory=list)
    network_errors: List[NetworkError] = field(default_factory=list)

    @property
    def console_errors(self) -> List[ConsoleEntry]:
        """Only console.error messages."""
        return [c for c in self.console if c.type == "error"]

    @property
    def console_warnings(self) -> List[ConsoleEntry]:
        """Only console.warn messages."""
        return [c for c in self.console if c.type == "warning"]

    @property
    def has_errors(self) -> bool:
        """True if there are any JS errors, console errors, or network failures."""
        return bool(self.errors or self.console_errors or self.network_errors)

    def summary(self) -> Dict:
        """Summary dict for CLI/JSON output."""
        return {
            "js_errors": len(self.errors),
            "console_errors": len(self.console_errors),
            "console_warnings": len(self.console_warnings),
            "console_logs": len(self.console),
            "network_errors": len(self.network_errors),
            "has_errors": self.has_errors,
        }

    def to_dict(self) -> Dict:
        """Full devtools data as dict."""
        return {
            "summary": self.summary(),
            "js_errors": [asdict(e) for e in self.errors],
            "console_errors": [asdict(c) for c in self.console_errors],
            "console_warnings": [asdict(c) for c in self.console_warnings],
            "network_errors": [asdict(n) for n in self.network_errors],
            "console_all": [asdict(c) for c in self.console],
        }

    def format_report(self) -> str:
        """Human-readable error report for CLI output."""
        lines = []
        s = self.summary()
        if not self.has_errors and not self.console_warnings:
            lines.append("✅ No errors detected")
            lines.append(f"   Console: {s['console_logs']} messages")
            return "\n".join(lines)

        if self.errors:
            lines.append(f"❌ JS Errors ({len(self.errors)}):")
            for e in self.errors:
                lines.append(f"   • {e.message}")
                if e.stack:
                    for sl in e.stack.strip().split("\n")[:3]:
                        lines.append(f"     {sl.strip()}")

        if self.console_errors:
            lines.append(f"❌ Console Errors ({len(self.console_errors)}):")
            for c in self.console_errors:
                loc = f" ({c.url}:{c.line})" if c.url else ""
                lines.append(f"   • {c.text}{loc}")

        if self.network_errors:
            lines.append(f"❌ Network Errors ({len(self.network_errors)}):")
            for n in self.network_errors:
                if n.status:
                    lines.append(
                        f"   • {n.method} {n.url} → {n.status} {n.status_text}"
                    )
                else:
                    lines.append(f"   • {n.method} {n.url} → FAILED: {n.failure}")

        if self.console_warnings:
            lines.append(f"⚠️  Console Warnings ({len(self.console_warnings)}):")
            for c in self.console_warnings:
                lines.append(f"   • {c.text}")

        return "\n".join(lines)


class BrowserInterface:
    """High-level Playwright wrapper with devtools capture.

    Use as context manager or call start()/stop().
    Console logs, JS errors, and network failures are captured automatically
    and available via the .devtools property.

    Persistence:
        Pass user_data_dir to persist cookies, cache, localStorage, and login
        sessions across runs. Uses Playwright's launch_persistent_context().
        Without user_data_dir, each session starts with a fresh ephemeral browser.
    """

    def __init__(
        self,
        headless=False,
        viewport_width=1600,
        viewport_height=900,
        timeout=30000,
        slow_mo=0,
        user_agent=None,
        capture_console=True,
        user_data_dir=None,
        proxy=None,
    ):
        """
        Args:
            headless: False = visible on VNC (default). True = no display.
            viewport_width/height: Browser viewport size (default 1600x900).
            timeout: Default timeout for all operations in ms (default 30000).
            slow_mo: Delay between actions in ms, useful for VNC demos (default 0).
            user_agent: Custom User-Agent string (optional).
            capture_console: Auto-capture console logs, errors, and network failures (default True).
            user_data_dir: Path to browser profile directory for persistent sessions.
                           Cookies, cache, localStorage, and login state are preserved
                           across runs. If None, uses a fresh ephemeral browser each time.
            proxy: Proxy server URL (e.g. "http://proxy:8080"). Optional.
        """
        self._headless = headless
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._timeout = timeout
        self._slow_mo = slow_mo
        self._user_agent = user_agent
        self._capture_console = capture_console
        self._user_data_dir = user_data_dir
        self._proxy = proxy
        self._persistent = False  # True when using launch_persistent_context
        self._cdp = False  # True when connected via connect_cdp()
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._started = False
        self.devtools = DevTools()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop()
        return False

    @classmethod
    def connect_cdp(
        cls,
        endpoint="http://localhost:9222",
        viewport_width=1600,
        viewport_height=900,
        timeout=30000,
        capture_console=True,
    ):
        """Connect to an already-running Chromium via Chrome DevTools Protocol.

        This is the preferred way to use the browser in Ninja tasks.
        The browser process persists across tasks -- tabs, cookies, and state
        are preserved between runs.

        Args:
            endpoint: CDP endpoint URL (default http://localhost:9222).
            viewport_width/height: Viewport size for new pages.
            timeout: Default timeout in ms.
            capture_console: Capture console/network events.

        Returns:
            A BrowserInterface instance connected to the running browser.

        Raises:
            ConnectionError: If no browser is running on the endpoint.
        """
        import urllib.request

        # Quick health check
        try:
            urllib.request.urlopen(f"{endpoint}/json/version", timeout=3)
        except Exception as e:
            raise ConnectionError(
                f"No browser found at {endpoint}. "
                f"Start one with: python ninja/browser_server.py start"
            ) from e

        inst = cls.__new__(cls)
        inst._headless = False
        inst._viewport = {"width": viewport_width, "height": viewport_height}
        inst._timeout = timeout
        inst._slow_mo = 0
        inst._user_agent = None
        inst._capture_console = capture_console
        inst._user_data_dir = None
        inst._proxy = None
        inst._persistent = False
        inst._cdp = True  # Flag: connected via CDP, don't kill browser on stop()
        inst.devtools = DevTools()

        inst._playwright = sync_playwright().start()
        inst.browser = inst._playwright.chromium.connect_over_cdp(endpoint)

        # Reuse existing context/page if available, else create new
        contexts = inst.browser.contexts
        if contexts:
            inst.context = contexts[0]
            if inst.context.pages:
                inst.page = inst.context.pages[-1]
            else:
                inst.page = inst.context.new_page()
        else:
            inst.context = inst.browser.new_context(viewport=inst._viewport)
            inst.context.set_default_timeout(timeout)
            inst.page = inst.context.new_page()

        if capture_console:
            inst._attach_devtools_listeners()
        inst._apply_stealth()
        inst._started = True
        return inst

    def start(self):
        """Launch Chromium and create a page. Hooks up devtools listeners.

        If user_data_dir was provided, uses launch_persistent_context() which
        preserves cookies, cache, and login state between runs. Otherwise
        launches a fresh ephemeral browser.
        """
        if self._started:
            raise RuntimeError("Already started. Call stop() first.")
        self._playwright = sync_playwright().start()

        # Shared context args
        ctx_args = {"viewport": self._viewport}
        if self._user_agent:
            ctx_args["user_agent"] = self._user_agent
        if self._proxy:
            ctx_args["proxy"] = {"server": self._proxy}

        if self._user_data_dir:
            # --- Persistent mode ---
            # launch_persistent_context returns a BrowserContext directly
            # (no separate Browser object). Profile data is saved to disk.
            Path(self._user_data_dir).mkdir(parents=True, exist_ok=True)
            self.context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=self._user_data_dir,
                headless=self._headless,
                slow_mo=self._slow_mo,
                **ctx_args,
            )
            self.browser = None  # no separate browser in persistent mode
            self._persistent = True
        else:
            # --- Ephemeral mode (original behavior) ---
            # Proxy goes on launch() for ephemeral mode; remove from ctx_args
            # to avoid passing it twice (launch + new_context)
            launch_args = {}
            if self._proxy:
                launch_args["proxy"] = {"server": self._proxy}
                ctx_args.pop("proxy", None)
            self.browser = self._playwright.chromium.launch(
                headless=self._headless,
                slow_mo=self._slow_mo,
                **launch_args,
            )
            self.context = self.browser.new_context(**ctx_args)
            self._persistent = False

        self.context.set_default_timeout(self._timeout)
        # Reuse existing blank tab if persistent context opened one, else create
        if self.context.pages:
            self.page = self.context.pages[-1]
        else:
            self.page = self.context.new_page()
        self.devtools = DevTools()
        if self._capture_console:
            self._attach_devtools_listeners()
        self._apply_stealth()
        self._started = True

    def _apply_stealth(self):
        """Inject stealth anti-detection patches into the browser context.

        Uses context.add_init_script() so patches run automatically on every
        page load and new tab — no need to re-apply manually.
        """
        js = _get_stealth_js()
        if not js:
            return
        try:
            self.context.add_init_script(js)
            # Also apply to current page immediately
            try:
                self.page.evaluate(js)
            except Exception:
                pass  # Page might not be ready yet
        except Exception:
            pass  # Non-fatal: stealth is best-effort

    def check_stealth(self) -> dict:
        """Check if stealth patches are active on the current page.

        Returns dict with: webdriver, webdriverType, chromeRuntime, plugins, languages.
        """
        from browser.stealth import check_stealth

        return check_stealth(self)

    def check_session(self, service: str = "google") -> dict:
        """Check if browser has valid session cookies for a service.

        Args:
            service: Service name — "google", "linkedin", "twitter",
                     "github", "amazon", "facebook".

        Returns dict with: valid (bool), cookies_found (list), login_url (str), etc.
        """
        from browser.session_health import check_session

        return check_session(service)

    def session_status(self) -> dict:
        """Check session health for all configured services.

        Returns dict mapping service name → check result.
        """
        from browser.session_health import check_all_sessions

        return check_all_sessions()

    def vnc_url(self) -> str:
        """Get the VNC URL for manual browser login."""
        from browser.session_health import get_vnc_url

        return get_vnc_url()

    def _attach_devtools_listeners(self):
        """Attach console, error, and network listeners to current page."""
        page = self.page

        # Capture console messages (log, error, warn, info, debug, etc.)
        def on_console(msg):
            location = msg.location
            self.devtools.console.append(
                ConsoleEntry(
                    type=msg.type,
                    text=msg.text,
                    url=location.get("url", "") if location else "",
                    line=location.get("lineNumber", 0) if location else 0,
                )
            )

        page.on("console", on_console)

        # Capture uncaught JS errors (window.onerror / unhandled rejections)
        def on_page_error(error):
            self.devtools.errors.append(
                PageError(
                    message=str(error),
                    name=getattr(error, "name", "Error"),
                    stack=getattr(error, "stack", ""),
                )
            )

        page.on("pageerror", on_page_error)

        # Capture failed network requests (connection errors, DNS failures)
        def on_request_failed(request):
            self.devtools.network_errors.append(
                NetworkError(
                    url=request.url,
                    method=request.method,
                    resource_type=request.resource_type,
                    failure=request.failure or "Unknown failure",
                )
            )

        page.on("requestfailed", on_request_failed)

        # Capture HTTP error responses (4xx, 5xx)
        def on_response(response):
            if response.status >= 400:
                self.devtools.network_errors.append(
                    NetworkError(
                        url=response.url,
                        method=response.request.method,
                        status=response.status,
                        status_text=response.status_text,
                        resource_type=response.request.resource_type,
                    )
                )

        page.on("response", on_response)

    def stop(self):
        """Close browser. Safe to call multiple times. DevTools data preserved.

        In persistent mode, closing the context saves profile data to disk.
        In CDP mode, only disconnects — the browser process keeps running.
        """
        if not self._started:
            return
        try:
            if getattr(self, "_cdp", False):
                # CDP mode: just disconnect, don't kill the browser
                # The browser process is managed by browser_server.py
                pass
            elif self._persistent:
                # Persistent mode: context IS the browser, closing it saves state
                if self.context:
                    self.context.close()
            else:
                # Ephemeral mode: close context then browser
                if self.context:
                    self.context.close()
                if self.browser:
                    self.browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        finally:
            self.page = self.context = self.browser = self._playwright = None
            self._started = False
            self._persistent = False

    def clear_devtools(self):
        """Clear all captured devtools data (console, errors, network)."""
        self.devtools = DevTools()

    def _ok(self):
        if not self._started or not self.page:
            raise RuntimeError(
                "Browser not started. Use context manager or call start()."
            )

    # --- Navigation ---

    def goto(self, url, wait_until="load", timeout=None):
        """Navigate to URL. Returns {"url", "title", "status"}.
        wait_until: "load" | "domcontentloaded" | "networkidle" | "commit"
        """
        self._ok()
        kw = {"wait_until": wait_until}
        if timeout:
            kw["timeout"] = timeout
        r = self.page.goto(url, **kw)
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "status": r.status if r else None,
        }

    def reload(self, wait_until="load"):
        """Reload page. Returns {"url", "title", "status"}."""
        self._ok()
        r = self.page.reload(wait_until=wait_until)
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "status": r.status if r else None,
        }

    def go_back(self):
        """Navigate back."""
        self._ok()
        r = self.page.go_back()
        return {"url": self.page.url, "title": self.page.title()} if r else None

    def go_forward(self):
        """Navigate forward."""
        self._ok()
        r = self.page.go_forward()
        return {"url": self.page.url, "title": self.page.title()} if r else None

    # --- Properties ---

    @property
    def title(self):
        """Page title."""
        self._ok()
        return self.page.title()

    @property
    def url(self):
        """Page URL."""
        self._ok()
        return self.page.url

    @property
    def content(self):
        """Full page HTML."""
        self._ok()
        return self.page.content()

    # --- Interaction ---

    def click(self, selector, timeout=None, **kw):
        """Click element. Selector: CSS, "text=...", or Playwright locator."""
        self._ok()
        if timeout:
            kw["timeout"] = timeout
        self.page.click(selector, **kw)

    def double_click(self, selector, **kw):
        """Double-click element."""
        self._ok()
        self.page.dblclick(selector, **kw)

    def right_click(self, selector, **kw):
        """Right-click element."""
        self._ok()
        self.page.click(selector, button="right", **kw)

    def hover(self, selector, **kw):
        """Hover over element."""
        self._ok()
        self.page.hover(selector, **kw)

    def fill(self, selector, value, **kw):
        """Fill input (clears first). Works on input, textarea, contenteditable."""
        self._ok()
        self.page.fill(selector, value, **kw)

    def type_text(self, selector, text, delay=0, **kw):
        """Type character-by-character. delay=ms between keys."""
        self._ok()
        self.page.type(selector, text, delay=delay, **kw)

    def press(self, selector, key, **kw):
        """Press key on element. key: 'Enter', 'Tab', 'Escape', 'ArrowDown', etc."""
        self._ok()
        self.page.press(selector, key, **kw)

    def select_option(self, selector, value=None, label=None, index=None, **kw):
        """Select <option> by value, label, or index. Returns selected values."""
        self._ok()
        opts = {}
        if value is not None:
            opts["value"] = value
        if label is not None:
            opts["label"] = label
        if index is not None:
            opts["index"] = index
        return self.page.select_option(selector, **opts, **kw)

    def check(self, selector, **kw):
        """Check checkbox/radio."""
        self._ok()
        self.page.check(selector, **kw)

    def uncheck(self, selector, **kw):
        """Uncheck checkbox."""
        self._ok()
        self.page.uncheck(selector, **kw)

    # --- Content Extraction ---

    def text(self, selector="body"):
        """Get visible text of element (default: entire page)."""
        self._ok()
        return self.page.inner_text(selector)

    def html(self, selector="body"):
        """Get inner HTML of element."""
        self._ok()
        return self.page.inner_html(selector)

    def attribute(self, selector, name):
        """Get element attribute. e.g. attribute('a', 'href')"""
        self._ok()
        return self.page.get_attribute(selector, name)

    def query_all(self, selector):
        """Count matching elements."""
        self._ok()
        return len(self.page.query_selector_all(selector))

    def query_texts(self, selector):
        """Get text of ALL matching elements. Returns list of strings."""
        self._ok()
        return [el.inner_text() for el in self.page.query_selector_all(selector)]

    def evaluate(self, js):
        """Execute JavaScript and return result."""
        self._ok()
        return self.page.evaluate(js)

    # --- Screenshots & PDF ---

    def screenshot(
        self, path="screenshot.png", full_page=False, selector=None, quality=None
    ):
        """Take screenshot. Returns absolute path.
        full_page=True captures entire scrollable page.
        selector: screenshot a specific element.
        quality: JPEG quality 0-100 (only for .jpg).
        """
        self._ok()
        kw = {"path": path, "full_page": full_page}
        if quality is not None:
            kw["quality"] = quality
        if selector:
            el = self.page.query_selector(selector)
            if not el:
                raise ValueError(f"Element not found: {selector}")
            el.screenshot(path=path)
        else:
            self.page.screenshot(**kw)
        return str(Path(path).resolve())

    def pdf(self, path="page.pdf", format="A4", print_background=True):
        """Save page as PDF. In headed mode, uses a temp headless browser."""
        self._ok()
        if self._headless:
            self.page.pdf(path=path, format=format, print_background=print_background)
        else:
            url = self.page.url
            tb = self._playwright.chromium.launch(headless=True)
            tp = tb.new_page()
            tp.goto(url, wait_until="networkidle")
            tp.pdf(path=path, format=format, print_background=print_background)
            tb.close()
        return str(Path(path).resolve())

    # --- Waiting ---

    def wait_for(self, selector, state="visible", timeout=None):
        """Wait for element. state: 'visible'|'hidden'|'attached'|'detached'."""
        self._ok()
        kw = {"state": state}
        if timeout:
            kw["timeout"] = timeout
        self.page.wait_for_selector(selector, **kw)

    def wait_for_url(self, pattern, timeout=None):
        """Wait for URL to match pattern (glob or regex)."""
        self._ok()
        kw = {}
        if timeout:
            kw["timeout"] = timeout
        self.page.wait_for_url(pattern, **kw)

    def wait_for_load(self, state="load", timeout=None):
        """Wait for load state: 'load'|'domcontentloaded'|'networkidle'."""
        self._ok()
        kw = {}
        if timeout:
            kw["timeout"] = timeout
        self.page.wait_for_load_state(state, **kw)

    def sleep(self, seconds):
        """Sleep for N seconds."""
        time.sleep(seconds)

    # --- Tabs ---

    def new_tab(self, url=None):
        """Open new tab, optionally navigate to URL. Attaches devtools listeners + stealth."""
        self._ok()
        self.page = self.context.new_page()
        if self._capture_console:
            self._attach_devtools_listeners()
        # Stealth init_script runs automatically via context, but apply to current doc too
        js = _get_stealth_js()
        if js:
            try:
                self.page.evaluate(js)
            except Exception:
                pass
        if url:
            self.page.goto(url)

    def close_tab(self):
        """Close current tab, switch to last remaining."""
        self._ok()
        if len(self.context.pages) <= 1:
            raise RuntimeError("Cannot close last tab. Use stop().")
        self.page.close()
        self.page = self.context.pages[-1]

    @property
    def tab_count(self):
        """Number of open tabs."""
        self._ok()
        return len(self.context.pages)

    # --- Scroll ---

    def scroll_down(self, px=500):
        """Scroll down by pixels."""
        self._ok()
        self.page.evaluate(f"window.scrollBy(0,{px})")

    def scroll_up(self, px=500):
        """Scroll up by pixels."""
        self._ok()
        self.page.evaluate(f"window.scrollBy(0,-{px})")

    def scroll_to_top(self):
        """Scroll to top of page."""
        self._ok()
        self.page.evaluate("window.scrollTo(0,0)")

    def scroll_to_bottom(self):
        """Scroll to bottom of page."""
        self._ok()
        self.page.evaluate("window.scrollTo(0,document.body.scrollHeight)")

    def scroll_to(self, selector):
        """Scroll element into view."""
        self._ok()
        el = self.page.query_selector(selector)
        if el:
            el.scroll_into_view_if_needed()

    # --- Cookies & Storage ---

    def cookies(self):
        """Get all cookies. Returns list of cookie dicts."""
        self._ok()
        return self.context.cookies()

    def set_cookie(self, name, value, url=None, domain=None, path=None):
        """Set a cookie. Provide either url OR domain+path, not both."""
        self._ok()
        cookie = {"name": name, "value": value}
        if url:
            cookie["url"] = url
        else:
            if domain:
                cookie["domain"] = domain
            cookie["path"] = path or "/"
        self.context.add_cookies([cookie])

    def clear_cookies(self):
        """Clear all cookies."""
        self._ok()
        self.context.clear_cookies()

    def local_storage(self, key=None):
        """Get localStorage. If key provided, get single value. Else get all as dict."""
        self._ok()
        if key:
            return self.page.evaluate(f"localStorage.getItem({json.dumps(key)})")
        return self.page.evaluate("Object.fromEntries(Object.entries(localStorage))")

    # --- Network ---

    def block_resources(self, types=None):
        """Block resource types. types: list of 'image','stylesheet','font','script','media'.
        Call before navigating."""
        self._ok()
        if types is None:
            types = ["image", "stylesheet", "font"]

        def handle(route):
            if route.request.resource_type in types:
                route.abort()
            else:
                route.continue_()

        self.page.route("**/*", handle)

    def intercept_requests(self, callback):
        """Set a request interceptor. callback(route, request) called for every request."""
        self._ok()
        self.page.route("**/*", lambda route: callback(route, route.request))

    # --- DevTools Access ---

    def console_logs(self, type_filter=None):
        """Get captured console messages. Optionally filter by type.
        type_filter: "log", "error", "warning", "info", "debug", or None for all.
        Returns list of ConsoleEntry.
        """
        if type_filter:
            return [c for c in self.devtools.console if c.type == type_filter]
        return list(self.devtools.console)

    def js_errors(self):
        """Get captured uncaught JavaScript errors. Returns list of PageError."""
        return list(self.devtools.errors)

    def network_errors(self):
        """Get captured network failures (connection errors + HTTP 4xx/5xx).
        Returns list of NetworkError."""
        return list(self.devtools.network_errors)

    def error_report(self):
        """Get a human-readable error report string."""
        return self.devtools.format_report()

    def assert_no_errors(self):
        """Raise AssertionError if there are any JS errors, console errors, or network failures.
        Useful for QA testing — call after page interactions to verify no errors occurred.
        """
        if self.devtools.has_errors:
            raise AssertionError(
                f"Browser errors detected:\n{self.devtools.format_report()}"
            )


# ============================================================================
# CLI Interface
# ============================================================================


def _print_devtools(b, show_json=False):
    """Print devtools report after CLI commands."""
    if show_json:
        print(json.dumps(b.devtools.to_dict(), indent=2))
    else:
        report = b.devtools.format_report()
        if report:
            print(f"\n--- DevTools ---\n{report}")


def main():
    parser = argparse.ArgumentParser(
        description="Browser automation CLI with devtools error capture",
        epilog="Examples:\n"
        "  %(prog)s goto https://example.com\n"
        "  %(prog)s screenshot page.png --url https://example.com\n"
        "  %(prog)s text h1 --url https://example.com\n"
        "  %(prog)s pdf report.pdf --url https://example.com\n"
        "  %(prog)s check https://example.com  (check for JS/network errors)\n"
        "  %(prog)s console https://example.com (show all console output)\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Global flags
    parser.add_argument(
        "--no-devtools", action="store_true", help="Suppress devtools error report"
    )
    parser.add_argument(
        "--devtools-json",
        action="store_true",
        help="Output devtools data as JSON instead of human-readable",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # goto
    p = sub.add_parser("goto", help="Navigate to URL and print page info + errors")
    p.add_argument("url", help="URL to navigate to")
    p.add_argument("--headless", action="store_true", help="Run headless")
    p.add_argument(
        "--wait",
        default="load",
        choices=["load", "domcontentloaded", "networkidle", "commit"],
    )

    # screenshot
    p = sub.add_parser("screenshot", help="Take a screenshot")
    p.add_argument("path", help="Output file path (.png or .jpg)")
    p.add_argument("--url", help="URL to navigate to first")
    p.add_argument("--full-page", action="store_true")
    p.add_argument("--selector", help="Screenshot a specific element")
    p.add_argument("--headless", action="store_true")

    # text
    p = sub.add_parser("text", help="Extract text from element")
    p.add_argument("selector", nargs="?", default="body", help="CSS selector")
    p.add_argument("--url", help="URL to navigate to first")
    p.add_argument("--headless", action="store_true")

    # pdf
    p = sub.add_parser("pdf", help="Save page as PDF")
    p.add_argument("path", help="Output PDF path")
    p.add_argument("--url", help="URL to navigate to first")
    p.add_argument("--format", default="A4", help="Paper format")

    # html
    p = sub.add_parser("html", help="Get page HTML")
    p.add_argument("--url", help="URL to navigate to first")
    p.add_argument("--selector", default="body")
    p.add_argument("--headless", action="store_true")

    # check — dedicated error checking command
    p = sub.add_parser("check", help="Load URL and report JS/console/network errors")
    p.add_argument("url", help="URL to check")
    p.add_argument("--headless", action="store_true")
    p.add_argument(
        "--wait",
        default="networkidle",
        choices=["load", "domcontentloaded", "networkidle", "commit"],
    )

    # console — show all console output
    p = sub.add_parser("console", help="Load URL and show all console output")
    p.add_argument("url", help="URL to load")
    p.add_argument("--headless", action="store_true")
    p.add_argument(
        "--wait",
        default="networkidle",
        choices=["load", "domcontentloaded", "networkidle", "commit"],
    )

    args = parser.parse_args()
    headless = getattr(args, "headless", True)
    show_devtools = not args.no_devtools
    devtools_json = args.devtools_json

    with BrowserInterface(headless=headless) as b:
        # Navigate if URL provided
        url = getattr(args, "url", None)
        if args.command in ("goto", "check", "console"):
            url = args.url

        if url:
            result = b.goto(url, wait_until=getattr(args, "wait", "load"))
            if args.command == "goto":
                print(json.dumps(result, indent=2))
                if show_devtools:
                    _print_devtools(b, devtools_json)
                sys.exit(1 if b.devtools.has_errors else 0)

        # check — focused error report
        if args.command == "check":
            if devtools_json:
                print(json.dumps(b.devtools.to_dict(), indent=2))
            else:
                print(b.devtools.format_report())
            sys.exit(1 if b.devtools.has_errors else 0)

        # console — show all console messages
        elif args.command == "console":
            if devtools_json:
                print(json.dumps(b.devtools.to_dict(), indent=2))
            else:
                if not b.devtools.console:
                    print("(no console output)")
                else:
                    for entry in b.devtools.console:
                        prefix = {
                            "error": "❌",
                            "warning": "⚠️ ",
                            "info": "ℹ️ ",
                            "debug": "🔧",
                        }.get(entry.type, "  ")
                        loc = f" [{entry.url}:{entry.line}]" if entry.url else ""
                        print(f"{prefix} [{entry.type:>7}] {entry.text}{loc}")
                # Also show errors/network issues
                if b.devtools.errors or b.devtools.network_errors:
                    print()
                    print(b.devtools.format_report())
            sys.exit(1 if b.devtools.has_errors else 0)

        elif args.command == "screenshot":
            path = b.screenshot(
                args.path, full_page=args.full_page, selector=args.selector
            )
            print(f"Screenshot saved: {path}")
            if show_devtools:
                _print_devtools(b, devtools_json)

        elif args.command == "text":
            print(b.text(args.selector))
            if show_devtools and b.devtools.has_errors:
                _print_devtools(b, devtools_json)

        elif args.command == "pdf":
            path = b.pdf(args.path, format=args.format)
            print(f"PDF saved: {path}")

        elif args.command == "html":
            print(b.html(args.selector))
            if show_devtools and b.devtools.has_errors:
                _print_devtools(b, devtools_json)


if __name__ == "__main__":
    main()
