#!/bin/bash
# Claude Code Wrapper Script

export HOME=/root
export PATH="/usr/local/bin:$PATH"
export USER=root
export TERM=xterm-256color
export IS_SANDBOX=1

# Disable Claude Code's internal cron so sessions launched through this
# wrapper rely on external cron instead.
export CLAUDE_CODE_DISABLE_CRON=1

# LiteLLM cannot provide server-side auto-mode checks, so avoid the eligibility warning.
export CLAUDE_CODE_AUTO_MODE_SERVER=0

# Auto-compact each persistent Claude Code session as its context approaches 150K tokens
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=75
# Hide Claude Code's bundled dev-workflow skills (code-review, verify, init, ...)
# so the skills catalog only lists Ninja's own .claude/skills/.
export CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1

# Claude Code stops honoring a Stop hook after 8 blocks in a row.
# So raise the cap. This is the effective per-run backstop (around 50 issues).
export CLAUDE_CODE_STOP_HOOK_BLOCK_CAP="${CLAUDE_CODE_STOP_HOOK_BLOCK_CAP:-100}"

# When this run started. The monitor length hook reads it to measure elapsed
# time, and uses it to tell one run's step count from the next.
export NINJA_RUN_STARTED_AT="$(date +%s)"

# CLAUDE_CWD is the working directory (repo root) set by ClaudeProvider.
CLAUDE_CWD="${CLAUDE_CWD:?CLAUDE_CWD must be set by the caller}"
STOPPED_LENGTH_PROMPT="$CLAUDE_CWD/stopped_length_prompt.txt"
SETTINGS_FILE="${CLAUDE_SETTINGS_FILE:-$CLAUDE_CWD/settings.json}"

# Temp files for stdout and stderr (kept separate).
OUT_FILE=$(mktemp)
ERR_FILE=$(mktemp)
trap "rm -f '$OUT_FILE' '$ERR_FILE'" EXIT

# Locate the claude binary (npm global install location varies by environment)
CLAUDE_BIN=""
for candidate in "/root/.local/bin/claude" "/usr/local/bin/claude" "/usr/bin/claude" "$(which claude 2>/dev/null)"; do
    if [ -x "$candidate" ]; then
        CLAUDE_BIN="$candidate"
        break
    fi
done

if [ -z "$CLAUDE_BIN" ]; then
    echo "ERROR: claude binary not found. Tried: /root/.local/bin/claude, /usr/local/bin/claude, /usr/bin/claude" >&2
    exit 1
fi

# Default timeout is 2 hours (7200 seconds).  Callers override via
# env CLAUDE_TIMEOUT.  Python subprocess.run(timeout=CLAUDE_TIMEOUT+60)
# provides the outer safety net.
CLAUDE_TIMEOUT="${CLAUDE_TIMEOUT:-7200}"

# Prompt is fed via stdin from CLAUDE_PROMPT_FILE to avoid E2BIG.
# stdout and stderr are kept SEPARATE so Python's capture_output=True
# sees real stderr and --output-format json stdout stays clean.
timeout --signal=TERM --kill-after=10s "${CLAUDE_TIMEOUT}s" \
    "$CLAUDE_BIN" --settings "$SETTINGS_FILE" "$@" \
    < "$CLAUDE_PROMPT_FILE" > "$OUT_FILE" 2> "$ERR_FILE"
CLAUDE_EXIT=$?

# Only check stop_hook_length_state if Claude did NOT time out.
# GNU timeout returns 124 when the command times out.
STOP_HOOK_STATE="$CLAUDE_CWD/stop_hook_length_state.json"
if [ "$CLAUDE_EXIT" -ne 124 ] && [ -f "$STOP_HOOK_STATE" ]; then
    STOPPED_BY_LENGTH=$(python3 - "$STOP_HOOK_STATE" <<'PY'
import json, sys

path = sys.argv[1]

try:
    with open(path) as f:
        state = json.load(f)
    print("true" if state.get("stopped_by_length") is True else "false")
except (FileNotFoundError, json.JSONDecodeError, OSError):
    print("false")
PY
)

    if [ "$STOPPED_BY_LENGTH" = "true" ]; then
        RETRY_ARGS=()
        SHOULD_RETRY=false

        for ((i=0; i<${#@}; i++)); do
            arg="${@:i+1:1}"

            if [ "$arg" = "--session-id" ] && [ "$((i + 1))" -lt "$#" ]; then
                # First run created the session; retry resumes it.
                SHOULD_RETRY=true
                RETRY_ARGS+=("--resume" "${@:i+2:1}")
                ((i++))
            elif [ "$arg" = "--resume" ] && [ "$((i + 1))" -lt "$#" ]; then
                SHOULD_RETRY=true
                RETRY_ARGS+=("--resume" "${@:i+2:1}")
                ((i++))
            else
                RETRY_ARGS+=("$arg")
            fi
        done

        if [ "$SHOULD_RETRY" = true ]; then
            # Reset the flag before starting the retry.
            python3 - "$STOP_HOOK_STATE" <<'PY'
import json, sys

path = sys.argv[1]

with open(path, "w") as f:
    json.dump({"stopped_by_length": False}, f)
PY

            # Fresh budget for the handoff, or it starts already over.
            export NINJA_RUN_STARTED_AT="$(date +%s)"

            # Continue the existing monitor session with the new prompt.
            timeout --signal=TERM --kill-after=10s "${CLAUDE_TIMEOUT}s" \
                "$CLAUDE_BIN" --settings "$SETTINGS_FILE" "${RETRY_ARGS[@]}" \
                < "$STOPPED_LENGTH_PROMPT" > "$OUT_FILE" 2> "$ERR_FILE"
            CLAUDE_EXIT=$?
        fi
    fi
fi

# `timeout` exits 124 when it kills the process due to the time limit.
if [ "$CLAUDE_EXIT" -eq 124 ]; then
    echo "ERROR: claude timed out after ${CLAUDE_TIMEOUT}s" >> "$ERR_FILE"
fi

# Emit sanitised stdout (ANSI codes, OSC sequences, control chars stripped).
# With --output-format json the output is already clean, but the filter is
# harmless and protects against unexpected escape sequences.
cat "$OUT_FILE" | tr -d '\r' | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g; s/\x1b\[[?][0-9]*[a-zA-Z]//g; s/\x1b\[<u//g; s/\x1b\][0-9]*;[^\x07]*\x07//g'

# Emit raw stderr on fd 2 (no ANSI stripping — errors should be readable).
cat "$ERR_FILE" >&2

# Propagate the real exit code.
exit "$CLAUDE_EXIT"
