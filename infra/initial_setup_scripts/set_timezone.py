#!/usr/bin/env python3
"""
set_timezone.py
===============

Detect the messaging user's IANA timezone and apply it as the Linux system
timezone. Intended to be run once, early in a sandbox's lifecycle
(alongside the other Ninja bootstrap steps), so all subsequent
timestamps — logs, scheduled jobs, Slack messages, GitHub commits —
line up with the operator's local time.

How it works
------------

Slack (default): reads the Slack user token from ``/dev/shm/mcp-token``,
calls ``auth.test`` + ``users.info`` for the profile ``tz``. If that
fails, falls back to ``user_timezone`` in ``sandbox_metadata.json``.

Teams / WhatsApp (``--channel teams`` or ``--channel whatsapp``): reads
``user_timezone`` from ``/dev/shm/sandbox_metadata.json`` (set by Super at
deploy from the operator's browser).

Both paths then:

1. Applies the timezone by symlinking ``/etc/localtime`` to
   ``/usr/share/zoneinfo/<zone>`` and writing ``<zone>`` to
   ``/etc/timezone``. We deliberately do NOT use
   ``timedatectl set-timezone``: in containers without a real RTC it
   rewrites the system wall clock (jumps it by the new zone's UTC
   offset) on the first TZ change from boot, which breaks every TTL,
   JWT expiry, S3 signature and cron schedule downstream.
2. Verifies the change and prints the resulting ``date`` output.

CLI
---

::

    python src/ninja/initial_setup_scripts/set_timezone.py           # Slack detect + apply
    python src/ninja/initial_setup_scripts/set_timezone.py --channel teams
    python src/ninja/initial_setup_scripts/set_timezone.py --channel whatsapp
    python src/ninja/initial_setup_scripts/set_timezone.py --dry-run # detect only, don't touch the system
    python src/ninja/initial_setup_scripts/set_timezone.py --timezone Europe/Berlin
                                                           # override detection
    python src/ninja/initial_setup_scripts/set_timezone.py --quiet   # minimal output

Exit codes
----------

  0 — timezone was detected AND successfully applied (or --dry-run succeeded)
  1 — could not resolve a timezone (missing Slack token or sandbox metadata)
  2 — Slack API call failed
  3 — timezone is not a valid IANA zone on this host
  4 — could not apply the timezone (permissions, missing tzdata, …)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

MCP_TOKEN_FILE = Path("/dev/shm/mcp-token")
SANDBOX_METADATA_PATH = Path("/dev/shm/sandbox_metadata.json")
ZONEINFO_ROOT = Path("/usr/share/zoneinfo")
LOCALTIME_PATH = Path("/etc/localtime")
ETC_TIMEZONE = Path("/etc/timezone")
SLACK_API = "https://slack.com/api"


# ---------------------------------------------------------------------------
# Slack token + API helpers (kept inline so this script has no dependencies
# beyond Python's stdlib — bootstrap scripts must be runnable before the
# Ninja package itself is installed).
# ---------------------------------------------------------------------------


@dataclass
class SlackUserTZ:
    user_id: str
    name: str
    real_name: str
    tz: str
    tz_label: str
    tz_offset: int


def _read_slack_token() -> Optional[str]:
    """
    Read the Slack user token from ``/dev/shm/mcp-token``.

    The file format is one ``Key=<json>`` record per line. We only look at
    the ``Slack=`` record and return its ``access_token``.
    """
    if not MCP_TOKEN_FILE.exists():
        return None
    try:
        for line in MCP_TOKEN_FILE.read_text().splitlines():
            if line.startswith("Slack="):
                data = json.loads(line[len("Slack=") :])
                token = data.get("access_token", "").strip()
                return token or None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _slack_call(method: str, token: str, **params: Any) -> Dict[str, Any]:
    """POST to a Slack Web API method and return the decoded JSON."""
    url = f"{SLACK_API}/{method}"
    data = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    ).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"http_{e.code}", "detail": e.reason}
    except urllib.error.URLError as e:
        return {"ok": False, "error": "network_error", "detail": str(e)}


def get_slack_user_tz(token: str) -> SlackUserTZ:
    """
    Resolve the Slack user bound to ``token`` and return their timezone.
    Raises ``RuntimeError`` on any API failure.
    """
    auth = _slack_call("auth.test", token)
    if not auth.get("ok"):
        raise RuntimeError(f"Slack auth.test failed: {auth.get('error')}")
    user_id = auth.get("user_id")
    if not user_id:
        raise RuntimeError("Slack auth.test returned no user_id")

    info = _slack_call("users.info", token, user=user_id)
    if not info.get("ok"):
        raise RuntimeError(f"Slack users.info failed: {info.get('error')}")

    u = info["user"]
    tz = (u.get("tz") or "").strip()
    if not tz:
        raise RuntimeError(f"Slack user {user_id} has no timezone set in their profile")

    return SlackUserTZ(
        user_id=user_id,
        name=u.get("name") or "",
        real_name=u.get("real_name") or u.get("name") or "",
        tz=tz,
        tz_label=(u.get("tz_label") or "").strip(),
        tz_offset=int(u.get("tz_offset") or 0),
    )


def get_teams_user_tz() -> str:
    """Return IANA timezone from sandbox_metadata ``user_timezone``."""
    try:
        meta = json.loads(SANDBOX_METADATA_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"Could not read {SANDBOX_METADATA_PATH}: {e}. "
            "Pass --timezone <IANA> to skip metadata detection."
        ) from e

    tz = (meta.get("user_timezone") or "").strip()
    if not tz:
        raise RuntimeError(
            f"No user_timezone in {SANDBOX_METADATA_PATH}. "
            "Pass --timezone <IANA> to skip metadata detection."
        )
    return tz


# ---------------------------------------------------------------------------
# Timezone application
# ---------------------------------------------------------------------------


def validate_iana_zone(tz: str) -> bool:
    """Return True if ``tz`` resolves to a real zoneinfo file."""
    zone_path = ZONEINFO_ROOT / tz
    # Guard against traversal and symlinks outside /usr/share/zoneinfo
    try:
        resolved = zone_path.resolve()
    except OSError:
        return False
    return zone_path.is_file() and str(resolved).startswith(
        str(ZONEINFO_ROOT.resolve())
    )


def apply_timezone(tz: str) -> None:
    """
    Apply ``tz`` to the running Linux system by swapping the
    ``/etc/localtime`` symlink and writing ``/etc/timezone``. Raises
    ``RuntimeError`` on failure.

    We deliberately do NOT use ``timedatectl set-timezone``: in
    containers without a real RTC, ``systemd-timedated`` rewrites
    ``CLOCK_REALTIME`` on the first TZ change from boot, jumping the
    wall clock by the new zone's UTC offset and breaking every TTL,
    JWT expiry, S3 signature and cron schedule downstream.
    """
    zone_file = ZONEINFO_ROOT / tz
    if not zone_file.is_file():
        raise RuntimeError(
            f"Failed to apply timezone '{tz}' — {zone_file} not found. "
            f"Install tzdata."
        )
    try:
        # Replace /etc/localtime atomically.
        tmp_link = LOCALTIME_PATH.with_suffix(".tmp")
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(zone_file)
        os.replace(tmp_link, LOCALTIME_PATH)
        ETC_TIMEZONE.write_text(tz + "\n")
    except (OSError, PermissionError) as e:
        raise RuntimeError(
            f"Failed to apply timezone '{tz}' — could not write "
            f"/etc/localtime ({e}). Re-run with sudo."
        ) from e


def current_system_tz() -> str:
    """
    Best-effort readback of the active system timezone.

    Prefers the ``/etc/localtime`` symlink (which every modern init
    writes, including systemd, busybox, and our fallback path) and only
    falls back to ``/etc/timezone`` if the symlink isn't usable. This
    matters because ``timedatectl set-timezone`` rewrites
    ``/etc/localtime`` but may leave a stale ``/etc/timezone`` behind on
    some distros, which would otherwise lie about the active zone.
    """
    # 1. /etc/localtime -> .../zoneinfo/<zone> is the ground truth.
    try:
        if LOCALTIME_PATH.is_symlink():
            target = os.readlink(LOCALTIME_PATH)
            # Resolve relative symlinks (e.g. "../usr/share/zoneinfo/UTC")
            # against /etc/ so we can slice off the zoneinfo prefix cleanly.
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(LOCALTIME_PATH.parent, target))
            marker = "zoneinfo/"
            idx = target.find(marker)
            if idx != -1:
                zone = target[idx + len(marker) :]
                if zone:
                    return zone
    except OSError:
        pass

    # 2. Fall back to /etc/timezone if it exists and is non-empty.
    if ETC_TIMEZONE.is_file():
        try:
            val = ETC_TIMEZONE.read_text().strip()
            if val:
                return val
        except OSError:
            pass

    return "unknown"


def current_date_line() -> str:
    """Human-readable confirmation line, e.g. ``Thu May 21 22:20:51 AEST 2026``."""
    import time as _t

    _t.tzset()  # pick up the new /etc/localtime in this process
    return _t.strftime("%a %b %e %H:%M:%S %Z %Y")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="set_timezone.py",
        description=(
            "Detect the messaging user's timezone and apply it as the Linux "
            "system timezone. Part of the Ninja initial-setup bootstrap."
        ),
    )
    p.add_argument(
        "--channel",
        choices=("slack", "teams", "whatsapp"),
        default="slack",
        help=(
            "Detection source: Slack profile (default), or Teams/WhatsApp "
            "sandbox metadata user_timezone."
        ),
    )
    p.add_argument(
        "--timezone",
        "-t",
        help="Override detection and apply this IANA zone directly "
        "(e.g. `Australia/Canberra`).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect the timezone and print what would happen, but don't "
        "modify the system.",
    )
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Only print errors and the final applied zone.",
    )
    return p


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


def _try_metadata_user_timezone(quiet: bool) -> Optional[str]:
    """Read deploy-time browser TZ from sandbox metadata (Slack fallback only)."""
    _log("▶ Falling back to sandbox metadata user_timezone…", quiet)
    try:
        tz = get_teams_user_tz()
    except RuntimeError:
        return None
    _log(f"  • IANA zone  : {tz}", quiet)
    return tz


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    quiet = args.quiet

    # -----------------------------------------------------------------
    # 1. Resolve the target zone (CLI override wins over channel detection)
    # -----------------------------------------------------------------
    tz: str

    if args.timezone:
        tz = args.timezone.strip()
        _log(f"▶ Using explicit timezone: {tz}", quiet)
    elif args.channel in ("teams", "whatsapp"):
        _log("▶ Detecting timezone from sandbox metadata…", quiet)
        try:
            tz = get_teams_user_tz()
        except RuntimeError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        _log(f"  • IANA zone  : {tz}", quiet)
    else:
        token = _read_slack_token()
        if not token:
            tz = _try_metadata_user_timezone(quiet)
            if tz is None:
                print(
                    "✗ No Slack token found in /dev/shm/mcp-token. "
                    "Pass --timezone <IANA> to skip Slack detection.",
                    file=sys.stderr,
                )
                return 1
        else:
            _log("▶ Detecting timezone from Slack user profile…", quiet)
            try:
                detected = get_slack_user_tz(token)
            except RuntimeError as e:
                tz = _try_metadata_user_timezone(quiet)
                if tz is None:
                    print(f"✗ Slack API error: {e}", file=sys.stderr)
                    return 2
            else:
                tz = detected.tz
                _log(
                    f"  • Slack user : {detected.name} ({detected.user_id})\n"
                    f"  • IANA zone  : {tz}\n"
                    f"  • Label      : {detected.tz_label}\n"
                    f"  • UTC offset : {detected.tz_offset:+d} seconds "
                    f"({detected.tz_offset // 3600:+d}h)",
                    quiet,
                )

    # -----------------------------------------------------------------
    # 2. Validate the zone against the host's tzdata
    # -----------------------------------------------------------------
    if not validate_iana_zone(tz):
        print(
            f"✗ '{tz}' is not a valid IANA zone on this host "
            f"(missing {ZONEINFO_ROOT / tz}). Install tzdata and retry.",
            file=sys.stderr,
        )
        return 3

    # -----------------------------------------------------------------
    # 3. Apply (or skip, on --dry-run)
    # -----------------------------------------------------------------
    if args.dry_run:
        _log(f"▶ --dry-run: would apply timezone '{tz}'. Not modifying system.", quiet)
        _log(f"  Current system tz: {current_system_tz()}", quiet)
        return 0

    current = current_system_tz()
    if current == tz:
        _log(f"✓ System timezone is already '{tz}' — no change needed.", quiet)
        _log(f"  {current_date_line()}", quiet)
        return 0

    _log(f"▶ Applying timezone '{tz}' (was '{current}')…", quiet)
    try:
        apply_timezone(tz)
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 4

    new_tz = current_system_tz()
    _log(
        f"✓ Timezone set to '{new_tz}'.\n" f"  {current_date_line()}",
        quiet,
    )
    # Even in --quiet we print the one canonical confirmation line
    if quiet:
        print(new_tz)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
