"""Shared message processing utilities across all messaging adapters."""

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from core.metadata import load_sandbox_metadata
from messaging.pending import PendingMessage

# Set by the monitor when the pending batch has exactly one message, so the
# say/upload CLI can enforce its thread when the model drops the -t flag.
FORCE_THREAD_ENV = "NINJA_FORCE_THREAD_TS"
_NINJA_EMOJI = "\U0001f977"  # 🥷
_NINJA_SHORTCODE = ":ninja:"

# Whisper-compatible audio MIME types by extension. Voice notes are usually .m4a
# (mobile) or .webm (desktop). The transcription endpoint rejects a file whose
# declared name/type don't match its bytes ("corrupted or unsupported"), so the
# format must be detected — never hardcoded.
_AUDIO_MIME = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".mpeg": "audio/mpeg",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


def audio_upload_part(url: str, resp: Any) -> tuple:
    """Build the multipart ``file`` tuple for a transcription upload.

    Detects the audio format so a voice note is never mislabeled (e.g. an m4a
    sent as webm → rejected). Priority: the URL's file extension (channels like
    Slack encode it in the download URL), then the download's ``Content-Type``,
    defaulting to m4a — the common voice-note format. Always returns a
    whisper-accepted ``audio/*`` type. ``resp`` is a requests-style response
    (``.headers``/``.content``). Returns ``(filename, bytes, mime)``.
    """
    ext = Path(urlparse(url or "").path).suffix.lower()
    if ext not in _AUDIO_MIME:
        # No usable extension on the URL — map the download's declared audio type
        # back to a known extension (reverse lookup, not mimetypes.guess_extension
        # which doesn't know audio/webm etc.). Unknown/generic types default to
        # m4a, the common voice-note format.
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = next((e for e, m in _AUDIO_MIME.items() if m == ctype), ".m4a")
    # Extension is now always a known audio format → MIME is always audio/*.
    return (f"audio{ext}", resp.content, _AUDIO_MIME[ext])


# Extensions that are unambiguously audio — safe to classify a mislabeled or
# generically-typed attachment as audio by name alone. Container formats that
# are commonly video (.mp4, .mpeg, .webm) are excluded: those are treated as
# audio only when the content-type explicitly says audio/*.
_UNAMBIGUOUS_AUDIO_EXTS = {".m4a", ".mp3", ".mpga", ".wav", ".flac", ".ogg", ".oga"}


def has_audio_ext(name: str, url: str) -> bool:
    """True if a filename or URL path ends in an unambiguous audio extension."""
    return any(
        Path(urlparse(v or "").path).suffix.lower() in _UNAMBIGUOUS_AUDIO_EXTS
        for v in (name, url)
    )


def resolve_reply_thread(explicit_thread: Optional[str]) -> Optional[str]:
    """Return the reply thread: explicit -t wins, else the enforced env value."""
    if explicit_thread:
        return explicit_thread
    forced = (os.environ.get(FORCE_THREAD_ENV) or "").strip()
    return forced or None


def forced_thread_for_batch(pending_messages: list[PendingMessage]) -> Optional[str]:
    """Return the thread_id to enforce when the batch has exactly one message.

    collect_pending points top-level messages at themselves and thread replies
    at their thread root, so one rule covers both. Multi-message batches return
    None: routing stays with the model's per-message -t hints.
    """
    if len(pending_messages) != 1:
        return None
    thread_id = pending_messages[0].thread_id
    return str(thread_id) if thread_id else None


def mark_handled(
    seen_messages: set,
    agent_data: Dict[str, Any],
    pending_messages: list[PendingMessage],
) -> None:
    """Record a dispatched batch as done, so it is never picked up again.

    The monitor calls this once a run has answered the batch — or once it has
    given up on it. A batch that is queued but neither answered nor abandoned
    stays unmarked, so the next poll retries it instead of losing the question.
    """
    seen_replies = dict.fromkeys(agent_data.get("seen_replies", []))
    for message in pending_messages:
        message_id = message.id
        parent_id = message.thread_id
        # Cron entries are synthetic (``cron:<id>:<unix>``) and de-duplicated by
        # claim_cron, so they must not consume the bounded seen store.
        if not message_id or message.type == "cron":
            continue
        if parent_id and parent_id != message_id:
            seen_replies[f"{parent_id}:{message_id}"] = None
        else:
            seen_messages.add(message_id)
    agent_data["seen_replies"] = list(seen_replies)


