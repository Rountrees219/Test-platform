#!/usr/bin/env python3
"""
issues — Ninja GitHub-issue work-queue CLI.

Ninja uses GitHub Issues as its durable work queue (the "memory/state"
spine of the agent loop):

  * The monitor turns substantial Slack/cron requests into issues and, when
    there is open work and the orchestrator is idle, launches the orchestrator.
  * The orchestrator works open issues (phase 1), then reflects and files new
    follow-up issues (phase 2).

This tool is a thin wrapper around the ``gh`` CLI so Claude Code (and the
Python services) have one consistent, scriptable surface for issue work.
See ``agent-docs/LOOP.md`` for the full architecture.

Examples:
    python tools/issues.py list
    python tools/issues.py list --json
    python tools/issues.py count
    python tools/issues.py create --title "Fix X" --body "details" --label ninja
    python tools/issues.py comment 42 --body "progress update"
    python tools/issues.py block 42 --comment "needs prod API access"
    python tools/issues.py unblock 42
    python tools/issues.py close 42 --comment "done in PR #99"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# The stop hook writes this when reflect starts. One issue per phase, or
# reflect refills the queue as fast as the orchestrator drains it.
ISSUE_PERMIT_FILE = Path(__file__).resolve().parent.parent / "cycle_issue_permit"

# Label Ninja stamps on every issue it files so the loop can distinguish
# its own work queue from human-filed issues if desired.
NINJA_LABEL = "ninja"

# Label that marks an issue as blocked: Ninja attempted it but cannot progress
# (missing access, external dependency, waiting on a human). Blocked issues are
# excluded from the work queue and re-checked periodically by the orchestrator.
BLOCKED_LABEL = "blocked"


def _gh(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a ``gh`` command and return the completed process."""
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def list_issues(state: str = "open", label: str | None = None) -> list[dict]:
    """Return issues as a list of dicts (number, title, labels, url, state)."""
    args = [
        "issue",
        "list",
        "--state",
        state,
        "--limit",
        "200",
        "--json",
        "number,title,labels,url,state,body,createdAt",
    ]
    if label:
        args += ["--label", label]
    proc = _gh(args)
    return json.loads(proc.stdout or "[]")


def count_open(label: str | None = None) -> int:
    """Return the number of open issues (optionally filtered by label)."""
    return len(list_issues(state="open", label=label))


def _is_blocked(issue: dict) -> bool:
    return any(lbl["name"] == BLOCKED_LABEL for lbl in issue.get("labels", []))


def count_actionable() -> int:
    """Return the number of open issues that are NOT blocked (the work queue)."""
    return sum(1 for it in list_issues(state="open") if not _is_blocked(it))


def count_blocked() -> int:
    """Return the number of open issues labelled blocked."""
    return sum(1 for it in list_issues(state="open") if _is_blocked(it))


def block_issue(number: int, comment: str) -> None:
    """Mark an issue blocked: add the label and explain why in a comment."""
    # Ensure the label exists (--force makes this idempotent); tolerate failure.
    _gh(
        [
            "label",
            "create",
            BLOCKED_LABEL,
            "--color",
            "B60205",
            "--description",
            "Ninja cannot progress this issue",
            "--force",
        ],
        check=False,
    )
    _gh(["issue", "edit", str(number), "--add-label", BLOCKED_LABEL])
    comment_issue(number, f"\U0001f6a7 BLOCKED: {comment}")


def unblock_issue(number: int, comment: str | None = None) -> None:
    """Remove the blocked label so the issue rejoins the work queue."""
    _gh(["issue", "edit", str(number), "--remove-label", BLOCKED_LABEL])
    if comment:
        comment_issue(number, comment)


def create_issue(title: str, body: str = "", labels: list[str] | None = None) -> str:
    """Create an issue and return its URL."""
    args = ["issue", "create", "--title", title, "--body", body or title]
    for lab in labels or [NINJA_LABEL]:
        args += ["--label", lab]
    # Tolerate a missing label (repos without the 'ninja' label preconfigured)
    proc = _gh(args, check=False)
    if proc.returncode != 0:
        # Retry once without labels if the label doesn't exist.
        retry = _gh(
            ["issue", "create", "--title", title, "--body", body or title],
            check=True,
        )
        return retry.stdout.strip()
    return proc.stdout.strip()


