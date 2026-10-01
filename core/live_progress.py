"""
Live progress — server-side activity progress via the agent-event-cache.

The server owns rendering, so the state file holds only the addressing tuple
and lifecycle flags.

The steps stay in a second file. The hook is a fresh process per tool call and
must not reach the network there, so it appends the label and the progress
thread reports it. A separate file also keeps a late write from wiping the
state — exactly what broke the length hook's hand-off flag.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from constants import (
    LIVE_PROGRESS_LABEL_CHARS,
    LIVE_PROGRESS_STATE_PATH,
    LIVE_PROGRESS_STEPS_PATH,
)

_FILE_TOOLS = ("Read", "Write", "Edit", "NotebookEdit")


def read_state() -> dict:
    """Return the active run's state, or {} when live progress is not running."""
    try:
        with open(LIVE_PROGRESS_STATE_PATH) as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def arm(
    *,
    channel: Optional[str],
    thread_ts: Optional[str],
    provider: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> None:
    """Mark a run as started.  Nothing is posted yet."""
    payload = {
        "channel": channel,
        "thread_ts": thread_ts,
        "provider": provider,
        "workspace_id": workspace_id,
        "started_at": datetime.now(timezone.utc).timestamp(),
        "activity_started": False,
    }
    with open(LIVE_PROGRESS_STATE_PATH, "w") as handle:
        json.dump(payload, handle)
    # Drop the last run's steps so they don't show up under the new message.
    Path(LIVE_PROGRESS_STEPS_PATH).unlink(missing_ok=True)


def mark_activity_started(activity_id: Optional[str] = None) -> bool:
    """Record that /activity/start has been called.

    Returns False if no run is armed (race with end_live_progress).
    """
    state = read_state()
    if not state:
        return False
    state["activity_started"] = True
    if activity_id:
        state["activity_id"] = activity_id
    with open(LIVE_PROGRESS_STATE_PATH, "w") as handle:
        json.dump(state, handle)
    return True


def seconds_running(state: dict, *, now: Optional[float] = None) -> float:
    """How long the current run has been going.  0.0 if the state is unusable."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    try:
        return max(0.0, now - float(state.get("started_at")))
    except (TypeError, ValueError):
        return 0.0


def clear_state() -> None:
    """Forget the activity.  Safe to call when nothing is running."""
    Path(LIVE_PROGRESS_STATE_PATH).unlink(missing_ok=True)
    Path(LIVE_PROGRESS_STEPS_PATH).unlink(missing_ok=True)


def label_for(payload: Dict[str, Any]) -> str:
    """Pick something worth showing the user for this tool call.

    Bash gives us the model's own one-line description, file tools give a path.
    """
    tool = str(payload.get("tool_name") or "").strip() or "step"
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return tool

    if tool == "Bash":
        described = str(tool_input.get("description") or "").strip()
        return described or tool
    if tool in _FILE_TOOLS:
        path = str(tool_input.get("file_path") or "").strip()
        return f"{tool} {os.path.basename(path)}" if path else tool
    if tool == "Skill":
        skill = str(tool_input.get("skill") or "").strip()
        return f"Skill {skill}" if skill else tool
    return tool


def record_step(payload: Dict[str, Any]) -> bool:
    """Add this tool call to the checklist. False when no run is active.

    Runs on every tool call, so it stays cheap: one small read, one append, no
    network. The progress thread reports these on its own timer.
    """
    if not read_state():
        return False

    entry = {
        "t": datetime.now(timezone.utc).timestamp(),
        "label": label_for(payload)[:LIVE_PROGRESS_LABEL_CHARS],
    }
    try:
        with open(LIVE_PROGRESS_STEPS_PATH, "a") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        return False
    return True


def read_steps() -> List[dict]:
    """Every step recorded for the current run, oldest first."""
    steps: List[dict] = []
    try:
        with open(LIVE_PROGRESS_STEPS_PATH) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("label"):
                    steps.append(entry)
    except OSError:
        return []
    return steps
