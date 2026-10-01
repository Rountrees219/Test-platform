#!/usr/bin/env python3
"""
Stealth patches for Chromium anti-bot detection evasion.

This module provides the stealth JavaScript that gets injected into every page
via Playwright's context.add_init_script(). It's automatically applied when
connecting to the browser via BrowserInterface.connect_cdp() or start().

Usage:
    # Automatic (preferred) — stealth is applied by BrowserInterface:
    from browser.browser_interface import BrowserInterface
    browser = BrowserInterface.connect_cdp()  # stealth already active

    # Manual — get the script for custom use:
    from browser.stealth import STEALTH_JS
    browser.context.add_init_script(STEALTH_JS)

    # Check stealth status on current page:
    from browser.stealth import check_stealth
    result = check_stealth(browser)

    # CLI:
    python ninja/stealth.py check   # Verify stealth via running browser
"""

# ─── Stealth JavaScript ──────────────────────────────────────────────────────
# Combined script injected via context.add_init_script().
# Runs before any page JavaScript on every navigation and new tab.

STEALTH_JS = """
// === Ninja Stealth Patches ===
// Injected via Playwright context.add_init_script()
// Runs before any page JS on every navigation.

// 1. Hide navigator.webdriver (must pass BOTH checks):
//    - navigator.webdriver === undefined  (value check)
//    - 'webdriver' in navigator === false  (property existence check)
// Simply setting the getter to undefined leaves the property in the descriptor,
// so 'webdriver' in navigator still returns true. We must delete from the
// prototype chain and ensure no own-property remains.
(function() {
    // Delete from Navigator.prototype (where Chromium defines it)
    const proto = Object.getPrototypeOf(navigator);
    if ('webdriver' in proto) {
        delete proto.webdriver;
    }
    // Also delete any own property on the navigator instance
    if (Object.getOwnPropertyDescriptor(navigator, 'webdriver')) {
        delete navigator.webdriver;
    }
    // Final fallback: if somehow still present, redefine with value false
    // and then delete again (handles frozen prototypes)
    if ('webdriver' in navigator) {
        Object.defineProperty(navigator, 'webdriver', {
            value: undefined,
            writable: true,
            configurable: true,
        });
        delete navigator.webdriver;
    }
})();

// 2. Fix chrome.runtime (normal Chrome has this, automated often doesn't)
if (!window.chrome) {
    window.chrome = {};
}
if (!window.chrome.runtime) {
    window.chrome.runtime = {
        connect: function() {},
        sendMessage: function() {},
        onMessage: { addListener: function() {} },
    };
}

// 3. Fix permissions API (automated browsers return inconsistent states)
// Also mask toString() so it returns '[native code]' instead of leaking
// the override as a Proxy or wrapper function.
(function() {
    try {
        const originalQuery = navigator.permissions.query.bind(navigator.permissions);

        // Create the replacement function
        const patchedQuery = function query(parameters) {
            if (parameters.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission });
            }
            return originalQuery(parameters);
        };

        // Mask toString/toSource to look native
        const nativeToString = 'function query() { [native code] }';
        patchedQuery.toString = function() { return nativeToString; };
        patchedQuery.toString.toString = function() { return 'function toString() { [native code] }'; };

        // Also handle Function.prototype.toString.call(fn)
        const origFnToString = Function.prototype.toString;
        const fnToStringProxy = new Proxy(origFnToString, {
            apply: function(target, thisArg, args) {
                if (thisArg === patchedQuery) {
                    return nativeToString;
                }
                if (thisArg === patchedQuery.toString) {
                    return 'function toString() { [native code] }';
                }
                return Reflect.apply(target, thisArg, args);
            }
        });
        Function.prototype.toString = fnToStringProxy;

        // Set the patched function on permissions
        Object.defineProperty(navigator.permissions, 'query', {
            value: patchedQuery,
            writable: true,
            configurable: true,
        });
    } catch(e) {}
})();

// 4. Fix plugins array (automated Chrome often has empty plugins)
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
              description: 'Portable Document Format',
              length: 1, item: function(i) { return this; },
              namedItem: function(n) { return this; },
              0: { type: 'application/x-google-chrome-pdf',
                   suffixes: 'pdf', description: 'Portable Document Format',
                   enabledPlugin: null }},
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
              description: '', length: 1,
              item: function(i) { return this; },
              namedItem: function(n) { return this; },
              0: { type: 'application/pdf', suffixes: 'pdf',
                   description: '', enabledPlugin: null }},
            { name: 'Native Client', filename: 'internal-nacl-plugin',
              description: '', length: 2,
              item: function(i) { return this; },
              namedItem: function(n) { return this; },
              0: { type: 'application/x-nacl', suffixes: '',
                   description: 'Native Client Executable', enabledPlugin: null },
              1: { type: 'application/x-pnacl', suffixes: '',
                   description: 'Portable Native Client Executable',
                   enabledPlugin: null }},
        ];
        plugins.length = 3;
        plugins.item = function(i) { return this[i] || null; };
        plugins.namedItem = function(n) {
            for (let i = 0; i < this.length; i++) {
                if (this[i].name === n) return this[i];
            }
            return null;
        };
        plugins.refresh = function() {};
        return plugins;
    },
    configurable: true,
});

// 5. Fix languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
    configurable: true,
});

// 5b. Spoof hardware fingerprint (VM/server detection)
// Real desktops: 4-16 cores, 8-32GB RAM. Servers: 2 cores, no deviceMemory.
Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
    configurable: true,
});
Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
    configurable: true,
});
// Spoof platform to Windows (Linux x86_64 is a server giveaway)
Object.defineProperty(navigator, 'platform', {
    get: () => 'Win32',
    configurable: true,
});
// Match userAgent to Win32 platform (replace Linux x86_64 with Windows NT 10.0)
(function() {
    const origUA = navigator.userAgent;
    if (origUA.includes('Linux')) {
        const winUA = origUA
            .replace('X11; Linux x86_64', 'Windows NT 10.0; Win64; x64');
        Object.defineProperty(navigator, 'userAgent', {
            get: () => winUA,
            configurable: true,
        });
        // Also fix appVersion and oscpu to match
        Object.defineProperty(navigator, 'appVersion', {
            get: () => winUA.replace('Mozilla/', ''),
            configurable: true,
        });
    }
})();

// 6. Remove automation artifacts
(function() {
    try {
        const props = Object.getOwnPropertyNames(document);
        for (const prop of props) {
            if (prop.match(/^cdc_/)) {
                delete document[prop];
            }
        }
        delete navigator.__proto__.__webdriver_evaluate;
        delete navigator.__proto__.__driver_evaluate;
        delete navigator.__proto__.__webdriver_unwrap;
        delete navigator.__proto__.__driver_unwrap;
        delete navigator.__proto__.__selenium_evaluate;
        delete navigator.__proto__.__fxdriver_evaluate;
    } catch(e) {}
})();

// 7. Fix WebGL vendor/renderer
(function() {
    try {
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return 'Google Inc. (NVIDIA)';
            if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            return getParameter.call(this, parameter);
        };
        if (typeof WebGL2RenderingContext !== 'undefined') {
            const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Google Inc. (NVIDIA)';
                if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter2.call(this, parameter);
            };
        }
    } catch(e) {}
})();
"""

