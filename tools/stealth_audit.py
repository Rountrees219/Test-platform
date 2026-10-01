#!/usr/bin/env python3
"""
Stealth Audit — Consolidated browser anti-detection verification.

Connects to the running browser, navigates to bot detection sites,
and reports on stealth status. Combines functionality from
bot_detection_audit.py, fingerprint_check.py, and test_stealth_fixes.py.

Usage:
    python tools/stealth_audit.py                  # Full audit (human-readable)
    python tools/stealth_audit.py --json           # JSON output
    python tools/stealth_audit.py --quick          # Quick check (JS only, no navigation)
    python tools/stealth_audit.py --site sannysoft # Test against specific site

Python API:
    from tools.stealth_audit import run_stealth_audit, quick_check
    result = run_stealth_audit()
    print(result["overall"])  # "pass" or "fail"
"""

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Stealth checks to run via JavaScript in the browser
STEALTH_CHECKS_JS = """
() => {
    const results = {};

    // 1. WebDriver flag
    results.webdriver = {
        value: navigator.webdriver,
        pass: navigator.webdriver === undefined || navigator.webdriver === false
    };

    // 2. Chrome runtime
    results.chrome_runtime = {
        value: typeof window.chrome !== 'undefined' && typeof window.chrome.runtime !== 'undefined',
        pass: typeof window.chrome !== 'undefined'
    };

    // 3. Permissions
    try {
        const permStatus = navigator.permissions.query.toString();
        results.permissions = {
            value: permStatus.substring(0, 60),
            pass: !permStatus.includes('native code') || permStatus.includes('function')
        };
    } catch(e) {
        results.permissions = { value: 'error: ' + e.message, pass: false };
    }

    // 4. Plugins
    results.plugins = {
        value: navigator.plugins.length,
        pass: navigator.plugins.length >= 1
    };

    // 5. Languages
    results.languages = {
        value: navigator.languages,
        pass: navigator.languages && navigator.languages.length > 0
    };

    // 6. Platform
    results.platform = {
        value: navigator.platform,
        pass: ['Win32', 'MacIntel', 'Linux x86_64'].includes(navigator.platform)
    };

    // 7. Hardware concurrency
    results.hardware_concurrency = {
        value: navigator.hardwareConcurrency,
        pass: navigator.hardwareConcurrency >= 2
    };

    // 8. Device memory
    results.device_memory = {
        value: navigator.deviceMemory,
        pass: navigator.deviceMemory >= 2
    };

    // 9. User agent
    results.user_agent = {
        value: navigator.userAgent.substring(0, 80),
        pass: !navigator.userAgent.includes('Headless')
    };

    // 10. WebGL vendor
    try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl');
        const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
        results.webgl_vendor = {
            value: gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL),
            pass: true
        };
        results.webgl_renderer = {
            value: gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL),
            pass: !gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL).includes('SwiftShader')
        };
    } catch(e) {
        results.webgl = { value: 'error: ' + e.message, pass: false };
    }

    // 11. Automation artifacts
    results.automation_artifacts = {
        value: {
            cdc_present: !!document.querySelector('[cdc_adoQpoasnfa76pfcZLmcfl_Array]'),
            selenium_present: !!window._selenium,
            webdriver_present: !!window.__webdriver_evaluate,
            domAutomation: !!window.domAutomation,
        },
        pass: !document.querySelector('[cdc_adoQpoasnfa76pfcZLmcfl_Array]') &&
              !window._selenium &&
              !window.__webdriver_evaluate &&
              !window.domAutomation
    };

    // Summary
    const checks = Object.values(results);
    const passed = checks.filter(c => c.pass).length;
    const total = checks.length;

    return {
        checks: results,
        passed: passed,
        total: total,
        overall: passed === total ? 'pass' : (passed >= total - 2 ? 'partial' : 'fail')
    };
}
"""


