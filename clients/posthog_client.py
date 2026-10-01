"""
PostHog analytics client.

Provides a cached Posthog client instance and a convenience ``capture()``
function configured from ``/dev/shm/ph_metadata.json``.

Usage:
    from clients.posthog_client import capture

    capture(
        event="my_event_name",
        properties={"key": "value"},
    )

The ``distinct_id`` is resolved automatically from
``/dev/shm/sandbox_metadata.json`` (``thread_id`` field).
``sandbox_id`` is resolved from ``/dev/shm/ph_metadata.json``
"""

import os
from functools import cache
from typing import Any, Dict, Optional, Union

from core.logging import get_logger
from core.metadata import load_ph_metadata, load_sandbox_metadata
from posthog import Posthog

# core.logging, not logging.getLogger(__name__): the latter has no handler and
# propagates to a root that has none and sits at WARNING, so this module's
# warnings were dropped before reaching one. That included "feature_enabled
# failed", which made a PostHog outage look exactly like a disabled flag.
logger = get_logger("posthog")


def _is_local() -> bool:
    """Return True when running in local / docker-compose dev mode."""
    return os.environ.get("LOCAL_DEVELOPMENT_MODE", "").lower() in (
        "true",
        "1",
        "yes",
    )


@cache
def get_posthog_client() -> Posthog:
    """Return a cached Posthog client.

    Reads POSTHOG_KEY and POSTHOG_HOST from /dev/shm/ph_metadata.json.
    Raises AssertionError if no POSTHOG_KEY is available.
    """
    ph_meta = load_ph_metadata()
    key = ph_meta.get("posthog_key")
    host = ph_meta.get("posthog_host", "https://us.i.posthog.com")
    assert key, "POSTHOG_KEY is not configured in ph_metadata.json"
    return Posthog(project_api_key=key, host=host)


@cache
def get_sync_posthog_client() -> Posthog:
    """Return a cached Posthog client that uploads events synchronously.

    The default (async) client only enqueues events for a background consumer
    thread that uploads on a ~5s batch interval. Short-lived callers (e.g. the
    upgrade script's ``python -c``) can be torn down before that upload runs,
    silently losing the event. This client sends inline (``sync_mode=True``),
    so ``capture()`` does not return until the event has actually been POSTed.
    """
    ph_meta = load_ph_metadata()
    key = ph_meta.get("posthog_key")
    host = ph_meta.get("posthog_host", "https://us.i.posthog.com")
    assert key, "POSTHOG_KEY is not configured in ph_metadata.json"
    # Bound the inline upload: sync callers are best-effort telemetry, so a slow
    # or unreachable PostHog must never stall the caller (e.g. the upgrade run).
    return Posthog(
        project_api_key=key,
        host=host,
        sync_mode=True,
        timeout=5,
        max_retries=1,
    )


def capture(
    event: str,
    properties: Optional[Dict[str, Any]] = None,
    sync: bool = False,
) -> None:
    """Emit a PostHog event identified by the sandbox ``thread_id``.

    Silently no-ops when:
    - ``posthog_key`` is absent or empty in ``ph_metadata.json``
    - Sandbox metadata is unavailable (no ``thread_id``) in non-local mode

    Args:
        event:      Event name (e.g. ``"task_started"``).
        properties: Optional dict of metadata to attach to the event.
        sync:       Upload inline before returning (vs. the async ~5s batch);
                    set ``True`` for short-lived callers that may exit or be
                    torn down before the background upload runs.
    """
    # Require a PostHog key from ph_metadata.json.
    ph_meta = load_ph_metadata()
    key = ph_meta.get("posthog_key")
    if not key:
        return

    props = {**(properties or {})}

    if _is_local():
        props["ninja_sandbox_id"] = "local_dev"
        distinct_id = f"local-{os.environ.get('NINJA_USER_ID', 'unknown')}"
        print(
            f"[posthog] capture(distinct_id={distinct_id!r}, event={event!r}, properties={props})"
        )
        return

    # Resolve the distinct_id from sandbox metadata.
    metadata = load_sandbox_metadata()
    if not metadata.get("thread_id"):
        return

    user_id = metadata.get("user_id", "unknown")
    props["ninja_sandbox_id"] = ph_meta.get("sandbox_id", "")
    props["ninja_sandbox_provider"] = metadata.get("sandbox_provider", "unknown")
    props["ninja_thread_id"] = metadata["thread_id"]
    props["ninja_user_id"] = user_id
    distinct_id = user_id

    client = get_sync_posthog_client() if sync else get_posthog_client()
    client.capture(
        distinct_id=distinct_id,
        event=event,
        properties=props,
    )