# JS snippet to check stealth status (returns JSON)
STEALTH_CHECK_JS = """() => {
    return JSON.stringify({
        webdriver: navigator.webdriver,
        webdriverType: typeof navigator.webdriver,
        webdriverInNav: 'webdriver' in navigator,
        chromeRuntime: !!window.chrome?.runtime,
        plugins: navigator.plugins.length,
        languages: navigator.languages,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory,
        platform: navigator.platform,
        permissionsQueryToString: navigator.permissions.query.toString(),
    });
}"""


def check_stealth(browser) -> dict:
    """Check if stealth patches are active on the current page.

    Args:
        browser: A BrowserInterface instance (connected).

    Returns:
        dict with detection test results:
        - webdriver: value of navigator.webdriver (should be None/undefined)
        - webdriverType: typeof navigator.webdriver (should be "undefined")
        - chromeRuntime: bool (should be True)
        - plugins: int (should be > 0)
        - languages: list (should be ['en-US', 'en'])
    """
    import json

    try:
        raw = browser.evaluate(STEALTH_CHECK_JS)
        result = json.loads(raw)
        return result
    except Exception as e:
        return {"error": str(e)}


def print_stealth_status(result: dict):
    """Pretty-print stealth check results."""
    if "error" in result:
        print(f"  ❌ Stealth check error: {result['error']}")
        return False

    all_good = True

    wd = result.get("webdriver")
    wd_type = result.get("webdriverType")
    if wd is None or wd is False or wd_type == "undefined":
        print("  ✅ navigator.webdriver is hidden")
    else:
        print(f"  ❌ navigator.webdriver = {wd} (DETECTABLE)")
        all_good = False

    if result.get("chromeRuntime"):
        print("  ✅ chrome.runtime is present")
    else:
        print("  ⚠️  chrome.runtime is missing")
        all_good = False

    plugins = result.get("plugins", 0)
    if plugins > 0:
        print(f"  ✅ navigator.plugins has {plugins} entries")
    else:
        print("  ⚠️  navigator.plugins is empty")
        all_good = False

    return all_good


# ─── Human-Like Interaction Helpers ──────────────────────────────────────────
# These use Playwright's mouse/keyboard APIs to simulate realistic human behavior.
# Use these instead of element.click() and keyboard.type() for anti-detection.

import math
import random
import time as _time

from tools.message_sanitizer import sanitize as _sanitize_text


