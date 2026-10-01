"""
Ninja configuration.

Reads settings from environment variables, ninja/config.json, or defaults.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

NINJA_DIR = Path(__file__).parent
PROJECT_ROOT = NINJA_DIR.parent
BROWSER_DATA_DIR = NINJA_DIR / "browser_data"
SCREENSHOTS_DIR = NINJA_DIR / "screenshots"

# Ensure dirs exist
BROWSER_DATA_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR.mkdir(exist_ok=True)


@dataclass
class NinjaConfig:
    # LLM settings
    model: str = "claude-opus-5"
    max_tokens: int = 4096
    temperature: float = 0.0

    # Browser settings
    headless: bool = False
    viewport_width: int = 1600
    viewport_height: int = 900
    timeout: int = 30000
    slow_mo: int = 0
    user_data_dir: str = str(BROWSER_DATA_DIR)

    # Proxy settings
    proxy: Optional[str] = None  # e.g. "http://proxy:8080"

    # Agent settings
    max_steps: int = 30
    screenshot_on_step: bool = True
    verbose: bool = False

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "NinjaConfig":
        """Load config from JSON file, env vars, then defaults."""
        data = {}

        # 1. Load from file
        path = Path(config_path) if config_path else NINJA_DIR / "config.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)

        # 2. Override with env vars
        env_map = {
            "NINJA_MODEL": "model",
            "NINJA_MAX_STEPS": ("max_steps", int),
            "NINJA_HEADLESS": ("headless", lambda v: v.lower() in ("1", "true")),
            "NINJA_PROXY": "proxy",
            "NINJA_VERBOSE": ("verbose", lambda v: v.lower() in ("1", "true")),
            "NINJA_TIMEOUT": ("timeout", int),
        }
        for env_key, mapping in env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                if isinstance(mapping, str):
                    data[mapping] = val
                else:
                    field_name, converter = mapping
                    data[field_name] = converter(val)

        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