def comment_issue(number: int, body: str) -> None:
    _gh(["issue", "comment", str(number), "--body", body])


def close_issue(number: int, comment: str | None = None) -> None:
    args = ["issue", "close", str(number)]
    if comment:
        args += ["--comment", comment]
    _gh(args)


def _cmd_list(a: argparse.Namespace) -> int:
    issues = list_issues(state=a.state, label=a.label)
    if a.json:
        print(json.dumps(issues, indent=2))
        return 0
    if not issues:
        print(f"No {a.state} issues.")
        return 0
    for it in issues:
        labels = ",".join(lbl["name"] for lbl in it.get("labels", []))
        print(f"#{it['number']:<5} [{it['state']}] {it['title']}  ({labels})")
    return 0


def _cmd_count(a: argparse.Namespace) -> int:
    n = count_open(label=a.label)
    if a.json:
        print(json.dumps({"open": n}))
    else:
        print(n)
    return 0


def _spend_issue_permit() -> bool:
    """True if an issue may be filed now.
    Deleting the permit is the check, so two creates can't both win it.
    """
    if os.environ.get("NINJA_CYCLE_RUN") != "1":
        return True  # only orchestrator cycles are capped
    try:
        ISSUE_PERMIT_FILE.unlink()
    except OSError:
        return False
    return True


def _cmd_create(a: argparse.Namespace) -> int:
    if not _spend_issue_permit():
        sys.stderr.write(
            "Refused: this cycle has already filed its one issue (or is not in "
            "the reflect phase). Keep the idea in your memory file instead — "
            "the next run can file it. Do not work around this with a direct "
            "`gh issue create`.\n"
        )
        return 1
    labels = a.label or [NINJA_LABEL]
    url = create_issue(a.title, a.body, labels)
    print(url)
    return 0


def _cmd_comment(a: argparse.Namespace) -> int:
    comment_issue(a.number, a.body)
    print(f"Commented on #{a.number}")
    return 0


def _cmd_block(a: argparse.Namespace) -> int:
    block_issue(a.number, a.comment)
    print(f"Blocked #{a.number}")
    return 0


def _cmd_unblock(a: argparse.Namespace) -> int:
    unblock_issue(a.number, a.comment)
    print(f"Unblocked #{a.number}")
    return 0


def _cmd_close(a: argparse.Namespace) -> int:
    close_issue(a.number, a.comment)
    print(f"Closed #{a.number}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="issues",
        description="Ninja GitHub-issue work-queue CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("list", help="List issues")
    pl.add_argument("--state", default="open", choices=["open", "closed", "all"])
    pl.add_argument("--label", default=None, help="Filter by label")
    pl.add_argument("--json", action="store_true", help="Output JSON")
    pl.set_defaults(func=_cmd_list)

    pc = sub.add_parser("count", help="Count open issues")
    pc.add_argument("--label", default=None, help="Filter by label")
    pc.add_argument("--json", action="store_true", help="Output JSON")
    pc.set_defaults(func=_cmd_count)

    pcr = sub.add_parser("create", help="Create an issue")
    pcr.add_argument("--title", required=True)
    pcr.add_argument("--body", default="")
    pcr.add_argument(
        "--label", action="append", help="Label (repeatable); defaults to 'ninja'"
    )
    pcr.set_defaults(func=_cmd_create)

    pcm = sub.add_parser("comment", help="Comment on an issue")
    pcm.add_argument("number", type=int)
    pcm.add_argument("--body", required=True)
    pcm.set_defaults(func=_cmd_comment)

    pb = sub.add_parser("block", help="Mark an issue blocked (cannot progress)")
    pb.add_argument("number", type=int)
    pb.add_argument("--comment", required=True, help="Why blocked + what is needed")
    pb.set_defaults(func=_cmd_block)

    pu = sub.add_parser("unblock", help="Return a blocked issue to the work queue")
    pu.add_argument("number", type=int)
    pu.add_argument("--comment", default=None, help="Optional comment")
    pu.set_defaults(func=_cmd_unblock)

    pcl = sub.add_parser("close", help="Close an issue")
    pcl.add_argument("number", type=int)
    pcl.add_argument("--comment", default=None, help="Optional closing comment")
    pcl.set_defaults(func=_cmd_close)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except subprocess.CalledProcessError as e:
        sys.stderr.write((e.stderr or str(e)) + "\n")
        return e.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