def human_move_to(page, x: float, y: float, steps: int = 0):
    """Move mouse to (x, y) with a natural curved path.

    Generates a Bezier-like curve from current position to target,
    with slight randomness to look human. Much more natural than
    Playwright's default linear mouse.move().

    Args:
        page: Playwright Page object.
        x, y: Target coordinates.
        steps: Number of intermediate points (0 = auto based on distance).
    """
    # Get current mouse position (default to random starting point if unknown)
    current = page.evaluate("""() => {
        return JSON.stringify({
            x: window._ninjaMouseX || Math.random() * 400 + 100,
            y: window._ninjaMouseY || Math.random() * 300 + 100,
        });
    }""")
    import json

    pos = json.loads(current)
    cx, cy = pos["x"], pos["y"]

    # Calculate distance and auto-determine steps
    dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    if steps == 0:
        steps = max(5, min(25, int(dist / 30)))

    # Generate control points for a subtle curve
    # Offset perpendicular to the direct line
    mid_x = (cx + x) / 2 + random.uniform(-30, 30)
    mid_y = (cy + y) / 2 + random.uniform(-30, 30)

    for i in range(1, steps + 1):
        t = i / steps
        # Quadratic Bezier: B(t) = (1-t)^2*P0 + 2*(1-t)*t*P1 + t^2*P2
        bx = (1 - t) ** 2 * cx + 2 * (1 - t) * t * mid_x + t**2 * x
        by = (1 - t) ** 2 * cy + 2 * (1 - t) * t * mid_y + t**2 * y
        # Add tiny jitter
        bx += random.uniform(-1.5, 1.5)
        by += random.uniform(-1.5, 1.5)
        page.mouse.move(bx, by)
        _time.sleep(random.uniform(0.005, 0.02))

    # Final exact position
    page.mouse.move(x, y)

    # Track position for next call
    page.evaluate(f"window._ninjaMouseX = {x}; window._ninjaMouseY = {y};")


def human_click(page, selector: str, timeout: int = 10000):
    """Click an element with human-like mouse movement first.

    Moves the mouse naturally to the element's center (with slight offset),
    pauses briefly, then clicks. This generates proper mousemove, mouseover,
    mouseenter, mousedown, mouseup, and click events — matching what Twitter
    expects from real user interactions.

    Args:
        page: Playwright Page object.
        selector: CSS selector for the element to click.
        timeout: Max time to wait for element (ms).

    Returns:
        True if clicked successfully, False otherwise.
    """
    try:
        el = page.wait_for_selector(selector, timeout=timeout)
        if not el:
            return False

        # Get element bounding box
        box = el.bounding_box()
        if not box:
            return False

        # Target slightly off-center (humans don't click dead center)
        target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

        # Move mouse to element
        human_move_to(page, target_x, target_y)

        # Brief hover pause (humans hover before clicking)
        _time.sleep(random.uniform(0.05, 0.2))

        # Click
        page.mouse.click(target_x, target_y)
        return True
    except Exception:
        return False


def sanitize_tweet(text: str) -> str:
    """Sanitize tweet text before posting, removing AI slop.

    Removes em-dashes, emojis, over-punctuation, and other LLM artifacts.
    Call this to preview cleaned text before posting.

    Args:
        text: Raw tweet text.

    Returns:
        Cleaned text ready for posting.
    """
    return _sanitize_text(text)


def human_type(page, text: str, sanitize: bool = True):
    """Type text with human-like variable delays.

    Simulates realistic typing with:
    - Auto-sanitization via message_sanitizer (removes AI slop: em-dashes, emojis, etc.)
    - Variable delays between characters (30-120ms)
    - Longer pauses at punctuation and newlines (150-500ms)
    - Slight pauses between words (40-150ms)
    - Handles newlines via keyboard.press('Enter')

    Args:
        page: Playwright Page object.
        text: The text to type.
        sanitize: If True (default), run text through message_sanitizer first.
    """
    if sanitize:
        text = _sanitize_text(text)

    for char in text:
        if char == "\n":
            page.keyboard.press("Enter")
            _time.sleep(random.uniform(0.15, 0.5))
        else:
            page.keyboard.type(char, delay=0)
            # Variable delay based on character type
            if char in ".!?":
                _time.sleep(random.uniform(0.15, 0.5))
            elif char == ",":
                _time.sleep(random.uniform(0.08, 0.25))
            elif char == " ":
                _time.sleep(random.uniform(0.04, 0.15))
            else:
                _time.sleep(random.uniform(0.03, 0.12))


def main():
    """CLI entry point — check stealth on the running browser."""
    import sys
    from pathlib import Path

    # Ensure parent dir is on path for browser_interface import
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from browser.browser_interface import BrowserInterface

    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"

    if cmd == "check":
        print("🔍 Checking stealth status on running browser...")
        try:
            browser = BrowserInterface.connect_cdp()
            result = check_stealth(browser)
            print_stealth_status(result)
            browser.stop()
        except ConnectionError:
            print(
                "  ❌ No browser running. Start with: python ninja/browser_server.py start"
            )
            sys.exit(1)
    else:
        print(f"Usage: python ninja/stealth.py check")
        sys.exit(1)


if __name__ == "__main__":
    main()
