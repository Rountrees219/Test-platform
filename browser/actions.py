"""
Action Executor — Translates LLM action dicts into browser_interface calls.

Features:
- Self-healing selectors: tries multiple selector strategies on failure
- Selector cache: remembers which fallback selectors succeeded per page
- Overlay auto-dismissal: detects and closes cookie banners, popups, modals
- Smart retry for transient Playwright errors (element detached, navigation)
- Detailed result strings for the LLM
"""

import time
from typing import Optional

from browser.browser_interface import BrowserInterface
from browser.config import SCREENSHOTS_DIR

# Store interactive elements from the last observation for self-healing
_last_elements: list[dict] = []

# Selector cache: maps (original_selector -> successful_selector) per page URL.
# Cleared on navigation. Speeds up retries on the same page.
_selector_cache: dict[str, str] = {}
_cache_url: str = ""  # URL when cache was last valid


def set_elements(elements: list[dict]):
    """Store interactive elements from observer for self-healing selector resolution."""
    global _last_elements
    _last_elements = elements


def clear_selector_cache():
    """Clear the selector cache (called on navigation)."""
    global _selector_cache, _cache_url
    _selector_cache = {}
    _cache_url = ""


def _maybe_invalidate_cache(browser: BrowserInterface):
    """Invalidate selector cache if the page URL has changed."""
    global _cache_url
    try:
        current_url = browser.url
    except Exception:
        current_url = ""
    if current_url != _cache_url:
        clear_selector_cache()
        _cache_url = current_url


def _cache_selector(original: str, resolved: str):
    """Record a successful selector resolution."""
    if original != resolved:
        _selector_cache[original] = resolved


