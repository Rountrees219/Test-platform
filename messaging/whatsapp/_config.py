"""Shared WhatsApp gateway URL/token resolver.

interface.py (CLI) and services/monitor_whatsapp_service.py both need the same
resolution chain: CLI flag → env → settings.json → default. Duplicating it in
two places led to drift (e.g. one added a CLI-flag branch the other never got).
Single source of truth here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8090"
SETTINGS_PATH = Path.home() / ".agent_settings.json"


def _settings_whatsapp() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    wa = data.get("whatsapp")
    return wa if isinstance(wa, dict) else {}


def gateway_url(cli_override: Optional[str] = None) -> str:
    """Resolve gateway base URL: CLI arg → env → settings → default."""
    raw = (
        cli_override
        or os.environ.get("WHATSAPP_GATEWAY_URL")
        or _settings_whatsapp().get("gateway_url")
        or DEFAULT_GATEWAY_URL
    )
    return str(raw).rstrip("/")


def gateway_token(cli_override: Optional[str] = None) -> Optional[str]:
    """Resolve gateway bearer token: CLI arg → env → settings. None if unset."""
    return (
        cli_override
        or os.environ.get("WHATSAPP_GATEWAY_TOKEN")
        or _settings_whatsapp().get("gateway_token")
    )
