#!/usr/bin/env python3
"""
pause — stop Ninja's background work, or start it again.

Turns off the orchestrator and every cron job. The monitor keeps listening, so
Ninja still answers when spoken to, and resumes when asked.

The sandbox keeps running and keeps costing either way — only the main UI's
Pause stops the machine.

    python tools/pause.py pause
    python tools/pause.py resume
    python tools/pause.py status [--json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# Lets this run as `python tools/pause.py` from src/ninja.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.cron_service as cs
from clients.super_ninja_client import get_project_id, get_super_ninja_url
from core.config import (
    is_orchestrator_enabled,
    load_orchestrator_config,
    save_orchestrator_config,
)
from processes.orchestrator import ORCHESTRATOR_SERVICE, is_orchestrator_running

# Which crons we switched off, so resume restores those and leaves alone any the
# user had already disabled. Stored with the flag, outside the repo.
PAUSED_CRONS_KEY = "paused_cron_ids"

STOP_CONFIRM_SECONDS = 15


def _main_ui_url() -> str:
    """Link straight to the project; sandboxes too old to know theirs get the list."""
    project_id = get_project_id()
    path = f"/projects/{project_id}" if project_id else "/ai-workspace"
    return get_super_ninja_url() + path


def _stop_orchestrator_service() -> bool:
    """Stop the run already in flight, and check that it really stopped.

    The exit code only says systemd took the request; the kill lands later. So
    poll the same state the monitor reads instead of trusting it.
    """
    try:
        subprocess.run(
            ["systemctl", "stop", ORCHESTRATOR_SERVICE],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for _ in range(STOP_CONFIRM_SECONDS):
        if not is_orchestrator_running():
            return True
        time.sleep(1)
    return False


def _disable_enabled_crons() -> list[str]:
    stopped = []
    for job in cs.load_crons():
        if job.get("enabled") and cs.set_enabled(job["id"], False):
            stopped.append(job["id"])
    return stopped


def _restore_crons(job_ids: list[str]) -> tuple[list[str], int]:
    """Switch our crons back on. Returns the ids, and how many slots went by.

    A slot that passed while paused is not replayed — reminding someone about a
    meeting that already happened is noise. Those are moved to their next slot.
    A job whose slot is still ahead keeps it and fires on time.
    """
    now = time.time()
    wanted = set(job_ids)
    jobs = cs.load_crons()
    restored, missed = [], 0
    for job in jobs:
        if job.get("id") not in wanted:
            continue
        job["enabled"] = True
        next_run_at = job.get("next_run_at")
        if next_run_at is None or float(next_run_at) <= now:
            job["next_run_at"] = cs.calculate_next_run(job["schedule"], now)
            if next_run_at is not None:
                missed += 1
        restored.append(job["id"])
    if restored:
        cs.save_crons(jobs)
    return restored, missed


def cmd_pause(args: argparse.Namespace) -> int:
    # Flag before stop. Stop it while still enabled and the monitor's next poll
    # starts it straight back up.
    save_orchestrator_config({"enabled": False})
    stopped_crons = _disable_enabled_crons()
    config = load_orchestrator_config()
    already = [c for c in config.get(PAUSED_CRONS_KEY, []) if c not in stopped_crons]
    save_orchestrator_config({PAUSED_CRONS_KEY: already + stopped_crons})
    service_stopped = _stop_orchestrator_service()
    workspace_url = _main_ui_url()

    if args.json:
        print(
            json.dumps(
                {
                    "paused": True,
                    "crons_stopped": stopped_crons,
                    "orchestrator_service_stopped": service_stopped,
                    "workspace_url": workspace_url,
                },
                indent=2,
            )
        )
        return 0
    tail = "" if service_stopped else " (in-flight run may finish)"
    print("⏸️  Background work paused")
    print(f"   orchestrator: off{tail}")
    print(f"   cron jobs stopped: {len(stopped_crons)}")
    print("   monitor: still listening — Ninja answers and can be resumed")
    print(f"   main UI link: {workspace_url}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    config = load_orchestrator_config()
    restored, missed = _restore_crons(config.get(PAUSED_CRONS_KEY, []))
    save_orchestrator_config({"enabled": True, PAUSED_CRONS_KEY: []})
    from processes.common import maybe_launch_orchestrator

    launched = maybe_launch_orchestrator()

    if args.json:
        print(
            json.dumps(
                {
                    "paused": False,
                    "crons_restored": restored,
                    "crons_whose_slot_passed": missed,
                    "orchestrator_launched": launched,
                },
                indent=2,
            )
        )
        return 0
    note = f" ({missed} missed their slot — skipped, not replayed)" if missed else ""
    print("▶️  Background work resumed")
    print(f"   cron jobs restored: {len(restored)}{note}")
    print(f"   orchestrator: {'started' if launched else 'idle — nothing queued'}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_orchestrator_config()
    enabled = is_orchestrator_enabled(config)
    # The flag alone would call it stopped while a run is still finishing.
    running = is_orchestrator_running()
    crons_enabled = sum(1 for j in cs.load_crons() if j.get("enabled"))

    if args.json:
        print(
            json.dumps(
                {
                    "paused": not enabled,
                    "orchestrator_enabled": enabled,
                    "orchestrator_running": running,
                    "crons_enabled": crons_enabled,
                    "crons_paused_by_us": config.get(PAUSED_CRONS_KEY, []),
                },
                indent=2,
            )
        )
        return 0
    print("running" if enabled else "paused")
    state = "on" if enabled else "off"
    if running and not enabled:
        state += " — but a run is still finishing"
    print(f"   orchestrator: {state}")
    print(f"   cron jobs still enabled: {crons_enabled}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pause or resume Ninja's background work"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pp = sub.add_parser("pause", help="Stop the orchestrator and all cron jobs")
    pp.set_defaults(func=cmd_pause)

    pr = sub.add_parser("resume", help="Start background work again")
    pr.set_defaults(func=cmd_resume)

    ps = sub.add_parser("status", help="Report what is paused")
    ps.set_defaults(func=cmd_status)

    for p in (pp, pr, ps):
        p.add_argument("--json", action="store_true", help="Machine-readable output")

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
