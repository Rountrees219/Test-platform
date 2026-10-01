#!/usr/bin/env python3
"""
Token Health — Validate the active messaging channel and GitHub tokens.

Unlike health_check.py's config-level checks, this module validates the tokens
*currently in use* against their live APIs:
  - Messaging channel: delegated to the active adapter via the ABC
    (check_messaging_health() — works for Slack, WhatsApp, Teams, etc.)
  - GitHub: `gh auth status` (the session gh actually authenticated with)

Each check returns a small dict — {"service", "status", ...} — where status is
one of: "ok", "rotated", "missing", "invalid", "error". Never raises.

Usage:
    python tools/token_health.py            # human-readable
    python tools/token_health.py --json     # JSON for scripting

Python API:
    from tools.token_health import check_messaging_token, check_github_token
"""

import json
import os
import sys

from clients.token_resolver import resolve_github_token
from messaging import get_messaging_interface


def check_messaging_token() -> dict:
    """Validate the active messaging channel credentials via the ABC.

    Delegates entirely to the adapter's ``check_messaging_health()`` — no
    channel-specific logic here. The active channel is resolved from the
    ``MESSAGING_CHANNEL`` env-var (default: slack).

    Returns a dict with at minimum:
        ``service``  — channel name (e.g. "slack", "whatsapp", "teams")
        ``status``   — "ok", "rotated", "missing", "invalid", or "error"
    Never raises.
    """
    try:
        return get_messaging_interface().check_messaging_health()
    except Exception as e:
        channel = os.environ.get("MESSAGING_CHANNEL", "slack")
        return {"service": channel, "status": "error", "message": str(e)}


def check_github_token() -> dict:
    """Validate and rotate the GitHub token via the 3-tier resolver.

    Returns service/status/message; never raises.
    """
    result = resolve_github_token()
    return {"service": "github", **result}


def check_all() -> list[dict]:
    """Run all token checks and return their result dicts."""
    return [check_messaging_token(), check_github_token()]


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate messaging channel and GitHub tokens"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    results = check_all()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        icons = {
            "ok": "✅",
            "rotated": "🔄",
            "missing": "⚪",
            "invalid": "❌",
            "error": "⚠️",
        }
        for r in results:
            icon = icons.get(r["status"], "❓")
            extra = r.get("message") or r.get("team", "")
            print(
                f"  {icon} {r['service']:8s} — {r['status']}"
                + (f" ({extra})" if extra else "")
            )

    # Exit non-zero if any token is invalid or missing.
    bad = any(r["status"] in ("invalid", "missing") for r in results)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
