"""WhatsApp destination routing for the Ninja WhatsApp runtime.

Precedence (highest first):
  1. WHATSAPP_FORCE_SINGLE_TO=1 -- forces WHATSAPP_TO regardless of caller arg
  2. explicit `to` arg         -- value passed by the caller
  3. WHATSAPP_TO                -- fallback when nothing else specified

Allowlist (WHATSAPP_ALLOWED_TO, comma-separated E.164 digits) is enforced last
and raises ValueError when the resolved destination is not allowed.

Note: WHATSAPP_FORCE_TO and WHATSAPP_DEFAULT_TO were removed as duplicates of
WHATSAPP_TO + WHATSAPP_FORCE_SINGLE_TO=1.
"""

from __future__ import annotations

import os
import re
from typing import Optional

_DIGITS_RE = re.compile(r"\D+")


def _digits(s: Optional[str]) -> str:
    if not s:
        return ""
    return _DIGITS_RE.sub("", s)


def _envbool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v == "1" or v.lower() == "true"


def _allowed_set() -> set[str]:
    raw = os.environ.get("WHATSAPP_ALLOWED_TO", "")
    return {_digits(x) for x in raw.split(",") if _digits(x)}


def resolve_whatsapp_to(explicit: Optional[str] = None) -> str:
    """Resolve the destination E.164 (digits only) for an outbound message.

    Raises ValueError if no destination can be resolved or if the resolved
    destination is not in WHATSAPP_ALLOWED_TO (when that env is set).
    """
    if _envbool("WHATSAPP_FORCE_SINGLE_TO", False):
        resolved = _digits(os.environ.get("WHATSAPP_TO"))
        if not resolved:
            raise ValueError("WHATSAPP_FORCE_SINGLE_TO=1 but WHATSAPP_TO is not set")
    elif _digits(explicit):
        resolved = _digits(explicit)
    else:
        resolved = _digits(os.environ.get("WHATSAPP_TO"))

    if not resolved:
        raise ValueError("no WhatsApp destination resolved (set --to or WHATSAPP_TO)")

    allowed = _allowed_set()
    if allowed and resolved not in allowed:
        raise ValueError(f"destination {resolved} not in WHATSAPP_ALLOWED_TO allowlist")

    return resolved
