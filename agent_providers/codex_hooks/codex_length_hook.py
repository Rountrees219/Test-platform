import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from constants import (
    HANDOFF_CONTEXT,
    MONITOR_TURNS_THRESHOLD,
)

LOG_FILE = Path("/workspace/logs/codex_length_hook.log")
# Shared with the provider's resume logic (mirrors the Claude wrapper).
STATE_FILE_PATH = Path("/workspace/ninja/stop_hook_length_state.json")
# Codex rollout transcripts live here (see constants.CODEX_LOG_DIR).
SESSIONS_DIR = (
    Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
)


def _log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass  # read-only fs outside the sandbox -- fail quiet


def _session_id_from_file(file_path: Path):
    """First ``payload.session_id`` found in a rollout ``.jsonl`` file."""
    try:
        with open(file_path, encoding="utf-8") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload")
                if payload and payload.get("session_id"):
                    return payload["session_id"]
    except OSError:
        return None
    return None


def _resolve_transcript_path(hook_input: dict):
    """Map the current monitor run to its rollout ``.jsonl``.

    Preference order:
      1. ``transcript_path`` given directly by Codex on stdin.
      2. Lookup by ``session_id`` from stdin across all rollout files.
      3. Fallback: newest-modified ``.jsonl`` (the active session), matching
         the POC's ``get_latest_traces_session_id`` heuristic.
    """
    transcript_path = hook_input.get("transcript_path")
    if transcript_path and Path(transcript_path).is_file():
        return Path(transcript_path)

    if not SESSIONS_DIR.is_dir():
        return None

    rollouts = sorted(
        SESSIONS_DIR.rglob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not rollouts:
        return None

    session_id = hook_input.get("session_id")
    if session_id:
        for path in rollouts:
            if _session_id_from_file(path) == session_id:
                return path
        _log(f"session_id={session_id} not found -- falling back to newest rollout")

    return rollouts[0]


def count_turns_from_transcript(file_path: Path) -> int:
    """Count ``response_item`` entries since the last user ``input_text``.

    Ported from ``codex_sessions_n_context.count_actual_truns_from_file``:
    every ``response_item`` line increments the counter; a content block of
    ``type == "input_text"`` (a genuine user message) resets it to 0. So the
    result is the number of assistant answers / reasonings / tool calls / tool
    responses accumulated since the most recent real user turn.
    """
    counter = 0
    with open(file_path, encoding="utf-8") as f:
        for raw in f:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "response_item":
                continue
            payload = obj.get("payload")
            if payload:
                for block in payload.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "input_text":
                        counter = 0
            counter += 1
    return counter


def build_hook_output(*, hook_input: dict) -> dict:
    # Only the armed monitor run is affected.
    if os.environ.get("NINJA_MONITOR_RUN") != "1":
        return {}

    transcript_path = _resolve_transcript_path(hook_input)
    if transcript_path is None:
        _log("no transcript found -- fail open")
        return {}

    try:
        count = count_turns_from_transcript(transcript_path)
    except OSError as e:
        _log(f"transcript read failed ({e}) -- fail open")
        return {}

    if count < MONITOR_TURNS_THRESHOLD:
        return {}

    # Already stopped once for this run? Don't repeat the hard stop.
    try:
        already = json.loads(STATE_FILE_PATH.read_text()).get("stopped_by_length")
    except (OSError, ValueError):
        already = False
    if already is True:
        _log(f"skip transcript={transcript_path.name} count={count} (already stopped)")
        return {}

    _log(
        f"hardstop transcript={transcript_path.name} count={count} "
        f"threshold={MONITOR_TURNS_THRESHOLD}"
    )
    try:
        STATE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE_PATH.write_text(json.dumps({"stopped_by_length": True}))
    except OSError as e:
        # Can't signal the provider to resume -> don't hard-stop, or we'd end
        # the monitor without a handoff. Fail open.
        _log(f"state write failed ({e}) -- fail open")
        return {}

    return {
        "continue": False,
        "stopReason": "More than maximum allowed turns!",
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": HANDOFF_CONTEXT,
        },
    }


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        hook_input = {}

    try:
        output = build_hook_output(hook_input=hook_input)
    except Exception:  # noqa: BLE001 -- fail open
        _log("build_hook_output failed -- fail open")
        output = {}

    if output:
        print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
