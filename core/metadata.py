"""
core/metadata.py — Cached loaders for runtime metadata files.

Provides a single, canonical source for reading the three metadata files
written by the container entrypoint at startup. All results are cached via
``config_cached`` and can be invalidated with ``refresh_config()``.

Usage::

    from core.metadata import load_sandbox_metadata, load_ph_metadata

    thread_id = load_sandbox_metadata().get("thread_id")
    sandbox_id = load_ph_metadata().get("sandbox_id")
"""

from __future__ import annotations

import json
from pathlib import Path

from constants import (
    DEFAULT_MODEL,
    PH_METADATA_DURABLE_PATH,
    PH_METADATA_PATH,
    SANDBOX_METADATA_DURABLE_PATH,
    SANDBOX_METADATA_PATH,
)
from core.config import config_cached


def _load_with_durable_fallback(tmpfs: Path, durable: Path) -> dict:
    """Read the tmpfs copy, falling back to the durable one a restart did not wipe.

    Restores tmpfs on the fallback path: readers that open the literal path
    (dashboard, set_timezone.py) never come through here.
    """
    for path in (tmpfs, durable):
        try:
            metadata = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if path is durable:
            try:
                tmpfs.write_text(path.read_text())
            except OSError:
                pass
        return metadata
    return {}


@config_cached("sandbox_metadata")
def load_sandbox_metadata() -> dict:
    """
    Load and cache ``/dev/shm/sandbox_metadata.json``, falling back to the
    durable copy suna writes alongside it.

    Returns the full parsed dict, or ``{}`` when neither path is readable.

    Common keys: ``thread_id``, ``environment``, ``use_agent_event_cache``,
    ``litellm_selected_model``.
    """
    return _load_with_durable_fallback(
        SANDBOX_METADATA_PATH, SANDBOX_METADATA_DURABLE_PATH
    )


# A backend running locally has no service deployment of its own, so the
# "local" environment has no hostname to build from: ninja-suna-manus points a
# local backend at beta (`backend/.env.example` ships
# AGENT_EVENT_CACHE_SERVICE_URL=https://agent-event-cache.beta.myninja.ai). A
# sandbox it deploys therefore carries environment="local" and has to talk to
# the same beta services its backend does. ninja-install.sh already resolves
# the package CDN this way.
_SERVICE_ENVIRONMENT_ALIASES = {"local": "beta"}


def service_environment() -> str:
    """Environment name for building service URLs, with local backends aliased.

    Returns ``""`` when the metadata file carries no environment, so callers keep
    their own "cannot determine the URL" handling rather than guessing a stage.
    """
    environment = load_sandbox_metadata().get("environment", "").strip()
    return _SERVICE_ENVIRONMENT_ALIASES.get(environment, environment)


def get_selected_model() -> str:
    """
    Read litellm_selected_model from /dev/shm/sandbox_metadata.json if present.
    Falls back to DEFAULT_MODEL ('claude-opus-5') if the file
    doesn't exist, is unreadable, or doesn't contain litellm_selected_model.

    Returns:
        Model name string
    """
    meta = load_sandbox_metadata()
    if not meta:
        return DEFAULT_MODEL

    model = meta.get("litellm_selected_model", "").strip()
    if model:
        return model
    return DEFAULT_MODEL


@config_cached("ph_metadata")
def load_ph_metadata() -> dict:
    """
    Load and cache ``/dev/shm/ph_metadata.json``, falling back to the durable
    copy suna writes alongside it.

    Returns the full parsed dict, or ``{}`` when neither path is readable.

    Common keys: ``posthog_host``, ``posthog_key``, ``sandbox_id``.
    """
    return _load_with_durable_fallback(PH_METADATA_PATH, PH_METADATA_DURABLE_PATH)