# Sandbox URL conversion. Converts 0.0.0.0:<port> references in messages to
# public sandbox URLs. Every adapter must run this before rendering: a bare
# 0.0.0.0:<port> is not a URL, so Markdown renderers leave it as inert text.
# Reads sandbox_id and stage from /dev/shm/sandbox_metadata.json via core.metadata.
#
# Pattern: 0.0.0.0:<port> → <port>-<sandbox_id>.app.super.<stage>myninja.ai
# Example: 0.0.0.0:8080 → 8080-134212d3-8907-4593-8090-b21ec7365c33.app.super.betamyninja.ai

# Regex to match 0.0.0.0:<port> (port = 1-5 digit number)
_PORT_URL_PATTERN = re.compile(r"0\.0\.0\.0:(\d{1,5})")


def convert_sandbox_urls(text: str, *, force_public_urls: bool = False) -> str:
    """
    Convert 0.0.0.0:<port> patterns in text to public sandbox URLs.

    In a cloud sandbox (LOCAL_DEVELOPMENT_MODE not set):
        0.0.0.0:<port> → https://<port>-<sandbox_id>.app.super.<stage>myninja.ai

    When LOCAL_DEVELOPMENT_MODE=True (local docker-compose):
        0.0.0.0:<port> → http://localhost:<port>


    Args:
        text: Message text that may contain 0.0.0.0:<port> references

    Returns:
        Text with all 0.0.0.0:<port> replaced with public or local URLs.
    """
    local_mode = os.environ.get("LOCAL_DEVELOPMENT_MODE", "").lower() in (
        "true",
        "1",
        "yes",
    )
    proxy_host = os.environ.get("SANDBOX_PROXY_HOST", "").strip().rstrip("/")
    proxy_id = os.environ.get("SANDBOX_PROXY_ID", "").strip()

    if local_mode and not force_public_urls and not (proxy_host and proxy_id):
        # Local / docker-compose — use localhost
        def _replace_port(match):
            port = match.group(1)
            return f"http://localhost:{port}"

        return _PORT_URL_PATTERN.sub(_replace_port, text)

    if proxy_host and proxy_id:
        sandbox_id = proxy_id
        sandbox_domain = f"app.{proxy_host}"
    else:
        # Cloud sandbox — build the full public URL from sandbox metadata
        metadata = load_sandbox_metadata()
        if not metadata:
            return text

        sandbox_id = metadata.get("thread_id", "")
        stage = metadata.get("environment", "")
        if not sandbox_id:
            return text
        prefix = f"{stage}" if stage and stage != "prod" else ""
        sandbox_domain = f"app.super.{prefix}myninja.ai"

    def _replace_port(match):
        port = match.group(1)
        return f"https://{port}-{sandbox_id}.{sandbox_domain}"

    return _PORT_URL_PATTERN.sub(_replace_port, text)


