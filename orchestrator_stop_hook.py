#!/usr/bin/env python3
"""Keeps the orchestrator's Claude working until the issue queue is empty.

Claude Code runs this script every time the agent tries to stop. We check
GitHub and answer one of two ways: print {"decision": "block", "reason":
"<next instruction>"} and Claude keeps going in the SAME process (no relaunch,
no prompt-cache re-write) — or print nothing, which lets it stop.

Only launches armed with NINJA_CYCLE_RUN=1 are affected (see run_agent());
monitor batches and --task runs are left alone.

Without a depth cap a run never ends: reflect files new issues, and a non-empty
queue is what makes this hook start the next cycle.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
# Not /tmp: the sandbox wipes that mid-run and the depth count would restart.
STATE_FILE = REPO_ROOT / "cycle_state.json"
# We write it, issues.py deletes it to file one issue.
ISSUE_PERMIT_FILE = REPO_ROOT / "cycle_issue_permit"
LOG_FILE = Path("/workspace/logs/stop_hook.log")

# Issues one run may work before wrapping up. The monitor starts a fresh run
# if the queue still has work.
ORCHESTRATOR_DEPTH = 2


def _log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass  # no log dir outside the sandbox


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _set_issue_permit(granted: bool) -> None:
    """Let the phase starting now file one issue, or none."""
    try:
        if granted:
            ISSUE_PERMIT_FILE.write_text("1")
        else:
            ISSUE_PERMIT_FILE.unlink(missing_ok=True)
    except OSError:
        pass  # at worst one issue too many, or one too few


def _allow(msg: str) -> None:
    _set_issue_permit(False)
    _log(f"allow: {msg}")
    sys.exit(0)


def _block(reason: str, state: dict, *, may_file_issue: bool = False) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError as e:
        # Can't remember the phase -> we'd repeat this instruction forever.
        # End the run instead; the monitor recovers.
        _allow(f"state write failed ({e}) — fail open")
    _set_issue_permit(may_file_issue)
    _log(
        f"block #{state['blocks']} next-phase={state['phase']} "
        f"cycle={state['cycles']}/{ORCHESTRATOR_DEPTH}: {reason[:100]}"
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def main() -> None:
    if os.environ.get("NINJA_CYCLE_RUN") != "1":
        sys.exit(0)  # not a cycle run — never interfere

    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        hook_input = {}
    _log(f"stop: last_msg={str(hook_input.get('last_assistant_message', ''))[:80]!r}")

    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    # The agent can edit this file, so anything unusable means a fresh run —
    # better than crashing on every stop.
    if not isinstance(state, dict):
        state = {}
    phase = state.get("phase")
    state["phase"] = phase if phase in ("work", "reflect", "wrapup") else "work"
    state["blocks"] = _as_int(state.get("blocks")) + 1
    state["cycles"] = _as_int(state.get("cycles"))

    if state["phase"] == "work":
        state["cycles"] += 1  # this issue is done either way
        if state["cycles"] >= ORCHESTRATOR_DEPTH:
            state["phase"] = "wrapup"
            _block(
                "Phase 1 checkpoint. If the issue you picked is not yet closed "
                "or blocked, finish that first. You have now worked this run's "
                f"limit of {ORCHESTRATOR_DEPTH} issue(s), so do NOT start "
                "another one and do NOT file any new issues — record anything "
                "worth keeping in your memory file instead. Then stop.",
                state,
            )
        # Reflect runs even on an empty queue — it may file the next issue.
        state["phase"] = "reflect"
        _block(
            "Phase 1 checkpoint. If the issue you picked is not yet closed or "
            "blocked, finish that first. Then do Loop Phase 2 — REFLECT, PLAN "
            "& LEARN — exactly as described in your instructions, and stop. "
            "You may file at most ONE new issue in this phase, and only with "
            "tools/issues.py — never a direct `gh issue create`.",
            state,
            may_file_issue=True,
        )

    elif state["phase"] == "wrapup":
        _allow(f"depth {ORCHESTRATOR_DEPTH} reached — run complete")

    elif state["phase"] == "reflect":
        # Only this branch needs the count, so a gh outage costs one cycle
        # instead of the wrap-up instruction.
        try:
            sys.path.insert(0, str(REPO_ROOT))
            from tools import issues

            open_count = issues.count_actionable()
        except Exception as e:  # noqa: BLE001
            _allow(f"issue count failed ({e!r}) — fail open")

        if open_count > 0:
            state["phase"] = "work"
            _block(
                f"Reflect checkpoint passed. {open_count} actionable issue(s) "
                "in the queue — start the next cycle now: Loop Phase 1 (work "
                "the single highest-priority open issue to completion), then "
                "stop.",
                state,
            )
        _allow("queue empty after reflect — run complete")


if __name__ == "__main__":
    main()
