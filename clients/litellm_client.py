"""
Core LiteLLM Client Configuration
==================================

Reads API credentials from /root/.claude/settings.json and provides
shared configuration for all utility modules.

Settings keys (read from settings.json env block):
    ANTHROPIC_AUTH_TOKEN - API key for the gateway
    ANTHROPIC_BASE_URL   - Base URL of the LiteLLM gateway
    ANTHROPIC_MODEL      - Default model name
"""

import json
import os
import time
from functools import cache
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Settings discovery
# ---------------------------------------------------------------------------

SETTINGS_PATHS = [
    Path("/root/.claude/settings.json"),
    Path(__file__).resolve().parent.parent / "settings.json",
]


@cache
def _load_settings() -> dict:
    """Load settings from the first available settings file."""
    for path in SETTINGS_PATHS:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            env = data.get("env", {})
            return {
                "api_key": env.get("ANTHROPIC_AUTH_TOKEN", ""),
                "base_url": env.get("ANTHROPIC_BASE_URL", ""),
                "default_model": env.get("ANTHROPIC_MODEL", "claude-opus-5"),
                "source": str(path),
            }
    return {}


def get_config() -> dict:
    """
    Get the gateway configuration from settings.json only.

    Returns a dict with keys: api_key, base_url, default_model, source.
    """
    settings = _load_settings()
    return {
        "api_key": settings.get("api_key", ""),
        "base_url": settings.get("base_url", ""),
        "default_model": settings.get("default_model", "claude-opus-5"),
        "source": settings.get("source", ""),
    }


def _parse_custom_headers() -> dict:
    """Parse ANTHROPIC_CUSTOM_HEADERS env var into a dict.

    Claude Code reads this env var and forwards every entry as an HTTP header on
    each Anthropic API request it makes inside the subprocess. Parsing it here
    lets non-Claude callers (e.g. image/audio tool helpers) attach the same
    x-ninja-* tracking headers to their own LiteLLM requests without going
    through the Claude session.
    """
    raw = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
    if not raw:
        return {}
    result = {}
    for line in raw.strip().splitlines():
        if ": " in line:
            key, _, value = line.partition(": ")
            result[key.strip()] = value.strip()
    return result


def get_headers(extra: dict | None = None) -> dict:
    """Return standard Authorization + Content-Type headers, plus any x-ninja-* from env."""
    cfg = get_config()
    h = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    h.update(_parse_custom_headers())
    if extra:
        h.update(extra)
    return h


def api_url(path: str) -> str:
    """Build a full API URL from a relative path like '/v1/chat/completions'."""
    cfg = get_config()
    base = cfg["base_url"].rstrip("/")
    return f"{base}{path}"


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

#: HTTP status codes that indicate a transient server error worth retrying.
RETRIABLE_5XX = frozenset({500, 502, 503, 504})

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0  # seconds; delay = base * 2^(attempt-1) → 1s, 2s, 4s
_DEFAULT_TIMEOUT = 120  # seconds; prevents "read operation timed out" hangs


