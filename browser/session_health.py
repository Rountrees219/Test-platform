#!/usr/bin/env python3
"""
Session Health Checker — monitors browser login sessions for any service.

Checks if the browser has valid session cookies for configured services
by inspecting the Chromium cookie database. Supports any service that
uses cookies for authentication.

Usage:
    # Python API (via BrowserInterface):
    from browser.browser_interface import BrowserInterface
    browser = BrowserInterface.connect_cdp()
    browser.check_session("google")     # Check Google/Gmail session
    browser.check_session("linkedin")   # Check LinkedIn session
    browser.session_status()            # Check all configured services

    # Python API (direct):
    from browser.session_health import check_session, list_services
    result = check_session("google")
    all_results = check_all_sessions()

    # CLI:
    python ninja/session_health.py status              # All services
    python ninja/session_health.py check google        # Specific service
    python ninja/session_health.py check linkedin      # Specific service
    python ninja/session_health.py login-url           # VNC URL for manual login
    python ninja/session_health.py monitor 30          # Continuous monitoring
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.metadata import load_sandbox_metadata

BROWSER_DATA_DIR = Path(__file__).parent / "browser_data"
COOKIES_DB = BROWSER_DATA_DIR / "Default" / "Cookies"

# ─── Service Profiles ─────────────────────────────────────────────────────────
# Each service defines:
#   - cookie_names: set of session cookie names to look for
#   - host_patterns: SQL LIKE patterns for the cookie host_key
#   - min_cookies: minimum cookies needed to consider session valid
#   - label: human-readable name
#   - login_url: where to navigate for manual login

SERVICE_PROFILES = {
    "google": {
        "label": "Google (Gmail, YouTube, etc.)",
        "cookie_names": {
            "SID",
            "HSID",
            "SSID",
            "APISID",
            "SAPISID",
            "OSID",
            "COMPASS",
            "__Secure-1PSID",
            "__Secure-3PSID",
        },
        "host_patterns": ["%google.com", "%gmail.com", "%youtube.com"],
        "min_cookies": 3,
        "login_url": "https://accounts.google.com",
    },
    "linkedin": {
        "label": "LinkedIn",
        "cookie_names": {
            "li_at",
            "JSESSIONID",
            "li_mc",
            "lidc",
        },
        "host_patterns": ["%linkedin.com"],
        "min_cookies": 1,  # li_at alone is sufficient
        "login_url": "https://www.linkedin.com/login",
    },
    "twitter": {
        "label": "Twitter / X",
        "cookie_names": {
            "auth_token",
            "ct0",
            "twid",
        },
        "host_patterns": ["%twitter.com", "%x.com"],
        "min_cookies": 1,  # auth_token alone is sufficient
        "login_url": "https://x.com/login",
    },
    "github": {
        "label": "GitHub",
        "cookie_names": {
            "user_session",
            "dotcom_user",
            "__Host-user_session_same_site",
            "logged_in",
        },
        "host_patterns": ["%github.com"],
        "min_cookies": 1,  # user_session alone is sufficient
        "login_url": "https://github.com/login",
    },
    "amazon": {
        "label": "Amazon",
        "cookie_names": {
            "session-id",
            "session-token",
            "x-main",
            "at-main",
            "sess-at-main",
        },
        "host_patterns": ["%amazon.com", "%amazon.co%"],
        "min_cookies": 2,
        "login_url": "https://www.amazon.com/ap/signin",
    },
    "facebook": {
        "label": "Facebook / Meta",
        "cookie_names": {
            "c_user",
            "xs",
            "datr",
            "sb",
        },
        "host_patterns": ["%facebook.com"],
        "min_cookies": 2,  # c_user + xs
        "login_url": "https://www.facebook.com/login",
    },
}


def _read_cookies(host_patterns: list[str]) -> list[dict]:
    """Read cookies from Chrome's SQLite database for given host patterns.

    Returns list of cookie dicts with: name, host_key, expires_utc, is_secure, is_httponly.
    """
    if not COOKIES_DB.exists():
        raise FileNotFoundError(f"Cookie database not found: {COOKIES_DB}")

    # Copy DB to avoid Chrome lock
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        subprocess.run(
            ["cp", str(COOKIES_DB), tmp_path], check=True, capture_output=True
        )

        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row

        # Build WHERE clause for host patterns
        where_clauses = " OR ".join(
            f"host_key LIKE '{pattern}'" for pattern in host_patterns
        )

        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT name, host_key, path, expires_utc, is_secure, is_httponly,
                   last_access_utc, has_expires, is_persistent
            FROM cookies
            WHERE {where_clauses}
            ORDER BY name
        """)

        cookies = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return cookies
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _chrome_time_to_datetime(chrome_ts: int) -> Optional[datetime]:
    """Convert Chrome's microseconds-since-1601 timestamp to datetime."""
    if not chrome_ts or chrome_ts <= 0:
        return None
    # Chrome epoch: 1601-01-01, Unix epoch: 1970-01-01
    # Difference: 11644473600 seconds
    unix_ts = (chrome_ts / 1_000_000) - 11644473600
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    except (ValueError, OSError):
        return None


