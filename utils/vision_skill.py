"""Keeps the vision skill present only for models that need it.

Opus reads images itself; GLM 5.3 cannot and depends on the vision MCP. Boot
and a mid-session model switch both call this, so they share one rule.

    python -m utils.vision_skill [model]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from constants import VISION_SKILL_MODELS

SKILL_NAME = "vision"
SKILLS_ZIP = Path("/workspace/ninja/skills.zip")
SKILLS_HOMES = (Path.home() / ".claude", Path.home() / ".codex")


def vision_skill_enabled(model: str) -> bool:
    return (model or "").strip() in VISION_SKILL_MODELS


def apply_vision_skill(model: str) -> bool:
    """Add or remove the skill folder to match the model. Returns whether it is on."""
    wanted = vision_skill_enabled(model)
    for home in SKILLS_HOMES:
        folder = home / "skills" / SKILL_NAME
        if not wanted:
            shutil.rmtree(folder, ignore_errors=True)
        elif not folder.is_dir() and SKILLS_ZIP.is_file():
            home.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "unzip",
                    "-o",
                    "-q",
                    str(SKILLS_ZIP),
                    f"skills/{SKILL_NAME}/*",
                    "-d",
                    str(home),
                ],
                check=False,
            )
    return wanted


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else ""
    print(
        f"vision skill {'on' if apply_vision_skill(model) else 'off'} for {model or '(none)'}"
    )