def execute_action(browser: BrowserInterface, action: str, params: dict) -> str:
    """
    Execute a browser action and return a result description.

    Uses self-healing selectors: if the primary selector fails,
    tries alternative selectors from the elements list.
    Includes selector caching and smart retry for transient errors.
    """
    action = action.lower().strip()

    # Invalidate selector cache if URL changed since last action
    _maybe_invalidate_cache(browser)

    try:
        if action == "goto":
            url = params.get("url", "")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            clear_selector_cache()  # Navigation invalidates cache
            result = browser.goto(url, wait_until="load")
            return f"Navigated to {result['url']} (title: {result['title']}, status: {result['status']})"

        elif action == "click":
            selector = params.get("selector", "")
            _ensure_visible(browser, selector)
            url_before = _safe_url(browser)
            _click_with_healing(browser, selector)
            _wait_after_click(browser, url_before)
            return f"Clicked: {selector}"

        elif action == "fill":
            selector = params.get("selector", "")
            value = params.get("value", "")
            _ensure_visible(browser, selector)
            _fill_with_healing(browser, selector, value)
            return f"Filled {selector} with '{value}'"

        elif action == "type_text":
            selector = params.get("selector", "")
            text = params.get("text", "")
            delay = params.get("delay", 50)
            resolved = _resolve_selector(selector)
            browser.type_text(resolved, text, delay=delay)
            return f"Typed '{text}' into {selector}"

        elif action == "press":
            key = params.get("key", "Enter")
            selector = params.get("selector")
            if selector:
                browser.press(_resolve_selector(selector), key)
            else:
                browser.page.keyboard.press(key)
            return f"Pressed {key}"

        elif action == "select_option":
            selector = _resolve_selector(params.get("selector", ""))
            value = params.get("value")
            label = params.get("label")
            if label:
                browser.select_option(selector, label=label)
                return f"Selected option '{label}' in {selector}"
            browser.select_option(selector, value=value)
            return f"Selected value '{value}' in {selector}"

        elif action == "check":
            selector = _resolve_selector(params.get("selector", ""))
            browser.check(selector)
            return f"Checked: {selector}"

        elif action == "hover":
            selector = _resolve_selector(params.get("selector", ""))
            browser.hover(selector)
            return f"Hovered over: {selector}"

        elif action == "dismiss_overlay":
            return _dismiss_overlay(browser)

        elif action == "go_back":
            clear_selector_cache()
            browser.go_back()
            return f"Went back to {browser.url}"

        elif action == "go_forward":
            clear_selector_cache()
            browser.go_forward()
            return f"Went forward to {browser.url}"

        elif action == "reload":
            clear_selector_cache()
            browser.reload()
            return f"Reloaded {browser.url}"

        elif action == "scroll_down":
            px = params.get("px", 500)
            browser.scroll_down(px=px)
            return f"Scrolled down {px}px"

        elif action == "scroll_up":
            px = params.get("px", 500)
            browser.scroll_up(px=px)
            return f"Scrolled up {px}px"

        elif action == "scroll_to":
            selector = _resolve_selector(params.get("selector", ""))
            browser.scroll_to(selector)
            return f"Scrolled to: {selector}"

        elif action == "extract_text":
            selector = params.get("selector", "body")
            text = browser.text(selector)
            truncated = text[:2000] if text else "(empty)"
            return f"Text from {selector}: {truncated}"

        elif action == "extract_html":
            selector = params.get("selector", "body")
            html = browser.html(selector)
            truncated = html[:2000] if html else "(empty)"
            return f"HTML from {selector}: {truncated}"

        elif action == "extract_attribute":
            selector = _resolve_selector(params.get("selector", ""))
            attr = params.get("attribute", "href")
            val = browser.attribute(selector, attr)
            return f"Attribute {attr} of {selector}: {val}"

        elif action == "wait":
            seconds = params.get("seconds", 2)
            browser.sleep(seconds)
            return f"Waited {seconds}s"

        elif action == "screenshot":
            filename = params.get("filename", "manual.png")
            path = str(SCREENSHOTS_DIR / filename)
            browser.screenshot(path)
            return f"Screenshot saved to {path}"

        elif action == "done":
            result = params.get("result", "Task completed")
            return f"DONE: {result}"

        elif action == "fail":
            reason = params.get("reason", "Unknown failure")
            return f"FAIL: {reason}"

        elif action == "need_human":
            reason = params.get("reason", "Human intervention needed")
            return f"NEED_HUMAN: {reason}"

        # --- Extended actions ---

        elif action == "save_pdf":
            filename = params.get("filename", "page.pdf")
            path = str(SCREENSHOTS_DIR / filename)
            browser.pdf(path)
            return f"PDF saved to {path}"

        elif action == "scroll_to_top":
            browser.scroll_to_top()
            return "Scrolled to top of page"

        elif action == "scroll_to_bottom":
            browser.scroll_to_bottom()
            return "Scrolled to bottom of page"

        elif action == "wait_for_element":
            selector = _resolve_selector(params.get("selector", ""))
            timeout = params.get("timeout", 10000)
            browser.wait_for(selector, timeout=timeout)
            return f"Element appeared: {selector}"

        elif action == "extract_table":
            selector = params.get("selector", "table")
            safe_sel = selector.replace("'", "\\'")
            table_data = browser.evaluate(f"""
            (() => {{
                const table = document.querySelector('{safe_sel}');
                if (!table) return null;
                const rows = [];
                for (const tr of table.querySelectorAll('tr')) {{
                    const cells = [];
                    for (const td of tr.querySelectorAll('td, th')) {{
                        cells.push(td.innerText.trim());
                    }}
                    if (cells.length > 0) rows.push(cells);
                }}
                return rows;
            }})()
            """)
            if table_data is None:
                return f"No table found matching: {selector}"
            # Format as readable text
            lines = []
            for row in table_data[:50]:  # Cap at 50 rows
                lines.append(" | ".join(str(c) for c in row))
            truncated = "\n".join(lines)[:2000]
            return f"Table from {selector} ({len(table_data)} rows):\n{truncated}"

        elif action == "extract_links":
            selector = params.get("selector", "body")
            safe_sel = selector.replace("'", "\\'")
            links = browser.evaluate(f"""
            (() => {{
                const container = document.querySelector('{safe_sel}') || document.body;
                const anchors = container.querySelectorAll('a[href]');
                return Array.from(anchors).slice(0, 50).map(a => ({{
                    text: a.innerText.trim().substring(0, 100),
                    href: a.href
                }}));
            }})()
            """)
            if not links:
                return f"No links found in: {selector}"
            lines = [f"- [{l['text']}]({l['href']})" for l in links]
            truncated = "\n".join(lines)[:2000]
            return f"Links from {selector} ({len(links)}):\n{truncated}"

        elif action == "execute_js":
            script = params.get("script", "")
            # Wrap in IIFE if it contains return statements (common LLM output)
            if "return " in script and not script.strip().startswith("("):
                script = f"(() => {{ {script} }})()"
            result_val = browser.evaluate(script)
            result_str = (
                str(result_val)[:2000] if result_val is not None else "(undefined)"
            )
            return f"JS result: {result_str}"

        elif action == "get_cookies":
            cookies = browser.cookies()
            cookie_names = [c.get("name", "?") for c in (cookies or [])[:20]]
            return f"Cookies ({len(cookies or [])}): {', '.join(cookie_names)}"

        elif action == "clear_cookies":
            browser.clear_cookies()
            return "Cookies cleared"

        else:
            return f"Unknown action: {action}"

    except Exception as e:
        return f"ERROR: {action} failed — {type(e).__name__}: {e}"