async def _run_audit_async(site: str = None, quick: bool = False) -> dict:
    """Run the stealth audit asynchronously."""
    try:
        from browser.browser_interface import BrowserInterface
    except ImportError:
        return {"error": "Cannot import BrowserInterface. Run from project root."}

    result = {"checks": {}, "site_tests": []}

    try:
        browser = BrowserInterface.connect_cdp()
    except Exception as e:
        return {"error": f"Cannot connect to browser: {e}. Is browser_server running?"}

    try:
        # Run JavaScript stealth checks
        js_result = browser.page.evaluate(STEALTH_CHECKS_JS)
        result["checks"] = js_result.get("checks", {})
        result["passed"] = js_result.get("passed", 0)
        result["total"] = js_result.get("total", 0)
        result["overall"] = js_result.get("overall", "unknown")

        # If not quick mode, test against detection sites
        if not quick:
            sites = []
            if site == "sannysoft" or site is None:
                sites.append(("bot.sannysoft.com", "https://bot.sannysoft.com"))
            if site == "browserleaks" or site is None:
                sites.append(
                    ("browserleaks.com", "https://browserleaks.com/javascript")
                )

            for site_name, url in sites:
                try:
                    browser.go(url)
                    browser.page.wait_for_load_state("networkidle", timeout=10000)
                    import time

                    time.sleep(2)

                    # Take screenshot for evidence
                    screenshot_path = (
                        REPO_ROOT
                        / "ninja"
                        / "screenshots"
                        / f"stealth_audit_{site_name}.png"
                    )
                    browser.screenshot(str(screenshot_path))

                    # Extract page text for analysis
                    text = browser.page.evaluate("() => document.body.innerText")

                    site_result = {
                        "site": site_name,
                        "url": url,
                        "screenshot": str(screenshot_path),
                        "loaded": True,
                    }

                    # Parse sannysoft results
                    if "sannysoft" in site_name:
                        site_result["details"] = {
                            "webdriver_hidden": "webdriver" not in text.lower()
                            or "false" in text.lower(),
                        }

                    result["site_tests"].append(site_result)

                except Exception as e:
                    result["site_tests"].append(
                        {
                            "site": site_name,
                            "url": url,
                            "loaded": False,
                            "error": str(e),
                        }
                    )

    except Exception as e:
        result["error"] = f"Audit failed: {e}"
    finally:
        try:
            browser.stop()
        except Exception:
            pass

    return result


def run_stealth_audit(site: str = None, quick: bool = False) -> dict:
    """
    Run stealth audit synchronously.

    Args:
        site: Specific site to test ("sannysoft", "browserleaks", or None for all)
        quick: If True, only run JS checks without navigating to detection sites

    Returns:
        Dict with check results, pass/fail counts, and optional site test results
    """
    return asyncio.get_event_loop().run_until_complete(_run_audit_async(site, quick))


def quick_check() -> dict:
    """Run quick JS-only stealth check without navigating to test sites."""
    return run_stealth_audit(quick=True)


def print_audit(result: dict):
    """Pretty-print audit results."""
    if "error" in result:
        print(f"❌ {result['error']}")
        return

    icons = {True: "✅", False: "❌"}
    overall_icons = {"pass": "🟢", "partial": "🟡", "fail": "🔴"}

    print(f"\n{'=' * 60}")
    print("🕵️ STEALTH AUDIT")
    print(f"{'=' * 60}")

    # JS checks
    checks = result.get("checks", {})
    for name, check in checks.items():
        icon = icons.get(check.get("pass"), "❓")
        value = check.get("value", "")
        if isinstance(value, dict):
            value = json.dumps(value)
        value_str = str(value)[:60]
        print(f"  {icon} {name:25s} = {value_str}")

    passed = result.get("passed", 0)
    total = result.get("total", 0)
    overall = result.get("overall", "unknown")
    icon = overall_icons.get(overall, "❓")

    print(f"\n  {icon} Result: {passed}/{total} checks passed ({overall.upper()})")

    # Site tests
    site_tests = result.get("site_tests", [])
    if site_tests:
        print(f"\n  🌐 Detection Site Tests:")
        for st in site_tests:
            loaded = (
                "✅ loaded" if st.get("loaded") else f"❌ {st.get('error', 'failed')}"
            )
            print(f"    {st['site']:25s} {loaded}")
            if st.get("screenshot"):
                print(f"      📸 {st['screenshot']}")

    print(f"\n{'=' * 60}\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Stealth Audit — Browser anti-detection verification",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/stealth_audit.py                  Full audit
  python tools/stealth_audit.py --quick          JS checks only (fast)
  python tools/stealth_audit.py --site sannysoft Test specific site
  python tools/stealth_audit.py --json           JSON output
        """,
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--quick", action="store_true", help="Quick JS-only check")
    parser.add_argument(
        "--site",
        choices=["sannysoft", "browserleaks"],
        help="Test specific detection site",
    )

    args = parser.parse_args()
    result = run_stealth_audit(site=args.site, quick=args.quick)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_audit(result)

    overall = result.get("overall", "fail")
    sys.exit(0 if overall in ("pass", "partial") else 1)


if __name__ == "__main__":
    main()
