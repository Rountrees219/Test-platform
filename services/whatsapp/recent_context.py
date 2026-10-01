"""Shared rolling buffer of the last N WhatsApp messages, visible to every pool session.

Light workers each run their own `claude --resume` session, so their internal
histories diverge. This module gives them a shared short-term context: every
inbound + outbound line is appended to one JSON file trimmed to MAX_MESSAGES, and
monitor_whatsapp._build_prompt injects the tail into every turn's prompt.

Best-effort: all calls swallow errors so a logging hiccup never blocks a reply.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

try:
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # non-posix → best-effort
    _HAVE_FCNTL = False

MAX_MESSAGES = 5
_RUNTIME_DIR = Path("/root/.ninja_whatsapp")
_PATH = _RUNTIME_DIR / "recent_messages.json"
_MAXLEN = 280  # keep the injected block small


def _read(path: Path) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return []


def record_message(
    role: str, text: str, tag: "str | None" = None, ts: "int | None" = None
) -> None:
    """Append one line; keep last MAX_MESSAGES. role: 'user'|'ninja'. Never raises."""
    try:
        text = (text or "").strip()
        if not text:
            return
        if len(text) > _MAXLEN:
            text = text[: _MAXLEN - 1] + "…"
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        entry: dict = {"role": role, "text": text}
        if tag:
            entry["tag"] = tag
        if ts is not None:
            entry["ts"] = ts
        lf = open(str(_PATH) + ".lock", "w")
        try:
            if _HAVE_FCNTL:
                fcntl.flock(lf, fcntl.LOCK_EX)
            items = _read(_PATH)
            items.append(entry)
            items = items[-MAX_MESSAGES:]
            fd, tmp = tempfile.mkstemp(
                dir=str(_PATH.parent), prefix=".rm_", suffix=".json"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
            os.replace(tmp, _PATH)
        finally:
            if _HAVE_FCNTL:
                fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
    except Exception:
        pass


def clear_recent_messages() -> None:
    try:
        if _PATH.exists():
            _PATH.unlink()
    except OSError:
        pass


def render_recent(unified: bool = False, exclude_tag: "str | None" = None) -> str:
    """Render the buffer for prompt injection ('' on empty/error).

    unified=True drops per-worker tags so the answering session treats all
    Ninja lines as its own continuous voice. False keeps [ops0]/[ops1] tags.

    exclude_tag filters out Ninja lines whose tag matches — used by a worker
    that already has its own outputs in --resume history to avoid double-paying
    for them in the injected context. User lines (no tag) always pass through.
    """
    try:
        items = _read(_PATH)
        if not items:
            return ""
        lines = []
        for it in items:
            role = it.get("role")
            if role == "ninja":
                tag = it.get("tag")
                if exclude_tag and tag == exclude_tag:
                    continue
                if unified:
                    who = "Ninja"
                else:
                    who = "Ninja" + (f" [{tag}]" if tag else "")
            else:
                who = "User"
            lines.append(f"{who}: {it.get('text', '')}")
        return "\n".join(lines)
    except Exception:
        return ""
