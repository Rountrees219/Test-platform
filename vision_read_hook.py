"""Deny a Read of a picture and point it at the vision skill.

Read hands the file to the model, which one that cannot see answers with a 400
that takes the whole turn down. Registered only for such a model; see
processes/orchestrator.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

UNREADABLE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".mp4",
    ".mov",
    ".m4v",
}

REASON = (
    "You cannot see pictures, so Read would have failed the whole request. "
    "Run Skill(vision) and read the file with the helper it names."
)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, ValueError):
        sys.exit(0)

    path = str((payload.get("tool_input") or {}).get("file_path") or "")
    if (
        payload.get("tool_name") == "Read"
        and Path(path).suffix.lower() in UNREADABLE_SUFFIXES
    ):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": REASON,
                    }
                }
            )
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