def check_session(service: str) -> dict:
    """Check session cookies for a specific service.

    Args:
        service: Service name (e.g., "google", "linkedin", "twitter").
                 Must be a key in SERVICE_PROFILES, or a custom dict with
                 cookie_names, host_patterns, min_cookies.

    Returns:
        dict with:
            - service: str — service name
            - label: str — human-readable name
            - valid: bool — whether enough session cookies exist
            - cookies_found: list of cookie names found
            - cookies_missing: list of expected cookies not found
            - earliest_expiry: ISO datetime string or None
            - details: list of cookie detail dicts
            - login_url: str — where to login manually
            - error: str or None
    """
    if isinstance(service, str):
        profile = SERVICE_PROFILES.get(service)
        if not profile:
            return {
                "service": service,
                "label": service,
                "valid": False,
                "cookies_found": [],
                "cookies_missing": [],
                "earliest_expiry": None,
                "details": [],
                "login_url": "",
                "error": f"Unknown service: {service}. Available: {', '.join(SERVICE_PROFILES.keys())}",
            }
    else:
        profile = service

    result = {
        "service": (
            service if isinstance(service, str) else profile.get("label", "custom")
        ),
        "label": profile.get("label", service),
        "valid": False,
        "cookies_found": [],
        "cookies_missing": [],
        "earliest_expiry": None,
        "details": [],
        "login_url": profile.get("login_url", ""),
        "error": None,
    }

    try:
        cookies = _read_cookies(profile["host_patterns"])
    except FileNotFoundError as e:
        result["error"] = str(e)
        return result
    except Exception as e:
        result["error"] = f"Failed to read cookies: {e}"
        return result

    expected = profile["cookie_names"]
    found = set()
    earliest_expiry = None

    for cookie in cookies:
        name = cookie["name"]
        if name in expected:
            found.add(name)

            expiry_dt = _chrome_time_to_datetime(cookie["expires_utc"])
            if expiry_dt:
                if earliest_expiry is None or expiry_dt < earliest_expiry:
                    earliest_expiry = expiry_dt

                result["details"].append(
                    {
                        "name": name,
                        "host": cookie["host_key"],
                        "expires": expiry_dt.isoformat(),
                        "secure": bool(cookie["is_secure"]),
                        "httponly": bool(cookie["is_httponly"]),
                    }
                )

    result["cookies_found"] = sorted(found)
    result["cookies_missing"] = sorted(expected - found)
    result["earliest_expiry"] = earliest_expiry.isoformat() if earliest_expiry else None
    result["valid"] = len(found) >= profile["min_cookies"]

    return result


def check_all_sessions() -> dict[str, dict]:
    """Check session health for all configured services.

    Returns dict mapping service name → check result.
    """
    results = {}
    for service_name in SERVICE_PROFILES:
        results[service_name] = check_session(service_name)
    return results


def list_services() -> list[dict]:
    """List all configured service profiles."""
    return [
        {
            "name": name,
            "label": profile["label"],
            "cookies_tracked": len(profile["cookie_names"]),
            "login_url": profile["login_url"],
        }
        for name, profile in SERVICE_PROFILES.items()
    ]


def get_vnc_url() -> str:
    """Get the VNC URL for manual browser login (port 6081, no password)."""
    try:
        from browser.vnc import get_vnc_url as _get_vnc_url

        return _get_vnc_url()
    except ImportError:
        try:
            meta = load_sandbox_metadata()
            sandbox_id = meta["thread_id"]
            stage = meta.get("environment", "")
            prefix = f"{stage}" if stage and stage != "prod" else ""
            return f"https://6080-{sandbox_id}.app.super.{prefix}myninja.ai/vnc.html?autoconnect=true"
        except Exception:
            return "http://0.0.0.0:6080/vnc.html?autoconnect=true"