def litellm_request(
    method: str,
    path: str,
    *,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    backoff_base: float = _DEFAULT_BACKOFF_BASE,
    timeout: float | None = _DEFAULT_TIMEOUT,
    **kwargs,
) -> requests.Response:
    """Make an HTTP request to the LiteLLM gateway, retrying on transient 5xx errors.

    Args:
        method:      HTTP method (``"GET"``, ``"POST"``, etc.).
        path:        Gateway-relative path, e.g. ``"/v1/chat/completions"``.
        max_retries: Total number of attempts (default 3 → up to 2 retries).
        backoff_base: Base delay in seconds; doubles each attempt (1s, 2s, 4s).
        timeout:     Seconds to wait for a response (default 120). Pass ``None``
                     to disable the timeout. Without this default, slow or
                     unresponsive gateway requests hang indefinitely and surface
                     as "The read operation timed out" errors.
        **kwargs:    Forwarded verbatim to :func:`requests.request` (``json``,
                     ``data``, ``files``, ``headers``, …). Do not pass
                     ``timeout`` in kwargs; use the explicit parameter instead.

    Returns:
        The :class:`requests.Response` from the first successful (non-5xx) attempt
        *or* the final failed response after all retries are exhausted.

    Notes:
        * Only ``{500, 502, 503, 504}`` trigger a retry; 4xx and network errors
          are returned / raised immediately.
        * Callers are responsible for checking ``response.status_code`` and
          raising their own domain-specific errors.
        * If ``files`` are provided the caller must ensure they are seekable or
          pass fresh file handles on each call (this function does not re-open
          files between attempts). For multipart uploads that may be retried,
          open the files *inside* a ``for attempt`` loop and call this function
          once per attempt instead.
    """
    url = api_url(path)
    last_response: requests.Response | None = None

    for attempt in range(1, max_retries + 1):
        last_response = requests.request(method, url, timeout=timeout, **kwargs)

        if last_response.status_code not in RETRIABLE_5XX:
            return last_response

        if attempt < max_retries:
            time.sleep(backoff_base * (2 ** (attempt - 1)))

    return last_response  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

MODELS = {
    # Chat / Text models
    "claude-opus": "claude-opus-5",  # Default: latest Opus
    "claude-opus-5": "claude-opus-5",  # Explicit alias for the latest
    "claude-sonnet": "claude-sonnet-4-6",  # Was: claude-sonnet-4-5-20250929 (retired)
    "claude-sonnet-4-6": "claude-sonnet-4-6",  # Explicit alias
    "claude-haiku": "claude-haiku-4-5-20251001",
    "gpt-5": "openai/openai/gpt-5.5",  # Was: gpt-5.2 (retired); 5.5 is current
    "gpt-5.5": "openai/openai/gpt-5.5",  # Explicit alias
    "gpt-5.4": "openai/openai/gpt-5.4",  # Explicit alias (still available)
    "gpt-5.6-sol": "openai/openai/gpt-5.6-sol",  # Explicit alias
    "ninja-fast": "ninja-cline-fast",
    "ninja-standard": "ninja-cline-standard",
    "ninja-complex": "ninja-cline-complex",
    # Image models
    "gpt-image": "openai/openai/gpt-image-2.5-flare",  # Default (new): state-of-the-art, up to 2K, 16 reference images
    "gpt-image-2": "alias/openai/gpt-image-2.0",
    "gpt-image-2.5": "openai/openai/gpt-image-2.5-sunburst",
    "gpt-image-2.5-sunburst": "openai/openai/gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare": "openai/openai/gpt-image-2.5-flare",
    "gpt-image-1.5": "openai/openai/gpt-image-1.5",  # Legacy — kept for backward compatibility
    # Video has no alias — it is an MCP tool, see utils/video.py
    # Embedding models
    "embed-small": "openai/openai/text-embedding-3-small",
    "embed-large": "openai/openai/text-embedding-3-large",
    # Audio / transcription models
    "ninja-transcribe": "openai/openai/gpt-transcribe",  # Audio transcription (set to the gateway's id if it differs)
    "ninja-tts": "eleven-v3",  # Text to audio
}


def resolve_model(name: str) -> str:
    """
    Resolve a short model alias to its full gateway model ID.

    Examples:
        resolve_model("claude-opus")    -> "claude-opus-5"
        resolve_model("claude-sonnet")  -> "claude-sonnet-4-6"
        resolve_model("gpt-5")         -> "openai/openai/gpt-5.5"

    If the name is not a known alias, it is returned as-is (assumed to be
    a full model ID already).
    """
    return MODELS.get(name, name)


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = get_config()
    print(f"Source:  {cfg['source']}")
    print(f"Base:    {cfg['base_url']}")
    print(f"Key:     {cfg['api_key'][:10]}...{cfg['api_key'][-4:]}")
    print(f"Default: {cfg['default_model']}")
    print(f"\nModel aliases:")
    for alias, full in MODELS.items():
        print(f"  {alias:20s} -> {full}")
