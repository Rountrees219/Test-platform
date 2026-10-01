"""
Observer Module — Captures page state for the LLM planner.

Responsibilities:
- Take screenshots of the current page
- Extract accessibility tree (primary, ~90% token savings vs raw DOM)
- Extract interactive elements as fallback
- Capture page metadata (URL, title, errors)
- Detect overlays/popups that may block interaction
"""

import base64
import re
from typing import Optional

from browser.browser_interface import BrowserInterface
from browser.config import SCREENSHOTS_DIR


def observe(browser: BrowserInterface, step: int = 0, screenshot: bool = True) -> dict:
    """
    Capture the current page state as an observation dict.

    Returns:
        {
            "url": str,
            "title": str,
            "screenshot_path": str | None,
            "screenshot_b64": str | None,
            "accessibility_tree": str,
            "interactive_elements": list[dict],
            "has_overlay": bool,
            "errors": str | None,
        }
    """
    browser._ok()

    # Wait for page to settle before observing
    try:
        browser.page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    # Also try networkidle for SPAs (short timeout — don't block on long-polling)
    try:
        browser.page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass  # Many pages never reach networkidle — that's fine

    try:
        url = browser.url
    except Exception:
        url = "unknown"

    try:
        title = browser.title
    except Exception:
        title = ""

    # Interactive elements (extracted before screenshot for SoM labels)
    interactive_elements = _extract_interactive_elements(browser)

    # Screenshot (with optional Set-of-Mark labels)
    screenshot_path = None
    screenshot_b64 = None
    if screenshot:
        screenshot_path = str(SCREENSHOTS_DIR / f"step_{step:03d}.png")
        try:
            # Inject SoM labels before screenshot, remove after
            _inject_som_labels(browser, interactive_elements)
            browser.screenshot(screenshot_path)
            _remove_som_labels(browser)
            with open(screenshot_path, "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            _remove_som_labels(browser)  # cleanup on error
            screenshot_path = None

    # Accessibility tree (primary representation — compact, structured)
    a11y_tree = _get_accessibility_tree(browser)

    # Detect overlays/popups
    has_overlay = _detect_overlay(browser)

    # Error report
    errors = None
    if browser.devtools.has_errors:
        errors = browser.devtools.format_report()

    return {
        "url": url,
        "title": title,
        "screenshot_path": screenshot_path,
        "screenshot_b64": screenshot_b64,
        "accessibility_tree": a11y_tree,
        "interactive_elements": interactive_elements,
        "has_overlay": has_overlay,
        "errors": errors,
    }


def _get_accessibility_tree(browser: BrowserInterface) -> str:
    """
    Get the page's accessibility tree via Playwright.

    This is the primary page representation — far more compact than raw DOM
    while retaining all information the LLM needs to understand the page
    structure and available interactions.
    """
    try:
        snapshot = browser.page.accessibility.snapshot()
        if snapshot:
            return _format_a11y_node(snapshot, depth=0, max_depth=6)
    except Exception:
        pass

    # Fallback to text-based summary if a11y tree unavailable
    return _build_text_summary(browser)


def _format_a11y_node(node: dict, depth: int = 0, max_depth: int = 6) -> str:
    """Recursively format an accessibility tree node into a compact text representation."""
    if depth > max_depth:
        return ""

    lines = []
    indent = "  " * depth

    role = node.get("role", "")
    name = node.get("name", "").strip()
    value = node.get("value", "")
    description = node.get("description", "")
    focused = node.get("focused", False)

    # Skip generic/empty nodes
    if role in ("none", "generic", "LineBreak") and not name:
        children = node.get("children", [])
        for child in children:
            child_text = _format_a11y_node(child, depth, max_depth)
            if child_text:
                lines.append(child_text)
        return "\n".join(lines)

    # Build node description
    parts = [f"{indent}[{role}]"]
    if name:
        parts.append(f'"{name[:80]}"')
    if value:
        parts.append(f'value="{str(value)[:40]}"')
    if description:
        parts.append(f'desc="{description[:40]}"')
    if focused:
        parts.append("(focused)")

    line = " ".join(parts)
    lines.append(line)

    # Recurse into children
    children = node.get("children", [])
    for child in children:
        child_text = _format_a11y_node(child, depth + 1, max_depth)
        if child_text:
            lines.append(child_text)

    return "\n".join(lines)


def _build_text_summary(browser: BrowserInterface) -> str:
    """Fallback: build a text-based page summary when a11y tree is unavailable."""
    try:
        body_text = browser.text("body")
        if body_text:
            text = re.sub(r"\s+", " ", body_text).strip()[:2000]
            return f"Page text: {text}"
    except Exception:
        pass
    return "(empty page)"


def _extract_interactive_elements(browser: BrowserInterface) -> list[dict]:
    """Extract clickable/interactive elements with multiple selector candidates for self-healing."""
    js = """
    (() => {
        const selectors = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [onclick], [tabindex]:not([tabindex="-1"])';
        const elements = [...document.querySelectorAll(selectors)];
        return elements.slice(0, 100).map((el, i) => {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return null;
            if (getComputedStyle(el).display === 'none') return null;
            if (getComputedStyle(el).visibility === 'hidden') return null;

            const tag = el.tagName.toLowerCase();
            const type = el.getAttribute('type') || '';
            const text = (el.textContent || '').trim().slice(0, 80);
            const placeholder = el.getAttribute('placeholder') || '';
            const href = el.getAttribute('href') || '';
            const name = el.getAttribute('name') || '';
            const id = el.getAttribute('id') || '';
            const ariaLabel = el.getAttribute('aria-label') || '';
            const role = el.getAttribute('role') || '';
            const value = el.value || '';
            const title = el.getAttribute('title') || '';
            const className = el.className && typeof el.className === 'string' ? el.className.split(' ').filter(c => c.length > 0 && c.length < 30).slice(0, 3).join('.') : '';

            // Build multiple selector candidates (for self-healing)
            const selectors = [];
            if (id) selectors.push('#' + id);
            if (ariaLabel) selectors.push(tag + '[aria-label="' + ariaLabel.slice(0, 50) + '"]');
            if (name) selectors.push(tag + '[name="' + name + '"]');
            if (title) selectors.push(tag + '[title="' + title.slice(0, 50) + '"]');
            if (type && tag === 'input') selectors.push('input[type="' + type + '"]');
            if (href && tag === 'a' && href.length < 80) selectors.push('a[href="' + href + '"]');
            if (text && text.length > 0 && text.length < 40) selectors.push('text=' + text);
            if (className) selectors.push(tag + '.' + className);
            if (selectors.length === 0) selectors.push(tag);

            return {
                index: i,
                tag,
                type,
                text: text.slice(0, 60),
                placeholder,
                href: href.slice(0, 100),
                name,
                id,
                ariaLabel,
                role,
                value: value.slice(0, 40),
                selector: selectors[0],
                selectors: selectors,
                visible: rect.top < window.innerHeight && rect.bottom > 0,
            };
        }).filter(Boolean);
    })()
    """
    try:
        return browser.evaluate(js) or []
    except Exception:
        return []


def _detect_overlay(browser: BrowserInterface) -> bool:
    """Detect common overlays, cookie banners, and modals that may block interaction."""
    js = """
    (() => {
        // Common overlay selectors
        const overlaySelectors = [
            '[class*="cookie"]', '[id*="cookie"]',
            '[class*="consent"]', '[id*="consent"]',
            '[class*="modal"]', '[id*="modal"]',
            '[class*="popup"]', '[id*="popup"]',
            '[class*="overlay"]', '[id*="overlay"]',
            '[class*="banner"]', '[id*="banner"]',
            '[role="dialog"]', '[role="alertdialog"]',
            '.gdpr', '#gdpr',
        ];
        for (const sel of overlaySelectors) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                if (rect.width > 200 && rect.height > 100 &&
                    style.display !== 'none' && style.visibility !== 'hidden' &&
                    parseFloat(style.opacity || '1') > 0.5) {
                    return true;
                }
            }
        }
        return false;
    })()
    """
    try:
        return browser.evaluate(js) or False
    except Exception:
        return False


def _inject_som_labels(browser: BrowserInterface, elements: list[dict]) -> None:
    """
    Inject Set-of-Mark numbered labels onto interactive elements in the page.

    Labels are small numbered badges overlaid on each visible interactive element,
    allowing the vision model to reference elements by index number in screenshots.
    Labels are injected as a single overlay container (id=ninja-som) so they can
    be cleanly removed after the screenshot.
    """
    if not elements:
        return

    # Build label data for visible elements only
    visible = [e for e in elements if e.get("visible", True)]
    if not visible:
        return

    js = """
    (labels) => {
        // Remove any existing SoM overlay
        const existing = document.getElementById('ninja-som');
        if (existing) existing.remove();

        const container = document.createElement('div');
        container.id = 'ninja-som';
        container.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:2147483647;';
        document.body.appendChild(container);

        for (const label of labels) {
            let el = null;
            // Try each selector until one matches
            for (const sel of (label.selectors || [label.selector])) {
                try { el = document.querySelector(sel); } catch(e) {}
                if (el) break;
            }
            if (!el) continue;

            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            if (rect.top > window.innerHeight || rect.bottom < 0) continue;

            const badge = document.createElement('div');
            badge.className = 'ninja-som-label';
            badge.textContent = label.index;
            badge.style.cssText = `
                position:fixed;
                left:${Math.max(0, rect.left - 2)}px;
                top:${Math.max(0, rect.top - 2)}px;
                min-width:18px;
                height:18px;
                line-height:18px;
                padding:0 3px;
                font-size:11px;
                font-weight:bold;
                font-family:Arial,sans-serif;
                color:white;
                background:rgba(220,38,38,0.9);
                border-radius:9px;
                text-align:center;
                pointer-events:none;
                box-shadow:0 1px 3px rgba(0,0,0,0.3);
            `;
            container.appendChild(badge);
        }
    }
    """
    try:
        label_data = [
            {
                "index": e["index"],
                "selector": e.get("selector", ""),
                "selectors": e.get("selectors", []),
            }
            for e in visible[:50]  # Cap at 50 labels to avoid visual clutter
        ]
        browser.evaluate(js, label_data)
    except Exception:
        pass


def _remove_som_labels(browser: BrowserInterface) -> None:
    """Remove Set-of-Mark labels from the page."""
    try:
        browser.evaluate(
            "(() => { const el = document.getElementById('ninja-som'); if (el) el.remove(); })()"
        )
    except Exception:
        pass