def _safe_url(browser: BrowserInterface) -> str:
    """Get browser URL safely (may fail during navigation)."""
    try:
        return browser.url
    except Exception:
        return ""


def _wait_after_click(browser: BrowserInterface, url_before: str):
    """Smart post-click wait: short wait if same page, load wait if navigated."""
    time.sleep(0.3)
    url_after = _safe_url(browser)
    if url_after and url_before and url_after != url_before:
        # Click triggered navigation — wait for new page to load
        clear_selector_cache()
        try:
            browser.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass  # Best effort — some pages don't fire load events cleanly
    else:
        time.sleep(0.2)  # Short pause for same-page interactions


def _ensure_visible(browser: BrowserInterface, selector: str):
    """Scroll element into view if it exists but is outside the viewport."""
    resolved = _resolve_selector(selector)
    try:
        safe_sel = resolved.replace("'", "\\'")
        is_offscreen = browser.evaluate(f"""
        (() => {{
            let el = null;
            try {{ el = document.querySelector('{safe_sel}'); }} catch(e) {{}}
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            return rect.bottom < 0 || rect.top > window.innerHeight;
        }})()
        """)
        if is_offscreen:
            browser.scroll_to(resolved)
            time.sleep(0.3)
    except Exception:
        pass  # Best effort — don't block the action if this fails


def _click_with_healing(browser: BrowserInterface, selector: str):
    """Click with self-healing: try cached selector first, then primary, then fallbacks."""
    selectors = _get_selector_candidates(selector)
    last_error = None
    for sel in selectors:
        try:
            browser.click(sel, timeout=5000)
            _cache_selector(selector, sel)
            return
        except Exception as e:
            last_error = e
            # Retry once for transient errors (element detached during click)
            if _is_transient_error(e):
                time.sleep(0.3)
                try:
                    browser.click(sel, timeout=5000)
                    _cache_selector(selector, sel)
                    return
                except Exception:
                    pass
            continue
    raise last_error or RuntimeError(f"No valid selector found for: {selector}")