def _flag_context() -> Optional[tuple[str, Dict[str, Any]]]:
    """Resolve (distinct_id, person_properties) for flag evaluation.

    Deliberately does *not* short-circuit in local dev mode, unlike capture().
    Evaluating a flag is a read: it writes nothing to analytics, so a local run
    has nothing to pollute. Bailing out here meant is_feature_enabled() handed
    back its default without ever asking PostHog, which is indistinguishable
    from the flag being switched off — every flag in the agent was silently
    dead locally, which is precisely where a rollout most needs checking.

    Returns None only when the flag genuinely cannot be evaluated, and logs
    why: a silent default is what made this expensive to diagnose.
    """
    ph_meta = load_ph_metadata()
    if not ph_meta.get("posthog_key"):
        logger.debug(
            "posthog flags unavailable: no 'posthog_key' in ph_metadata.json; "
            "callers fall back to their default"
        )
        return None

    metadata = load_sandbox_metadata()
    # NINJA_USER_ID as a fallback because local dev writes no user_id into
    # sandbox_metadata.json. Note it is used bare here, while capture() sends
    # "local-<id>": targeting rules are written against real user ids, so a
    # prefix that keeps local *events* separate would stop local *flags* from
    # matching the very rules under test.
    user_id = metadata.get("user_id") or os.environ.get("NINJA_USER_ID")
    thread_id = metadata.get("thread_id")
    distinct_id = user_id or thread_id

    if not distinct_id:
        logger.debug(
            "posthog flags unavailable: no user_id/thread_id in "
            "sandbox_metadata.json and no NINJA_USER_ID; callers fall back to "
            "their default"
        )
        return None

    person_properties = {
        "ninja_sandbox_id": ph_meta.get("sandbox_id", ""),
        "ninja_sandbox_provider": metadata.get("sandbox_provider", "unknown"),
    }

    if user_id:
        person_properties["ninja_user_id"] = user_id
    if thread_id:
        person_properties["ninja_thread_id"] = thread_id

    return distinct_id, person_properties


def is_feature_enabled(flag_key: str, default: bool = False) -> bool:
    """Return whether a boolean PostHog feature flag is enabled.
    Args:
        flag_key: The PostHog feature flag key.
        default:  Value returned when the flag cannot be evaluated.
    """
    ctx = _flag_context()
    if ctx is None:
        logger.debug(
            "flag %s not evaluated (no context) -> default %s", flag_key, default
        )
        return default
    distinct_id, person_properties = ctx
    try:
        result = get_posthog_client().feature_enabled(
            flag_key,
            distinct_id,
            person_properties=person_properties,
        )
    except Exception:
        logger.warning("posthog feature_enabled(%s) failed", flag_key, exc_info=True)
        return default
    # None means PostHog knows nothing about the key — a typo or an unreleased
    # flag. Worth distinguishing from an explicit false, since it reads the same
    # at the call site.
    if result is None:
        logger.warning(
            "posthog returned no value for flag %s (unknown key?) -> default %s",
            flag_key,
            default,
        )
        return default
    logger.debug("flag %s = %s (distinct_id=%s)", flag_key, bool(result), distinct_id)
    return bool(result)


def get_feature_flag(
    flag_key: str,
    default: Optional[Union[bool, str]] = None,
) -> Optional[Union[bool, str]]:
    """Return the value of a (possibly multivariate) PostHog feature flag.

    Args:
        flag_key: The PostHog feature flag key.
        default:  Value returned when the flag cannot be evaluated.
    """
    ctx = _flag_context()
    if ctx is None:
        return default
    distinct_id, person_properties = ctx
    try:
        result = get_posthog_client().get_feature_flag(
            flag_key,
            distinct_id,
            person_properties=person_properties,
        )
    except Exception:
        logger.warning("posthog get_feature_flag(%s) failed", flag_key, exc_info=True)
        return default
    return default if result is None else result
