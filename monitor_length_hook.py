#!/usr/bin/env python3
"""PostToolUse hook: hand the task over when the monitor runs past its budget."""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from constants import (
    HANDOFF_CONTEXT,
    MONITOR_MINUTES_THRESHOLD,
    MONITOR_TURNS_THRESHOLD,
)
from core import live_progress
from core.config import is_orchestrator_enabled

LOG_FILE = Path("/workspace/logs/monitor_length_hook.log")
STATE_FILE_PATH = "/workspace/ninja/stop_hook_length_state.json"
STEP_FILE_PATH = "/workspace/ninja/monitor_step_count.json"


def _log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def _read_json(path: str) -> dict:
    try:
        with open(path) as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def _bump_steps(run_id: str) -> int:
    """Add this tool call to the run's step count and return the total.
    Keyed by run so a run that ends without clearing the file cannot leak its
    count into the next one.
    """
    saved = _read_json(STEP_FILE_PATH)
    steps = 0
    if saved.get("run") == run_id:
        try:
            steps = int(saved.get("steps", 0))
        except (TypeError, ValueError):
            steps = 0

    steps += 1
    try:
        with open(STEP_FILE_PATH, "w") as handle:
            json.dump({"run": run_id, "steps": steps}, handle)
    except OSError:
        pass
    return steps


def _minutes_since(run_id: str) -> float:
    """Minutes since the run started — the run id is its start time."""
    try:
        return max(time.time() - float(run_id), 0.0) / 60
    except ValueError:
        return 0.0


def build_hook_output() -> dict:
    """Decide whether this tool call is the one that ends the monitor's turn."""
    # Set only for monitor runs, so the orchestrator's tool calls are ignored.
    if os.environ.get("NINJA_MONITOR_RUN") != "1":
        return {}

    run_id = os.environ.get("NINJA_RUN_STARTED_AT", "").strip()
    if not run_id:
        return {}

    if not is_orchestrator_enabled():
        return {}

    # An assistant turn can hold several tool calls, so a later one may reach
    # the hook after the hand-off was already decided. Leave it alone.
    if _read_json(STATE_FILE_PATH).get("stopped_by_length") is True:
        return {}

    steps = _bump_steps(run_id)
    minutes = _minutes_since(run_id)

    # Whichever budget runs out first hands the task over.
    over_steps = steps >= MONITOR_TURNS_THRESHOLD
    over_time = minutes >= MONITOR_MINUTES_THRESHOLD
    if not (over_steps or over_time):
        return {}

    reason = "time" if over_time and not over_steps else "steps"
    _log(
        f"warn run={run_id} by={reason} steps={steps}/{MONITOR_TURNS_THRESHOLD} "
        f"minutes={minutes:.1f}/{MONITOR_MINUTES_THRESHOLD}"
    )

    # This run is over; the handoff run starts counting from zero.
    Path(STEP_FILE_PATH).unlink(missing_ok=True)

    with open(STATE_FILE_PATH, "w") as file:
        file.write(json.dumps({"stopped_by_length": True}))

    return {
        "continue": False,
        "stopReason": "Monitor turn budget exhausted (steps or time).",
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": HANDOFF_CONTEXT,
        },
    }


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, ValueError):
        payload = {}

    # Separate feature, so it runs before the budget checks and in its own try —
    # a problem here must never stop the hand-off. Monitor only: the orchestrator
    # fires the same hook and its steps are not the user's.
    if os.environ.get("NINJA_MONITOR_RUN") == "1":
        try:
            live_progress.record_step(payload)
        except Exception:
            _log("live progress record failed — ignored")

    try:
        output = build_hook_output()
    except Exception:  # noqa: BLE001 — fail open
        _log("build_hook_output failed — fail open")
        output = {}

    if output:
        print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
