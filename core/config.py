"""
core.config — shared config cache with SIGHUP-triggered refresh.

Usage
-----
Register a loader once per module:

    from core.config import config_cached

    @config_cached("slack")
    def _load_slack_config() -> dict:
        with open(Path.home() / ".agent_settings.json") as f:
            return json.load(f)

    # Call it anywhere — result is cached until refresh_config() is called.
    cfg = _load_slack_config()

Trigger a full refresh (e.g. from a test):

    from core.config import refresh_config
    refresh_config()          # invalidate all caches
    refresh_config("slack")   # invalidate only the "slack" cache

SIGHUP wiring
-------------
Call install_sighup_handler() once at process startup to make SIGHUP
automatically invalidate all caches:

    from core.config import install_sighup_handler
    install_sighup_handler()
"""

from __future__ import annotations

import functools
import json
import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

from constants import (
    AGENT_SETTINGS_PATH,
    CODEX_SETTINGS_FILE,
    COMMON_LOGGER_NAME,
    DEFAULT_ORCHESTRATOR_CONFIG,
    MONITOR_SERVICE_NAME,
    ORCHESTRATOR_CONFIG_PATH,
    ORCHESTRATOR_SERVICE_NAME,
)

_logger = logging.getLogger(__name__)
logger = logging.getLogger(COMMON_LOGGER_NAME)

_SENTINEL = object()
_registry: Dict[str, dict] = {}  # {"name": {"fn": callable, "value": Any}}
_lock = threading.Lock()
_sighup_installed = False

F = TypeVar("F", bound=Callable[..., Any])


def config_cached(name: str) -> Callable[[F], F]:
    """
    Decorator that caches the return value of a zero-argument loader function.

    The result is kept in memory until ``refresh_config(name)`` or
    ``refresh_config()`` is called.

    Args:
        name: Unique key for this config entry (used by refresh_config).

    Example:
        @config_cached("slack")
        def load_slack_config() -> dict:
            ...
    """

    def decorator(fn: F) -> F:
        with _lock:
            _registry[name] = {"fn": fn, "value": _SENTINEL}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _lock:
                entry = _registry.get(name)
                if entry is None:
                    return fn(*args, **kwargs)
                if entry["value"] is _SENTINEL:
                    entry["value"] = entry["fn"]()
                    _logger.debug("config_cached[%s]: loaded", name)
                return entry["value"]

        return wrapper  # type: ignore[return-value]

    return decorator


def refresh_config(name: Optional[str] = None) -> None:
    """
    Invalidate one or all cached config entries.

    On the next call to the decorated loader the value will be re-loaded
    from its source.

    Args:
        name: If given, invalidate only this entry. If None, invalidate all.
    """
    with _lock:
        if name is not None:
            entry = _registry.get(name)
            if entry is not None:
                entry["value"] = _SENTINEL
                _logger.debug("config_cached[%s]: invalidated", name)
            else:
                _logger.warning("refresh_config: unknown key %r", name)
        else:
            for key, entry in _registry.items():
                entry["value"] = _SENTINEL
            _logger.debug("config_cached: all entries invalidated (%d)", len(_registry))


def install_sighup_handler() -> None:
    """
    Install a SIGHUP handler that calls refresh_config() on receipt.

    Safe to call multiple times — only the first call installs the handler.
    """
    global _sighup_installed
    if _sighup_installed:
        return

    def _handler(signum: int, frame: Any) -> None:
        _logger.info("SIGHUP received — refreshing all config caches")
        refresh_config()

    signal.signal(signal.SIGHUP, _handler)
    _sighup_installed = True
    _logger.debug("install_sighup_handler: installed")


# ---------------------------------------------------------------------------
# Agent config
# ---------------------------------------------------------------------------


@config_cached("agent_config")
def load_agent_config() -> dict:
    """Load agent configuration from ~/.agent_settings.json.

    Result is cached until refresh_config() or a SIGHUP is received.
    Returns an empty dict if the file is missing or unreadable.
    """
    try:
        if AGENT_SETTINGS_PATH.exists():
            return json.loads(AGENT_SETTINGS_PATH.read_text())
    except Exception as e:
        print(f"⚠️ Warning: Could not read config: {e}", file=sys.stderr)
    return {}


# ---------------------------------------------------------------------------
# Monitor state I/O
# ---------------------------------------------------------------------------
# These helpers persist the monitor's seen-message dedup state and agent
# thread-tracking state to disk. They are NOT cached (state changes every
# poll cycle) — but they live here alongside load_agent_config() so all
# file-based state I/O is in one place.


def _get_repo_root() -> Path:
    """Return the repo root (src/ninja/) relative to this file."""
    return Path(__file__).parent.parent


class SeenMessages(set):
    """A set that remembers insertion order, so the saved window keeps the
    newest ids. Sorting only works for Slack ts; WhatsApp keys are random."""

    def __init__(self, items=()) -> None:
        super().__init__()
        self._order: dict = {}
        for item in items:
            self.add(item)

    def add(self, item) -> None:
        super().add(item)
        self._order.pop(item, None)
        self._order[item] = None

    def recent(self, n: int) -> list:
        return list(self._order)[-n:]