def _first_present(message: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = message.get(key)
        if value not in (None, ""):
            return value
    return default


def is_bot_message(message: Dict[str, Any]) -> bool:
    """True if message was posted by an application (bot), not a user.

    Works for both Teams (from_application_id) and Slack (bot_profile).
    For Teams with delegated tokens, also detects :ninja: emoji signature.
    """
    # Teams check: official app identity
    if message.get("from_application_id"):
        return True
    # Slack check
    if message.get("bot_profile"):
        return True
    # Teams delegated token: check for :ninja: emoji signature (bot self-marks)
    text = message.get("text", "").strip()
    if _NINJA_EMOJI in text or _NINJA_SHORTCODE in text:
        return True
    return False


def extract_file_attachments(
    message: Dict[str, Any],
    event_id: str = "",
) -> Dict[str, list]:
    """Categorize a message's attachments by type (audio/image/pdf/other).

    The single classifier for every adapter.  When the message carries the
    new-style ``attachments`` array (populated by the event-cache service with
    ``id``, ``name``, ``mimetype``, ``size``, ``url``, and ``type`` fields),
    that is used.  Otherwise the legacy ``files`` array is read and classified
    using mimetype heuristics.

    *event_id* is threaded through to every returned entry so that downstream
    consumers (transcription scripts, download utilities) can call
    ``AgentEventCacheClient.download_file(event_id, file_id)`` without needing
    any other context.

    Returns a dict with keys: audio_files, image_files, pdf_files, other_files.
    Each entry exposes id, event_id, name, mimetype, size, and type.
    """
    audio_files, image_files, pdf_files, other_files = [], [], [], []

    # Prefer new-style attachments (from /db/messages) over legacy files.
    raw_attachments = message.get("attachments")
    if (
        isinstance(raw_attachments, list)
        and raw_attachments
        and _is_new_style(raw_attachments[0])
    ):
        for att in raw_attachments:
            entry = {
                "id": att.get("id") or "",
                "event_id": event_id,
                "name": att.get("name") or "unknown",
                "mimetype": (att.get("mimetype") or "").lower(),
                "size": att.get("size") or 0,
                "type": att.get("type") or "other",
            }
            att_type = entry["type"]
            if att_type == "audio":
                audio_files.append(entry)
            elif att_type == "image":
                image_files.append(entry)
            elif att_type == "pdf":
                pdf_files.append(entry)
            else:
                if entry["name"] != "unknown" or entry["id"]:
                    other_files.append(entry)
        return {
            "audio_files": audio_files,
            "image_files": image_files,
            "pdf_files": pdf_files,
            "other_files": other_files,
        }

    # Legacy path: classify from raw provider file objects.
    for f in message.get("files") or []:
        content_type = (f.get("content_type") or f.get("mimetype") or "").lower()
        subtype = f.get("subtype") or ""
        name = f.get("name") or "unknown"
        url = (
            f.get("url")
            or f.get("content_url")
            or f.get("web_url")
            or f.get("url_private_download")
            or ""
        )
        entry = {
            "id": f.get("id") or "",
            "event_id": event_id,
            "name": name,
            "mimetype": content_type,
            "size": f.get("size") or 0,
            "type": "",
        }
        if (
            content_type.startswith("audio/")
            or subtype == "voice_message"
            or has_audio_ext(name, url)
        ):
            entry["type"] = "audio"
            audio_files.append(entry)
        elif content_type.startswith("image/"):
            entry["type"] = "image"
            image_files.append(entry)
        elif content_type == "application/pdf":
            entry["type"] = "pdf"
            pdf_files.append(entry)
        elif name != "unknown" or entry["id"]:
            entry["type"] = "other"
            other_files.append(entry)
    return {
        "audio_files": audio_files,
        "image_files": image_files,
        "pdf_files": pdf_files,
        "other_files": other_files,
    }


def _is_new_style(item: Any) -> bool:
    """Return True if *item* looks like a new-style ``Attachment`` dict
    (has ``type`` with a known classification value and ``url`` pointing at
    ``/files/download``).
    """
    if not isinstance(item, dict):
        return False
    return item.get("type") in {
        "audio",
        "image",
        "pdf",
        "other",
    } and "/files/download" in (item.get("url") or "")


def classify_message_type(attachments: Dict[str, list], is_reply: bool) -> str:
    """Derive a message type from attachments + position (attachment wins)."""
    if attachments["audio_files"]:
        return "audio_message"
    if (
        attachments["image_files"]
        or attachments["pdf_files"]
        or attachments["other_files"]
    ):
        return "file_message"
    return "thread_reply" if is_reply else "mention"


def normalize_cached_message(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return the monitor-facing message shape for generic cached rows."""
    if not isinstance(item, dict):
        return {}

    attachments = item.get("attachments")
    if not isinstance(attachments, list):
        attachments = item.get("files") if isinstance(item.get("files"), list) else []

    normalized = dict(item)
    normalized.update(
        {
            "id": str(_first_present(item, "id", "message_id", "ts", default="")),
            "created": _first_present(
                item,
                "created",
                "createdDateTime",
                "timestamp",
                "ts",
                default="",
            ),
            "from": _first_present(
                item,
                "from",
                "user_name",
                "username",
                "user",
                default="Unknown",
            ),
            "from_user_id": _first_present(
                item,
                "from_user_id",
                "user_id",
                "user",
            ),
            "text": _first_present(item, "text", "body_text", default=""),
            "web_url": _first_present(item, "web_url", "webUrl"),
            "attachments": attachments,
            "files": attachments,
        }
    )
    normalized.setdefault("raw", item)
    return normalized
