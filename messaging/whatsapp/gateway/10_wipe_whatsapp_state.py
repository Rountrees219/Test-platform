#!/usr/bin/env python3
"""Publish hook: run wipe-state.sh.

ninja-sandbox-template run_all_hooks.py only executes *.py hooks (via
sys.executable). Installers previously dropped a .sh that was silently ignored.
This shim keeps the bash wipe as the single source of truth.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_CANDIDATES = (
    Path(__file__).with_name("wipe-state.sh"),
    Path("/workspace/ninja/messaging/whatsapp/gateway/wipe-state.sh"),
)


def main() -> int:
    script = next((p for p in _CANDIDATES if p.is_file()), None)
    if script is None:
        print(
            "wipe script missing; checked: " + ", ".join(str(p) for p in _CANDIDATES),
            file=sys.stderr,
        )
        return 1
    result = subprocess.run(["/bin/bash", str(script)], check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
