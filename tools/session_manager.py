#!/usr/bin/env python3
"""
Session Manager — Save, restore, and list browser cookie snapshots.

Manages browser session state by exporting/importing cookies from the
persistent Chromium browser. Useful for preserving login states across
browser restarts or switching between different account profiles.

Usage:
    python tools/session_manager.py list                    # List saved sessions
    python tools/session_manager.py save <name>             # Save current session
    python tools/session_manager.py restore <name>          # Restore a session
    python tools/session_manager.py delete <name>           # Delete a session
    python tools/session_manager.py export <name> <file>    # Export to JSON file
    python tools/session_manager.py info <name>             # Show session details

Python API:
    from tools.session_manager import save_session, restore_session, list_sessions
    save_session("my_login")
    sessions = list_sessions()
    restore_session("my_login")
"""

import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = REPO_ROOT / "ninja" / "sessions"
BROWSER_DATA_DIR = REPO_ROOT / "ninja" / "browser_data"
COOKIES_DB = BROWSER_DATA_DIR / "Default" / "Cookies"


def _ensure_sessions_dir():
    """Create sessions directory if it doesn't exist."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _get_session_path(name: str) -> Path:
    """Get the path for a named session."""
    return SESSIONS_DIR / f"{name}.json"


def _copy_cookies_db() -> Path:
    """Copy the Cookies database to a temp location (Chrome locks it)."""
    if not COOKIES_DB.exists():
        raise FileNotFoundError(f"Cookies database not found: {COOKIES_DB}")

    tmp_path = SESSIONS_DIR / "_tmp_cookies.db"
    shutil.copy2(str(COOKIES_DB), str(tmp_path))
    return tmp_path


def _read_cookies_from_db(db_path: Path) -> list:
    """Read cookies from a Chromium Cookies SQLite database."""
    cookies = []
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        cursor.execute("""
            SELECT host_key, name, path, expires_utc, is_secure, is_httponly,
                   samesite, creation_utc, last_access_utc
            FROM cookies
            ORDER BY host_key, name
        """)

        for row in cursor.fetchall():
            cookies.append(
                {
                    "host": row[0],
                    "name": row[1],
                    "path": row[2],
                    "expires_utc": row[3],
                    "secure": bool(row[4]),
                    "httponly": bool(row[5]),
                    "samesite": row[6],
                    "creation_utc": row[7],
                    "last_access_utc": row[8],
                }
            )

        conn.close()
    except sqlite3.Error as e:
        raise RuntimeError(f"Cannot read cookies database: {e}")

    return cookies


def save_session(name: str, description: str = "") -> dict:
    """
    Save current browser session (cookies) to a named snapshot.

    Args:
        name: Session name (alphanumeric + underscores)
        description: Optional description of what this session contains

    Returns:
        Dict with session metadata
    """
    _ensure_sessions_dir()

    session_path = _get_session_path(name)

    try:
        # Copy and read cookies
        tmp_db = _copy_cookies_db()
        cookies = _read_cookies_from_db(tmp_db)
        tmp_db.unlink(missing_ok=True)
    except (FileNotFoundError, RuntimeError) as e:
        return {"error": str(e)}

    # Group cookies by domain for summary
    domains = {}
    for cookie in cookies:
        host = cookie["host"]
        domains[host] = domains.get(host, 0) + 1

    session_data = {
        "name": name,
        "description": description,
        "created": datetime.now().isoformat(),
        "cookie_count": len(cookies),
        "domains": domains,
        "cookies": cookies,
    }

    with open(session_path, "w") as f:
        json.dump(session_data, f, indent=2)

    return {
        "status": "ok",
        "message": f"Session '{name}' saved with {len(cookies)} cookies across {len(domains)} domains",
        "path": str(session_path),
        "cookie_count": len(cookies),
        "domain_count": len(domains),
    }


def restore_session(name: str) -> dict:
    """
    Restore a saved session by injecting cookies into the browser.

    Note: This uses Playwright's cookie API to set cookies in the running browser.
    The browser must be running and accessible.

    Args:
        name: Session name to restore

    Returns:
        Dict with restoration status
    """
    session_path = _get_session_path(name)
    if not session_path.exists():
        return {
            "error": f"Session '{name}' not found. Use 'list' to see available sessions."
        }

    try:
        with open(session_path) as f:
            session_data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return {"error": f"Cannot read session file: {e}"}

    cookies = session_data.get("cookies", [])
    if not cookies:
        return {"error": "Session contains no cookies"}

    # Connect to browser and set cookies
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from browser.browser_interface import BrowserInterface

        browser = BrowserInterface.connect_cdp()

        # Convert our cookie format to Playwright format
        pw_cookies = []
        for c in cookies:
            pw_cookie = {
                "name": c["name"],
                "domain": c["host"],
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httponly", False),
            }

            # Set expiry (Chromium uses microseconds since 1601-01-01)
            expires = c.get("expires_utc", 0)
            if expires and expires > 0:
                # Convert Chromium epoch to Unix epoch
                # Chromium epoch = Jan 1, 1601; Unix = Jan 1, 1970
                # Difference: 11644473600 seconds
                unix_expires = (expires / 1_000_000) - 11644473600
                if unix_expires > time.time():  # Only set future expiry
                    pw_cookie["expires"] = unix_expires

            # sameSite mapping
            samesite_map = {0: "None", 1: "Lax", 2: "Strict"}
            pw_cookie["sameSite"] = samesite_map.get(c.get("samesite", 0), "None")

            pw_cookies.append(pw_cookie)

        # Add cookies to browser context
        browser.context.add_cookies(pw_cookies)
        browser.stop()

        return {
            "status": "ok",
            "message": f"Restored {len(pw_cookies)} cookies from session '{name}'",
            "cookie_count": len(pw_cookies),
        }

    except ImportError:
        return {"error": "Cannot import BrowserInterface. Run from project root."}
    except Exception as e:
        return {"error": f"Failed to restore cookies: {e}"}


def list_sessions() -> dict:
    """
    List all saved sessions.

    Returns:
        Dict with list of session summaries
    """
    _ensure_sessions_dir()

    sessions = []
    for f in sorted(SESSIONS_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue  # Skip temp files
        try:
            with open(f) as fh:
                data = json.load(fh)
            sessions.append(
                {
                    "name": data.get("name", f.stem),
                    "description": data.get("description", ""),
                    "created": data.get("created", "unknown"),
                    "cookie_count": data.get("cookie_count", 0),
                    "domain_count": len(data.get("domains", {})),
                    "file": str(f),
                }
            )
        except (json.JSONDecodeError, IOError):
            sessions.append({"name": f.stem, "error": "Cannot read session file"})

    return {"sessions": sessions, "count": len(sessions)}


def delete_session(name: str) -> dict:
    """Delete a saved session."""
    session_path = _get_session_path(name)
    if not session_path.exists():
        return {"error": f"Session '{name}' not found"}

    session_path.unlink()
    return {"status": "ok", "message": f"Session '{name}' deleted"}


def session_info(name: str) -> dict:
    """Get detailed info about a saved session."""
    session_path = _get_session_path(name)
    if not session_path.exists():
        return {"error": f"Session '{name}' not found"}

    try:
        with open(session_path) as f:
            data = json.load(f)

        # Remove raw cookies for display
        info = {k: v for k, v in data.items() if k != "cookies"}
        info["top_domains"] = dict(
            sorted(data.get("domains", {}).items(), key=lambda x: -x[1])[:20]
        )
        return info
    except (json.JSONDecodeError, IOError) as e:
        return {"error": f"Cannot read session: {e}"}


def print_sessions(result: dict):
    """Pretty-print session list."""
    sessions = result.get("sessions", [])
    if not sessions:
        print("\n  📭 No saved sessions. Use 'save <name>' to create one.\n")
        return

    print(f"\n{'=' * 60}")
    print(f"📦 SAVED SESSIONS ({result['count']})")
    print(f"{'=' * 60}")

    for s in sessions:
        if "error" in s:
            print(f"\n  ❌ {s['name']} — {s['error']}")
            continue
        print(f"\n  📋 {s['name']}")
        if s.get("description"):
            print(f"     {s['description']}")
        print(f"     Created: {s['created']}")
        print(f"     Cookies: {s['cookie_count']} across {s['domain_count']} domains")

    print(f"\n{'=' * 60}\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Session Manager — Save/restore browser cookie snapshots",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/session_manager.py list                List saved sessions
  python tools/session_manager.py save twitter_login  Save current session
  python tools/session_manager.py restore twitter     Restore a session
  python tools/session_manager.py info twitter_login  Show session details
  python tools/session_manager.py delete old_session  Delete a session
        """,
    )
    parser.add_argument(
        "command",
        choices=["list", "save", "restore", "delete", "info"],
        help="Command to execute",
    )
    parser.add_argument("name", nargs="?", help="Session name")
    parser.add_argument(
        "--description", "-d", default="", help="Session description (for save)"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    if args.command == "list":
        result = list_sessions()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print_sessions(result)

    elif args.command == "save":
        if not args.name:
            parser.error("'save' requires a session name")
        result = save_session(args.name, args.description)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            icon = "✅" if result.get("status") == "ok" else "❌"
            print(
                f"\n  {icon} {result.get('message', result.get('error', 'Unknown error'))}\n"
            )

    elif args.command == "restore":
        if not args.name:
            parser.error("'restore' requires a session name")
        result = restore_session(args.name)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            icon = "✅" if result.get("status") == "ok" else "❌"
            print(
                f"\n  {icon} {result.get('message', result.get('error', 'Unknown error'))}\n"
            )

    elif args.command == "delete":
        if not args.name:
            parser.error("'delete' requires a session name")
        result = delete_session(args.name)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            icon = "✅" if result.get("status") == "ok" else "❌"
            print(
                f"\n  {icon} {result.get('message', result.get('error', 'Unknown error'))}\n"
            )

    elif args.command == "info":
        if not args.name:
            parser.error("'info' requires a session name")
        result = session_info(args.name)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if "error" in result:
                print(f"\n  ❌ {result['error']}\n")
            else:
                print(f"\n{'=' * 60}")
                print(f"📋 SESSION: {result.get('name', 'unknown')}")
                print(f"{'=' * 60}")
                print(f"  Description: {result.get('description', 'none')}")
                print(f"  Created: {result.get('created', 'unknown')}")
                print(f"  Cookies: {result.get('cookie_count', 0)}")
                print(f"\n  Top domains:")
                for domain, count in result.get("top_domains", {}).items():
                    print(f"    {domain:40s} {count} cookies")
                print(f"\n{'=' * 60}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