def load_seen_messages() -> set:
    """Load previously seen message IDs from .seen_messages.json."""
    path = _get_repo_root() / ".seen_messages.json"
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return SeenMessages(data.get("seen", []))
    except Exception:
        pass
    return SeenMessages()


def save_seen_messages(seen: set) -> None:
    """Persist seen message IDs (keeps last 100 to bound file size)."""
    path = _get_repo_root() / ".seen_messages.json"
    try:
        if isinstance(seen, SeenMessages):
            recent = seen.recent(100)
        else:
            recent = sorted(seen)[-100:]
        path.write_text(json.dumps({"seen": recent}))
    except Exception as e:
        print(f"⚠️ Warning: Could not save seen messages: {e}", file=sys.stderr)


# Sent-message IDs — the agent's own posts, persisted so the monitor process can recognize them (say() runs in a different process, so an in-memory set can't).
# Needed for Teams delegated tokens, which lack a bot identity / from_me flag.
def load_sent_messages() -> set:
    """Load IDs of messages this agent has sent."""
    path = _get_repo_root() / ".teams_sent_messages.json"
    try:
        if path.exists():
            return set(json.loads(path.read_text()).get("sent", []))
    except Exception:
        pass
    return set()


def record_sent_message(msg_id: str, keep: int = 500) -> None:
    """Append a sent message ID to the store, keeping the most recent ``keep``.

    Ordered append (not a set on disk) so the cap drops the oldest, not a random
    entry. Best-effort — a lost write just falls back to the :ninja: signature.
    """
    msg_id = str(msg_id or "")
    if not msg_id:
        return
    path = _get_repo_root() / ".teams_sent_messages.json"
    try:
        ids = json.loads(path.read_text()).get("sent", []) if path.exists() else []
        if msg_id in ids:
            return
        ids.append(msg_id)
        path.write_text(json.dumps({"sent": ids[-keep:]}))
    except Exception as e:
        logger.warning(f"⚠️ Warning: Could not record sent message: {e}")


def load_agent_messages() -> dict:
    """Load agent thread-tracking state from .agent_messages.json."""
    path = _get_repo_root() / ".agent_messages.json"
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {"messages": [], "seen_replies": []}


def save_agent_messages(data: dict) -> None:
    """Persist agent thread-tracking state (bounded to prevent unbounded growth)."""
    path = _get_repo_root() / ".agent_messages.json"
    try:
        data["messages"] = data.get("messages", [])[-20:]
        data["seen_replies"] = data.get("seen_replies", [])[-5000:]
        path.write_text(json.dumps(data))
    except Exception as e:
        logger.warning(f"Warning: Could not save agent messages: {e}")


# ---------------------------------------------------------------------------
# Orchestrator config
# ---------------------------------------------------------------------------
# Orchestrator enable/disable configuration.
# Fail open if the file is missing or unreadable, orchestrator is enabled by default.


def load_orchestrator_config() -> dict:
    """Return the orchestrator config. If file not found, write and return default config."""
    config = dict(DEFAULT_ORCHESTRATOR_CONFIG)
    try:
        if ORCHESTRATOR_CONFIG_PATH.exists():
            data = json.loads(ORCHESTRATOR_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update(data)
        else:
            config["updated_at"] = datetime.now(timezone.utc).isoformat()
            ORCHESTRATOR_CONFIG_PATH.write_text(
                json.dumps(config, indent=2), encoding="utf-8"
            )
    except Exception as e:
        logger.warning(
            f"Warning: Could not read orchestrator config, setting it to default: {e}"
        )
    return config


def save_orchestrator_config(config: dict) -> dict:
    """Persist config to disk with timestamp. Returns the written config."""
    merged = load_orchestrator_config()
    merged.update(config)
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    config = merged
    ORCHESTRATOR_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def is_orchestrator_enabled(config: dict = None) -> bool:
    """Return is orchestrator is enabled."""
    if config is None:
        config = load_orchestrator_config()
    if config.get("enabled", True):
        return True
    return False


# ---------------------------------------------------------------------------
# Codex settings
# ---------------------------------------------------------------------------
@config_cached("codex_settings")
def load_codex_settings() -> dict:
    """Load Codex settings from .codex/settings.json."""
    try:
        if not CODEX_SETTINGS_FILE.exists():
            CODEX_SETTINGS_FILE.write_text(
                json.dumps(
                    {
                        "session_id": {
                            MONITOR_SERVICE_NAME: None,
                            ORCHESTRATOR_SERVICE_NAME: None,
                        }
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return json.loads(CODEX_SETTINGS_FILE.read_text())

    except Exception as e:
        logger.warning(f"Could not read Codex config: {e}")
    return {}


def save_codex_settings(service_name, session_id) -> dict:
    """Persist Codex settings to disk. Returns the written config.

    Backs up the previous file with a timestamp only when the value changes.
    """
    config = load_codex_settings()
    if config.get("session_id", {}).get(service_name) == session_id:
        return config
    if CODEX_SETTINGS_FILE.is_file():
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        bak = CODEX_SETTINGS_FILE.with_suffix(f".{ts}.bak")
        try:
            CODEX_SETTINGS_FILE.rename(bak)
        except OSError:
            pass
    if "session_id" not in config:
        config["session_id"] = {}
    config["session_id"][service_name] = session_id
    CODEX_SETTINGS_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    refresh_config("codex_settings")
    return config