def print_status(results: Optional[dict] = None, service: Optional[str] = None):
    """Pretty-print session health status.

    Args:
        results: dict of service → check_result. If None, checks all.
        service: if provided, only check this service.
    """
    if results is None:
        if service:
            results = {service: check_session(service)}
        else:
            results = check_all_sessions()

    print("\n" + "=" * 60)
    print("  🔐 Browser Session Health")
    print("=" * 60)

    any_invalid = False

    for svc_name, result in results.items():
        label = result.get("label", svc_name)

        if result.get("error"):
            print(f"\n  ❌ {label}: ERROR — {result['error']}")
            any_invalid = True
        elif result["valid"]:
            found = ", ".join(result["cookies_found"][:5])
            extra = (
                f" +{len(result['cookies_found'])-5} more"
                if len(result["cookies_found"]) > 5
                else ""
            )
            print(f"\n  ✅ {label}: ACTIVE")
            print(f"     Cookies: {found}{extra}")
            if result["earliest_expiry"]:
                print(f"     Expires: {result['earliest_expiry']}")
        else:
            print(f"\n  ❌ {label}: NOT LOGGED IN")
            if result["cookies_found"]:
                print(f"     Partial: {', '.join(result['cookies_found'])}")
            if result.get("login_url"):
                print(f"     Login at: {result['login_url']}")
            any_invalid = True

    if any_invalid:
        print(f"\n  🔑 Manual login needed — open VNC:")
        print(f"     {get_vnc_url()}")

    print("\n" + "=" * 60)
    return not any_invalid


def monitor(interval_minutes: int = 30, services: Optional[list[str]] = None):
    """Continuously monitor session health.

    Args:
        interval_minutes: check interval
        services: list of service names to monitor (None = all)
    """
    svc_label = ", ".join(services) if services else "all services"
    print(f"🔄 Monitoring {svc_label} (every {interval_minutes} min)")
    print(f"   Press Ctrl+C to stop\n")

    check_count = 0
    while True:
        check_count += 1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{timestamp}] Check #{check_count}")

        if services:
            results = {s: check_session(s) for s in services}
        else:
            results = check_all_sessions()

        is_healthy = print_status(results)

        if not is_healthy:
            alert_path = Path(__file__).parent / ".session_alert"
            alert_data = {
                "timestamp": timestamp,
                "services": {
                    name: {"valid": r["valid"], "login_url": r.get("login_url", "")}
                    for name, r in results.items()
                    if not r["valid"]
                },
                "vnc_url": get_vnc_url(),
            }
            alert_path.write_text(json.dumps(alert_data))

        try:
            time.sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            print("\n\n👋 Monitor stopped")
            break


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "status":
        # Check all services
        print_status()

    elif cmd == "check":
        if len(sys.argv) < 3:
            print(f"Usage: check <service>")
            print(f"Available: {', '.join(SERVICE_PROFILES.keys())}")
            sys.exit(1)
        service = sys.argv[2].lower()
        result = check_session(service)
        print_status({service: result})
        sys.exit(0 if result["valid"] else 1)

    elif cmd == "services":
        for svc in list_services():
            print(
                f"  {svc['name']:12s}  {svc['label']} ({svc['cookies_tracked']} cookies)"
            )

    elif cmd in ("login-url", "url", "login", "vnc"):
        print(get_vnc_url())

    elif cmd == "monitor":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        services = sys.argv[3:] if len(sys.argv) > 3 else None
        monitor(interval, services)

    elif cmd == "json":
        service = sys.argv[2] if len(sys.argv) > 2 else None
        if service:
            result = check_session(service)
            result["vnc_url"] = get_vnc_url()
            print(json.dumps(result, indent=2))
        else:
            results = check_all_sessions()
            output = {"vnc_url": get_vnc_url(), "services": results}
            print(json.dumps(output, indent=2))

    else:
        print(f"Unknown command: {cmd}")
        print(
            "Usage: status | check <service> | services | login-url | monitor [min] | json [service]"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