def _fill_with_healing(browser: BrowserInterface, selector: str, value: str):
    """Fill with self-healing: try cached selector first, then primary, then fallbacks."""
    selectors = _get_selector_candidates(selector)
    last_error = None
    for sel in selectors:
        try:
            browser.fill(sel, value, timeout=5000)
            _cache_selector(selector, sel)
            return
        except Exception as e:
            last_error = e
            if _is_transient_error(e):
                time.sleep(0.3)
                try:
                    browser.fill(sel, value, timeout=5000)
                    _cache_selector(selector, sel)
                    return
                except Exception:
                    pass
            continue
    raise last_error or RuntimeError(f"No valid selector found for: {selector}")


def _is_transient_error(e: Exception) -> bool:
    """Check if an error is transient (worth retrying after a brief pause)."""
    msg = str(e).lower()
    transient_patterns = [
        "element is detached",
        "execution context was destroyed",
        "element is not attached",
        "frame was detached",
        "target closed",
        "element is not visible",
        "element is outside of the viewport",
        "intercept",  # "element click intercepted"
    ]
    return any(p in msg for p in transient_patterns)


def _get_selector_candidates(selector: str) -> list[str]:
    """Build a list of selector candidates for self-healing resolution.

    Priority order:
    1. Cached successful selector (from previous self-healing on same page)
    2. Primary resolved selector
    3. Alternative selectors from the interactive elements list
    """
    candidates = []

    # Check cache first — if we've resolved this selector before on this page, try that first
    cached = _selector_cache.get(selector)
    if cached:
        candidates.append(cached)

    candidates.append(_resolve_selector(selector))

    # If selector references an element by index, get its alternative selectors
    stripped = selector.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            idx = int(stripped[1:-1])
            for el in _last_elements:
                if el.get("index") == idx:
                    candidates.extend(el.get("selectors", []))
                    break
        except ValueError:
            pass
    else:
        # Try to find matching element and add its alternatives
        for el in _last_elements:
            if el.get("selector") == selector or el.get("id") == selector.lstrip("#"):
                candidates.extend(el.get("selectors", []))
                break

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _resolve_selector(selector: str) -> str:
    """Resolve a selector, handling [index] references from the elements list."""
    selector = selector.strip()
    if selector.startswith("[") and selector.endswith("]"):
        try:
            idx = int(selector[1:-1])
            # Look up actual selector from elements list
            for el in _last_elements:
                if el.get("index") == idx:
                    return el.get("selector", selector)
            # Fallback
            return f":nth-match(a, button, input, select, textarea, [role='button'], {idx + 1})"
        except ValueError:
            pass
    return selector


def _dismiss_overlay(browser: BrowserInterface) -> str:
    """Try to dismiss overlays, cookie banners, popups, and modals."""
    # Common dismiss button selectors, ordered by specificity
    dismiss_selectors = [
        # Cookie consent buttons
        'button[id*="accept"]',
        'button[id*="agree"]',
        'button[id*="consent"]',
        'button[class*="accept"]',
        'button[class*="agree"]',
        'button[class*="consent"]',
        "text=Accept All",
        "text=Accept all",
        "text=Accept Cookies",
        "text=Accept all cookies",
        "text=I agree",
        "text=Agree",
        "text=Got it",
        "text=OK",
        "text=I Accept",
        # Modal close buttons
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        'button[aria-label="Dismiss"]',
        '[class*="close-button"]',
        '[class*="close-btn"]',
        '[class*="modal-close"]',
        '[class*="popup-close"]',
        "button.close",
        ".modal .close",
        # Generic X buttons
        'button:has-text("×")',
        'button:has-text("✕")',
        # Escape key as last resort
    ]

    dismissed = False
    for sel in dismiss_selectors:
        try:
            count = browser.query_all(sel)
            if count > 0:
                browser.click(sel, timeout=3000)
                time.sleep(0.5)
                dismissed = True
                return f"Dismissed overlay using: {sel}"
        except Exception:
            continue

    # Try pressing Escape
    try:
        browser.page.keyboard.press("Escape")
        time.sleep(0.3)
        return "Pressed Escape to dismiss overlay"
    except Exception:
        pass

    return "No overlay found to dismiss" if not dismissed else "Overlay dismissed"
