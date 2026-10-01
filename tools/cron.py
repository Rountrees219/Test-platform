#!/usr/bin/env python3
"""
cron — Ninja cron scheduler CLI.

Schedule recurring agent prompts that Ninja will execute through the
normal monitor batch path. Schedules evaluate in the customer's local
timezone (Ninja sets ``/etc/localtime`` from Slack).

See ``agent-docs/CRON.md`` for the full spec.

Examples:
    python tools/cron.py add \\
        --id daily-summary \\
        --schedule "0 9 * * *" \\
        --prompt "Send a daily project summary to the team."

    python tools/cron.py list
    python tools/cron.py list --json
    python tools/cron.py show daily-summary
    python tools/cron.py disable daily-summary
    python tools/cron.py enable daily-summary
    python tools/cron.py trigger daily-summary
    python tools/cron.py remove daily-summary
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Allow running as `python tools/cron.py` from src/ninja.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.cron_service as cs  # noqa: E402


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def cmd_list(args: argparse.Namespace) -> int:
    jobs = cs.load_crons()
    if args.json:
        print(json.dumps(jobs, indent=2, default=str))
        return 0
    if not jobs:
        print("No cron jobs configured. Use `cron.py add` to create one.")
        return 0
    print(f"{'ID':<24} {'ENABLED':<8} {'SCHEDULE':<16} {'NEXT RUN':<22} RUNS")
    for j in jobs:
        print(
            f"{j.get('id',''):<24} "
            f"{'yes' if j.get('enabled', True) else 'no':<8} "
            f"{j.get('schedule',''):<16} "
            f"{_fmt_ts(j.get('next_run_at')):<22} "
            f"{j.get('run_count', 0)}"
        )
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    try:
        job = cs.add_cron(
            job_id=args.id,
            schedule=args.schedule,
            prompt=args.prompt,
            thread_ts=args.thread_ts,
            enabled=not args.disabled,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(job, indent=2, default=str))
    else:
        print(f"✓ added cron {job['id']!r} — next run {_fmt_ts(job['next_run_at'])}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    job = cs.get_cron(args.id)
    if not job:
        print(f"error: cron {args.id!r} not found", file=sys.stderr)
        return 1
    print(json.dumps(job, indent=2, default=str))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    if cs.remove_cron(args.id):
        print(f"✓ removed cron {args.id!r}")
        return 0
    print(f"error: cron {args.id!r} not found", file=sys.stderr)
    return 1


def cmd_enable(args: argparse.Namespace) -> int:
    if cs.set_enabled(args.id, True):
        print(f"✓ enabled cron {args.id!r}")
        return 0
    print(f"error: cron {args.id!r} not found", file=sys.stderr)
    return 1


def cmd_disable(args: argparse.Namespace) -> int:
    if cs.set_enabled(args.id, False):
        print(f"✓ disabled cron {args.id!r}")
        return 0
    print(f"error: cron {args.id!r} not found", file=sys.stderr)
    return 1


def cmd_trigger(args: argparse.Namespace) -> int:
    if cs.trigger_cron(args.id):
        print(f"✓ cron {args.id!r} will fire on the next monitor tick")
        return 0
    print(f"error: cron {args.id!r} not found", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cron",
        description="Ninja cron scheduler — schedule recurring agent prompts.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pa = sub.add_parser("add", help="Add a new cron job")
    pa.add_argument("--id", required=True, help="Unique job id, e.g. daily-summary")
    pa.add_argument(
        "--schedule", required=True, help='5-field cron expression, e.g. "0 9 * * *"'
    )
    pa.add_argument(
        "--prompt", required=True, help="Prompt the agent will execute when due"
    )
    pa.add_argument(
        "--thread-ts", dest="thread_ts", help="Optional Slack thread to reply in"
    )
    pa.add_argument("--disabled", action="store_true", help="Create disabled")
    pa.add_argument("--json", action="store_true", help="Print job as JSON")
    pa.set_defaults(func=cmd_add)

    pl = sub.add_parser("list", help="List all cron jobs")
    pl.add_argument("--json", action="store_true", help="JSON output")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="Show a single cron job")
    ps.add_argument("id")
    ps.set_defaults(func=cmd_show)

    pr = sub.add_parser("remove", help="Delete a cron job")
    pr.add_argument("id")
    pr.set_defaults(func=cmd_remove)

    pe = sub.add_parser("enable", help="Enable a cron job")
    pe.add_argument("id")
    pe.set_defaults(func=cmd_enable)

    pd = sub.add_parser("disable", help="Disable a cron job")
    pd.add_argument("id")
    pd.set_defaults(func=cmd_disable)

    pt = sub.add_parser("trigger", help="Force a cron job to run on the next tick")
    pt.add_argument("id")
    pt.set_defaults(func=cmd_trigger)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
