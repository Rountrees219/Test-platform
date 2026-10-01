"""WhatsApp monitor service — gateway poll loop and Claude dispatch."""

from __future__ import annotations

import argparse
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from agent_providers.base import AgentRunConfig
from agent_providers.claude.claude_output import ClaudeResult, primary_model
from agent_providers.claude.claude_provider import ClaudeProvider
from clients.posthog_client import is_feature_enabled
from clients.super_ninja_client import get_thread_id
from constants import DEFAULT_TASK_TITLE, WHATSAPP_SHARED_MONITOR_FLAG
from core.logging import get_logger
from services.whatsapp.recent_context import MAX_MESSAGES, record_message, render_recent
from utils.cost import build_custom_headers, generate_task_title, record_task_cost

logger = get_logger("whatsapp")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SETTINGS_PATH = Path.home() / ".agent_settings.json"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8090"

WA_CLI = "python messaging/whatsapp/interface.py"

SYSTEM_PROMPT_TEMPLATE = (
    REPO_ROOT / "messaging" / "whatsapp" / "gateway" / "system-prompt.md"
)
SYSTEM_PROMPT_RENDERED = Path("/tmp/ninja-wa-system-prompt.md")

POLL_INTERVAL = 1
POLL_JITTER = 0.3
MAX_RUNTIME = 24 * 60 * 60
IDLE_LOG_EVERY = 30  # seconds between idle (unbound) log lines
# ---------------------------------------------------------------------------
# settings I/O (compatible with whatsapp_interface.py)
# ---------------------------------------------------------------------------


def resolve_agent_done_emoji() -> str:
    """Done reaction after dispatch; ack stays 👀 via PHANTOM_AGENT_EMOJI."""
    return os.environ.get(
        "PHANTOM_AGENT_DONE_EMOJI",
        os.environ.get("NINJA_AGENT_DONE_EMOJI")
        or os.environ.get("NINJA_AGENT_EMOJI")
        or "🥷",
    ).strip()


def _read_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_whatsapp_settings(patch: dict[str, Any]) -> None:
    settings = _read_settings()
    existing = (
        settings.get("whatsapp") if isinstance(settings.get("whatsapp"), dict) else {}
    )
    merged = {**existing, **patch}
    settings["whatsapp"] = merged
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def _settings_whatsapp() -> dict[str, Any]:
    s = _read_settings().get("whatsapp")
    return s if isinstance(s, dict) else {}


# ---------------------------------------------------------------------------
# gateway HTTP client
# ---------------------------------------------------------------------------


def _gateway_url() -> str:
    from messaging.whatsapp._config import gateway_url

    return gateway_url()


def _gateway_token() -> Optional[str]:
    from messaging.whatsapp._config import gateway_token

    return gateway_token()


def _gateway_react(*args: Any, **kwargs: Any) -> bool:
    # Deferred: interface imports monitor_service helpers; react stays in interface.
    from messaging.whatsapp.interface import react

    return react(*args, **kwargs)


def _request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 15.0,
) -> tuple[int, bytes]:
    data = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        return 0, f"connection failed: {e.reason}".encode("utf-8")


def _gateway_send(base: str, token: Optional[str], chat_jid: str, text: str) -> bool:
    """Best-effort send into the bound chat (used to surface hard failures)."""
    if not chat_jid or not text:
        return False
    if chat_jid.endswith("@g.us"):
        body: dict[str, Any] = {"group_jid": chat_jid, "text": text}
    else:
        digits = chat_jid.split("@", 1)[0].split(":", 1)[0]
        if not digits.isdigit():
            return False
        body = {"to": digits, "text": text}
    status, _ = _request("POST", f"{base}/send", token=token, body=body, timeout=10.0)
    return status == 200


def _get_status(base: str, token: Optional[str]) -> Optional[dict[str, Any]]:
    status, raw = _request("GET", f"{base}/status", token=token)
    if status != 200:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _get_messages(
    base: str, token: Optional[str], since: int, limit: int
) -> Optional[dict[str, Any]]:
    url = f"{base}/messages?since={since}&limit={limit}"
    status, raw = _request("GET", url, token=token)
    if status != 200:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Cursor state
# ---------------------------------------------------------------------------


def _load_cursor() -> tuple[int, Optional[int]]:
    """Return (last_read_seq, last_read_inbox_epoch) from settings."""
    s = _settings_whatsapp()
    raw_seq = s.get("monitor_last_read_seq", s.get("last_read_seq"))
    raw_epoch = s.get("monitor_last_read_inbox_epoch", s.get("last_read_inbox_epoch"))
    seq = int(raw_seq) if isinstance(raw_seq, (int, float)) and raw_seq >= 0 else 0
    epoch = (
        int(raw_epoch)
        if isinstance(raw_epoch, (int, float)) and raw_epoch > 0
        else None
    )
    return seq, epoch


def _save_cursor(seq: int, epoch: Optional[int]) -> None:
    patch: dict[str, Any] = {"monitor_last_read_seq": seq}
    if epoch is not None:
        patch["monitor_last_read_inbox_epoch"] = epoch
    _write_whatsapp_settings(patch)


# ---------------------------------------------------------------------------
# Agent dispatch
# ---------------------------------------------------------------------------


def _derive_targets(bound_chat_jid: str) -> tuple[str, str]:
    """Build (reply_hint, upload_hint) for the bound chat.

    Both _build_prompt and _render_system_prompt call this; they MUST stay in
    sync — diverging hints would tell the agent the wrong destination flag.
    """
    if bound_chat_jid.endswith("@g.us"):
        reply_hint = (
            f'{WA_CLI} say "<your reply>" '
            f"--ninja-prefix --group-jid {bound_chat_jid}"
        )
        upload_target = f"--group-jid {bound_chat_jid}"
    else:
        digits = "".join(c for c in bound_chat_jid.split("@")[0] if c.isdigit())
        reply_hint = f'{WA_CLI} say "<your reply>" ' f"--ninja-prefix --to {digits}"
        upload_target = f"--to {digits}"
    upload_hint = (
        f"{WA_CLI} upload --kind image|document "
        f'--file <path> [--caption "<text>"] --ninja-prefix {upload_target}'
    )
    return reply_hint, upload_hint


def _render_system_prompt(bound_chat_jid: str) -> Optional[Path]:
    """Render the WhatsApp system prompt for this bound chat.

    Returns None on template/disk failure — dispatch then skips
    --append-system-prompt-file rather than failing the turn.
    """
    try:
        template = SYSTEM_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    _, upload_hint = _derive_targets(bound_chat_jid)
    rendered = template.replace("{BOUND_CHAT_JID}", bound_chat_jid).replace(
        "{UPLOAD_HINT}", upload_hint
    )
    try:
        SYSTEM_PROMPT_RENDERED.write_text(rendered, encoding="utf-8")
    except OSError:
        return None
    return SYSTEM_PROMPT_RENDERED


def _record_pending(pending: list[dict[str, Any]]) -> None:
    """Record inbound user lines into the shared recent_context buffer.

    Must be called ONCE in the main loop before fan-out — calling from workers
    would race on insertion order across the pool.
    """
    for msg in pending:
        record_message(
            "user",
            (msg.get("text") or "").replace("\n", " "),
            ts=msg.get("timestamp"),
        )


def _build_prompt(
    pending: list[dict[str, Any]],
    bound_chat_jid: str,
    *,
    current_tag: Optional[str] = None,
    same_session_continuation: bool = False,
) -> str:
    """Build a batched WhatsApp turn prompt. Pure — no side effects.

    current_tag filters recent_context to lines the calling session does NOT
    already have in its --resume history. same_session_continuation drops the
    persona-unification preamble; only safe when the worker that handled the
    last turn for this chat is handling this one too.
    """
    reply_hint, _ = _derive_targets(bound_chat_jid)

    # MUST run before _record_pending or pending msgs round-trip into their own context.
    history = render_recent(unified=True, exclude_tag=current_tag)

    lines: list[str] = []
    for i, msg in enumerate(pending, 1):
        text = (msg.get("text") or "").replace("\n", " ")
        # Sender fallback: pushName > participant > user_id.
        sender = (
            msg.get("sender_name")
            or msg.get("participant")
            or msg.get("user")
            or "unknown"
        )

        block = [
            f"--- Message {i} (whatsapp) ---",
            f"From: {sender}",
            f"Time: {msg.get('timestamp', '')}",
            f"Text: {text}" if text else "Text: (none)",
        ]

        # Gateway-truncated at 1000 chars + ellipsis.
        quoted_text = msg.get("quoted_text")
        if quoted_text:
            qsender = msg.get("quoted_sender") or "(unknown)"
            block.append(f"↪️ Replying to {qsender}: {quoted_text}")

        # Bytes fetched via `whatsapp_interface fetch-media` — auth resolved
        # from settings/env, never embed the literal token in the prompt.
        kind = msg.get("media_kind")
        media_id = msg.get("media_id")
        if kind and media_id:
            fetch_cmd = (
                f"{WA_CLI} fetch-media "
                f"--media-id {media_id} --out /tmp/wa_{media_id}"
            )
            mime = msg.get("media_mimetype") or "?"
            size = msg.get("media_bytes")
            size_kb = (
                f"{size // 1024} KB" if isinstance(size, (int, float)) and size else "?"
            )
            if kind == "voice":
                seconds = msg.get("media_seconds")
                dur = (
                    f"{seconds}s"
                    if isinstance(seconds, (int, float)) and seconds
                    else "?"
                )
                block.append(
                    f"🎤 Voice note ({dur}, {mime}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Transcribe: python messaging/whatsapp/transcribe.py /tmp/wa_{media_id}\n"
                    f"   Treat the transcript as the user's text."
                )
            elif kind == "image":
                block.append(
                    f"🖼 Image ({size_kb}, {mime}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Read it with the Read tool and respond about what you see."
                )
            elif kind == "pdf":
                fname = msg.get("media_filename") or f"{media_id}.pdf"
                block.append(
                    f"📄 PDF document: {fname} ({size_kb}, {mime}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Read the PDF with the Read tool and answer "
                    f"questions about its contents."
                )
            elif kind == "archive":
                fname = msg.get("media_filename") or f"{media_id}.zip"
                block.append(
                    f"🗜 Archive: {fname} ({size_kb}, {mime}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Then unzip into /tmp/wa_{media_id}_unpacked/ "
                    f"(`unzip -o /tmp/wa_{media_id} -d /tmp/wa_{media_id}_unpacked`) "
                    f"and inspect the extracted files before replying."
                )
            elif kind == "text":
                fname = msg.get("media_filename") or f"{media_id}.txt"
                block.append(
                    f"📝 Text file: {fname} ({size_kb}, {mime}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Read the file directly (it is text — `cat`, `head`, or "
                    f"open with the Read tool) and answer about its contents."
                )
            elif kind == "other":
                fname = msg.get("media_filename") or f"{media_id}.bin"
                block.append(
                    f"📎 File: {fname} ({size_kb}, {mime}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Inspect the file with an appropriate tool "
                    f"(Read, ffprobe, unzip, etc.) before replying."
                )
            else:
                fname = msg.get("media_filename") or f"{media_id}.bin"
                block.append(
                    f"📎 Unknown media kind '{kind}': {fname} ({size_kb}, {mime}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Inspect the downloaded bytes before replying."
                )

        block.append(f"Reply with: {reply_hint}")
        lines.append("\n".join(block))
    body = "\n\n".join(lines)
    recent_section = ""
    if history:
        if same_session_continuation:
            recent_section = f"{history}\n\n"
        else:
            recent_section = (
                f"This is YOUR ongoing conversation with the user (last "
                f"{MAX_MESSAGES} lines, oldest first, newest last). Every line "
                "marked 'Ninja:' is something YOU already said — even if a "
                "different internal worker produced it. Treat it ALL as your own "
                "memory and continue as ONE consistent assistant: never say 'the "
                "other session', never disown or contradict a prior Ninja reply, "
                "and don't re-ask what was already answered here. If a prior Ninja "
                "line committed to something, honour it.\n"
                f"{history}\n\n"
            )
    return (
        "You are 🥷 Ninja, the WhatsApp agent.\n\n"
        + recent_section
        + f"You have {len(pending)} message(s) to handle.\n\n"
        f"{body}\n"
    )


def _dispatch_to_claude(
    prompt: str,
    task_id: str,
    timeout: Optional[int] = None,
    session: Optional[str] = None,
    tag: Optional[str] = None,
    bound_chat_jid: Optional[str] = None,
    extra_env: Optional[dict] = None,
) -> tuple[bool, Optional[str], Optional[ClaudeResult]]:
    """Returns ``(ok, user_facing_error_or_None, parsed_result_or_None)``."""
    if timeout is None:
        try:
            timeout = int(os.environ.get("WHATSAPP_DISPATCH_TIMEOUT", "1500"))
        except (TypeError, ValueError):
            timeout = 1500

    sp_path = None
    if bound_chat_jid:
        sp_path = _render_system_prompt(bound_chat_jid)

    session_name = (tag or f"whatsapp-{session[:8]}") if session else "whatsapp"
    env = {}
    if tag:
        env["WHATSAPP_SESSION_TAG"] = tag
    if extra_env:
        env.update(extra_env)
    if "ANTHROPIC_CUSTOM_HEADERS" not in {**os.environ, **env}:
        conversation_id = get_thread_id()
        env["ANTHROPIC_CUSTOM_HEADERS"] = build_custom_headers(
            task_id, DEFAULT_TASK_TITLE, conversation_id
        )

    spec = AgentRunConfig(
        prompt=prompt,
        task_id=task_id,
        system_prompt_path=sp_path,
        system_prompt_mode="append",
        tools=["Bash", "Edit", "Read", "Skill", "Write"],
        timeout_seconds=timeout,
        session_name=session_name,
        cwd=REPO_ROOT,
        env=env,
        process_label="whatsapp",
    )

    provider = ClaudeProvider()
    result = provider.run(spec, logger)

    parsed = result.parsed
    text_output = parsed.text if parsed else (result.stdout or "")
    if text_output:
        logger.info(text_output[-1000:])
    if result.stderr:
        logger.warning(result.stderr[-1000:])

    if result.exit_code == 0:
        return True, None, parsed
    tail = (result.stderr or text_output or "").strip().splitlines()
    last = tail[-1] if tail else ""
    return (
        False,
        f"Claude exited {result.exit_code}" + (f": {last[:200]}" if last else ""),
        parsed,
    )


# --- async heavy-turn dispatch (ops/pr split) -------------------------------
# Heavy turns (builds, deploys) run in a background worker so the main loop
# stays free for light messages. Only one heavy at a time (two builds OOM).
# WHATSAPP_ASYNC_HEAVY=0 → every turn synchronous (old behaviour).
ASYNC_HEAVY = os.environ.get("WHATSAPP_ASYNC_HEAVY", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "",
)
_HEAVY_Q: "queue.Queue" = queue.Queue()
_HEAVY_WORKER_STARTED = False
# Single-session path uses `-c` (resume last); two concurrent claudes would
# corrupt the resumed session. All turns serialize through the worker.
_WORKER_BUSY = threading.Event()

_LAST_HANDLED_BY: dict[str, str] = {}
_LAST_HANDLED_LOCK = threading.Lock()


def _read_last_handler(chat_jid: str) -> Optional[str]:
    with _LAST_HANDLED_LOCK:
        return _LAST_HANDLED_BY.get(chat_jid)


def _set_last_handler(chat_jid: str, tag: str) -> None:
    with _LAST_HANDLED_LOCK:
        _LAST_HANDLED_BY[chat_jid] = tag


# /compact is a Claude Code slash command — slash commands in -p headless mode
# are unverified. Default 0 (disabled) until tested in your install.
COMPACT_AFTER_TURNS = max(
    0, int(os.environ.get("WHATSAPP_COMPACT_AFTER_TURNS", "0") or "0")
)
COMPACT_PROMPT = (
    os.environ.get("WHATSAPP_COMPACT_PROMPT", "/compact").strip() or "/compact"
)
_SESSION_TURN_COUNTS: dict[str, int] = {}
_SESSION_TURN_LOCK = threading.Lock()


def _bump_session_turn_count(session_id: str) -> int:
    with _SESSION_TURN_LOCK:
        _SESSION_TURN_COUNTS[session_id] = _SESSION_TURN_COUNTS.get(session_id, 0) + 1
        return _SESSION_TURN_COUNTS[session_id]


def _reset_session_turn_count(session_id: str) -> None:
    with _SESSION_TURN_LOCK:
        _SESSION_TURN_COUNTS[session_id] = 0


def _maybe_compact_session(
    session_id: Optional[str],
    tag: Optional[str],
    bound_chat_jid: Optional[str],
) -> None:
    """Compact the session inline if its turn count crossed the threshold.

    Blocks the calling worker (NOT the main loop). Counter is reset only on
    success — failed compactions retry next turn.
    """
    if not session_id or COMPACT_AFTER_TURNS <= 0:
        return
    with _SESSION_TURN_LOCK:
        count = _SESSION_TURN_COUNTS.get(session_id, 0)
    if count < COMPACT_AFTER_TURNS:
        return
    label = tag or session_id
    logger.info(
        "compacting session %s (turns=%s >= %s)",
        label,
        count,
        COMPACT_AFTER_TURNS,
    )
    ok, err, _parsed = _dispatch_to_claude(
        COMPACT_PROMPT,
        task_id=str(uuid.uuid4()),
        session=session_id,
        tag=tag,
        bound_chat_jid=bound_chat_jid,
    )
    if ok:
        _reset_session_turn_count(session_id)
        logger.info("compaction done (%s)", label)
    else:
        logger.warning("compaction failed (%s): %s", label, err)


# True 2-session: light and heavy on SEPARATE Claude sessions so they run
# concurrently (ops answers while pr builds) without the `-c` collision.
# Fixed uuids so each role resumes its own stable thread; context carries
# via the memory file. WHATSAPP_TRUE_2SESSION=0 falls back to serial.
TRUE_2SESSION = os.environ.get("WHATSAPP_TRUE_2SESSION", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "",
)
OPS_SESSION_ID = os.environ.get(
    "WHATSAPP_OPS_SESSION_ID", "11111111-0000-4000-8000-000000000001"
)
PR_SESSION_ID = os.environ.get(
    "WHATSAPP_PR_SESSION_ID", "22222222-0000-4000-8000-000000000002"
)

# Light-session POOL. Pinning every light turn to a single foreground session
# blocked the poll loop → back-to-back msgs batched. Pool of N worker sessions,
# each its own queue + thread:
#   - main loop never blocks (enqueue + keep polling)
#   - up to N light turns answer CONCURRENTLY (distinct session ids)
#   - light never waits behind heavy (separate lane)
# Heavy stays single-serial (two builds OOM).
# Rollback: WHATSAPP_LIGHT_POOL=0 → old single-ops foreground path.
LIGHT_POOL = os.environ.get("WHATSAPP_LIGHT_POOL", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "",
)
try:
    LIGHT_POOL_SIZE = max(1, int(os.environ.get("WHATSAPP_LIGHT_POOL_SIZE", "2")))
except (TypeError, ValueError):
    LIGHT_POOL_SIZE = 2


def _ops_session_ids(n: int) -> list:
    """N distinct ops session ids. WHATSAPP_OPS_SESSION_IDS (csv) overrides;
    else worker 0 uses OPS_SESSION_ID and the rest derive deterministically."""
    raw = os.environ.get("WHATSAPP_OPS_SESSION_IDS")
    if raw:
        ids = [s.strip() for s in raw.split(",") if s.strip()]
        if ids:
            return ids
    ids = [OPS_SESSION_ID]
    for i in range(1, n):
        ids.append(f"11111111-0000-4000-8000-{str(i + 1).zfill(12)}")
    return ids


# Conservative: anything not matched is LIGHT, so a misclassify degrades to
# the synchronous old path, never worse.
_HEAVY_RE = re.compile(
    r"\b(implement|build|rebuild|deploy|redeploy|merge|rebase|refactor|migrat\w*|"
    r"pnpm|npm\s+install|next\s+build|tsc|compile|commit|push\s+to|"
    r"open\s+(a\s+)?pr|pull\s+request|create\s+(a\s+)?branch|clone|"
    r"spin\s+up|bring\s+up|set\s+up|wire\s+up|scaffold|"
    r"add\s+.{0,30}(feature|endpoint|route|page|screen|column|table)|"
    r"fix\s+.{0,30}(bug|code|build)|write\s+.{0,30}(code|test|migration))\b",
    re.IGNORECASE,
)

_NINJA_OUTBOUND_PREFIX = "🥷 Ninja"


def resolve_include_from_me(*, bound_via: Optional[str], cli_include: bool) -> bool:
    if cli_include:
        return True
    return bound_via in ("auto_group", "pairing_code")


def should_dispatch_inbound_item(
    item: dict[str, Any],
    *,
    include_from_me: bool,
    bound_via: Optional[str] = None,
) -> bool:
    from_me = bool(item.get("from_me"))
    if from_me and not include_from_me:
        return False
    text = (item.get("text") or "").strip()
    media_kind = item.get("media_kind")
    if not text and not media_kind:
        return False
    if from_me and text.startswith(_NINJA_OUTBOUND_PREFIX):
        if bound_via not in ("auto_group", "pairing_code"):
            return False
    return True


def inbound_item_to_pending(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "user": it.get("user_id") or "unknown",
        "text": it.get("text") or "",
        "timestamp": it.get("ts"),
        "seq": it.get("seq"),
        "channel_id": it.get("channel_id"),
        "type": "whatsapp",
        "message_key": it.get("message_key"),
        "from_me": bool(it.get("from_me")),
        "participant": it.get("participant"),
        "sender_name": it.get("sender_name"),
        "media_kind": it.get("media_kind"),
        "media_id": it.get("media_id"),
        "media_mimetype": it.get("media_mimetype"),
        "media_seconds": it.get("media_seconds"),
        "media_bytes": it.get("media_bytes"),
        "media_filename": it.get("media_filename"),
        "quoted_message_key": it.get("quoted_message_key"),
        "quoted_text": it.get("quoted_text"),
        "quoted_sender": it.get("quoted_sender"),
    }


def filter_inbox_for_dispatch(
    raw_items: list[dict[str, Any]],
    *,
    include_from_me: bool,
    bound_via: Optional[str] = None,
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for it in raw_items:
        if not should_dispatch_inbound_item(
            it,
            include_from_me=include_from_me,
            bound_via=bound_via,
        ):
            continue
        pending.append(inbound_item_to_pending(it))
    return pending


def is_heavy_batch(pending: list[dict[str, Any]]) -> bool:
    try:
        text = " ".join(str(p.get("text") or "") for p in pending)
        return bool(_HEAVY_RE.search(text))
    except Exception:
        return False


def should_cold_start_skip_backlog(
    *,
    cold_start_done: bool,
    latest_seq: int,
    last_seq: int,
) -> bool:
    return not cold_start_done and latest_seq > last_seq


def _is_heavy(pending: list) -> bool:
    return is_heavy_batch(pending)


def _handle_dispatch_result(
    ok, err_msg, acked, base, token, chat_jid, done_emoji
) -> None:
    """Surface hard failures + swap the ack reaction. Shared sync + bg path."""
    if not ok and err_msg:
        if not _gateway_send(
            base, token, chat_jid, f"\U0001f977 Ninja: \u274c {err_msg}"
        ):
            logger.warning("failed to surface error to chat: %s", err_msg)
    if ok and done_emoji and acked:
        done = 0
        for a in acked:
            if _gateway_react(
                base,
                token,
                a["message_key"],
                done_emoji,
                from_me=a["from_me"],
                participant=a["participant"],
            ):
                done += 1
        if done:
            logger.info("%s marked %s message(s) done", done_emoji, done)


def _heavy_worker() -> None:
    """Serial worker: one heavy turn at a time so two builds never overlap."""
    while True:
        job = _HEAVY_Q.get()
        _WORKER_BUSY.set()
        try:
            started = time.time()
            chat_jid = job.get("chat", "")
            tag = job.get("tag")
            session_id = job.get("session")
            same = bool(tag) and _read_last_handler(chat_jid) == tag
            texts = [m.get("text", "") for m in job["pending"] if m.get("text")]
            task_id = str(uuid.uuid4())
            combined = " | ".join(texts)
            conversation_id = get_thread_id()
            title = (
                (
                    generate_task_title(
                        combined, task_id=task_id, conversation_id=conversation_id
                    )
                    if combined
                    else None
                )
                or (combined[:50] + "…" if len(combined) > 50 else combined)
                or DEFAULT_TASK_TITLE
            )
            custom_headers = build_custom_headers(task_id, title, conversation_id)
            prompt = _build_prompt(
                job["pending"],
                chat_jid,
                current_tag=tag,
                same_session_continuation=same,
            )
            ok, err, parsed = _dispatch_to_claude(
                prompt,
                task_id=task_id,
                session=session_id,
                tag=tag,
                bound_chat_jid=chat_jid,
                extra_env={"ANTHROPIC_CUSTOM_HEADERS": custom_headers},
            )
            cost = parsed.total_cost_usd if parsed else 0.0
            model = primary_model(parsed) if parsed else ""
            threading.Thread(
                target=record_task_cost,
                args=(texts, title, cost),
                kwargs={
                    "model": model,
                    "task_id": task_id,
                    "conversation_id": conversation_id,
                },
                daemon=True,
            ).start()
            if ok:
                if tag:
                    _set_last_handler(chat_jid, tag)
                if session_id:
                    _bump_session_turn_count(session_id)
                    _maybe_compact_session(session_id, tag, chat_jid)
            logger.info(
                "heavy turn done in %.1fs (qsize=%s)",
                time.time() - started,
                _HEAVY_Q.qsize(),
            )
            _handle_dispatch_result(
                ok,
                err,
                job["acked"],
                job["base"],
                job["token"],
                job["chat"],
                job["done_emoji"],
            )
        except Exception as e:  # never let a job kill the worker
            try:
                _gateway_send(
                    job["base"],
                    job["token"],
                    job["chat"],
                    f"\U0001f977 Ninja: \u274c background task error: {e}",
                )
            except Exception:
                pass
        finally:
            _WORKER_BUSY.clear()
            _HEAVY_Q.task_done()


def _start_heavy_worker() -> None:
    global _HEAVY_WORKER_STARTED
    if _HEAVY_WORKER_STARTED:
        return
    threading.Thread(target=_heavy_worker, name="heavy-worker", daemon=True).start()
    _HEAVY_WORKER_STARTED = True


# --- light-session pool -----------------------------------------------------
# N interchangeable light workers each pinned to a session id with its own queue.
# A shared queue + blocking .get() doesn't round-robin: the OS keeps re-waking
# ops0, so serial msgs all land on ops0 and ops1+ never run. Per-worker queues
# + explicit rotating picker (_enqueue_light) make back-to-back turns alternate.
_LIGHT_POOL_STARTED = False
_LIGHT_BUSY: "list" = []
_LIGHT_QS: "list" = []
_LIGHT_LOCK = threading.Lock()
_LIGHT_RR = 0


def _light_worker(
    session_id: str, busy: "threading.Event", label: str, q: "queue.Queue"
) -> None:
    """One light lane: serial per session (no -c clash), concurrent across the pool."""
    while True:
        job = q.get()
        busy.set()
        try:
            started = time.time()
            chat_jid = job.get("chat", "")
            same = _read_last_handler(chat_jid) == label
            texts = [m.get("text", "") for m in job["pending"] if m.get("text")]
            task_id = str(uuid.uuid4())
            combined = " | ".join(texts)
            conversation_id = get_thread_id()
            title = (
                (
                    generate_task_title(
                        combined, task_id=task_id, conversation_id=conversation_id
                    )
                    if combined
                    else None
                )
                or (combined[:50] + "…" if len(combined) > 50 else combined)
                or DEFAULT_TASK_TITLE
            )
            custom_headers = build_custom_headers(task_id, title, conversation_id)
            prompt = _build_prompt(
                job["pending"],
                chat_jid,
                current_tag=label,
                same_session_continuation=same,
            )
            ok, err, parsed = _dispatch_to_claude(
                prompt,
                task_id=task_id,
                session=session_id,
                tag=label,
                bound_chat_jid=chat_jid,
                extra_env={"ANTHROPIC_CUSTOM_HEADERS": custom_headers},
            )
            cost = parsed.total_cost_usd if parsed else 0.0
            model = primary_model(parsed) if parsed else ""
            threading.Thread(
                target=record_task_cost,
                args=(texts, title, cost),
                kwargs={
                    "model": model,
                    "task_id": task_id,
                    "conversation_id": conversation_id,
                },
                daemon=True,
            ).start()
            if ok:
                _set_last_handler(chat_jid, label)
                _bump_session_turn_count(session_id)
                _maybe_compact_session(session_id, label, chat_jid)
            logger.info(
                "%s turn done in %.1fs (qsize=%s)",
                label,
                time.time() - started,
                q.qsize(),
            )
            _handle_dispatch_result(
                ok,
                err,
                job["acked"],
                job["base"],
                job["token"],
                job["chat"],
                job["done_emoji"],
            )
        except Exception as e:  # never let a job kill the worker
            try:
                _gateway_send(
                    job["base"],
                    job["token"],
                    job["chat"],
                    f"\U0001f977 Ninja: ❌ light task error: {e}",
                )
            except Exception:
                pass
        finally:
            busy.clear()
            q.task_done()


def _enqueue_light(job: dict) -> tuple:
    """Assign light turn to a worker; return (label, all_busy).

    Prefer an idle worker (no current turn AND empty queue), chosen in RR order
    from _LIGHT_RR so consecutive turns alternate sessions. If none idle, append
    to least-loaded queue and report all_busy=True. Lock makes the just-queued
    job count as non-idle on next pick, closing the race before busy flag is set.
    """
    global _LIGHT_RR
    with _LIGHT_LOCK:
        n = len(_LIGHT_QS)
        idle = [
            i for i in range(n) if not _LIGHT_BUSY[i].is_set() and _LIGHT_QS[i].empty()
        ]
        if idle:
            start = _LIGHT_RR % n
            chosen = next(
                ((start + off) % n for off in range(n) if (start + off) % n in idle),
                idle[0],
            )
            _LIGHT_RR = chosen + 1
            all_busy = False
        else:
            chosen = min(range(n), key=lambda i: _LIGHT_QS[i].qsize())
            all_busy = True
        _LIGHT_QS[chosen].put(job)
        return f"ops{chosen}", all_busy


def _start_light_pool() -> None:
    global _LIGHT_POOL_STARTED
    if _LIGHT_POOL_STARTED:
        return
    ids = _ops_session_ids(LIGHT_POOL_SIZE)
    for i, sid in enumerate(ids):
        ev = threading.Event()
        q: "queue.Queue" = queue.Queue()
        _LIGHT_BUSY.append(ev)
        _LIGHT_QS.append(q)
        threading.Thread(
            target=_light_worker,
            args=(sid, ev, f"ops{i}", q),
            name=f"light-worker-{i}",
            daemon=True,
        ).start()
    _LIGHT_POOL_STARTED = True
    logger.info("light pool started (%s sessions, round-robin)", len(ids))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ninja Monitor — WhatsApp transport (MESSAGING_CHANNEL=whatsapp)"
    )
    # Default poll interval: CLI flag > WHATSAPP_POLL_INTERVAL env > module default.
    env_interval_raw = os.environ.get("WHATSAPP_POLL_INTERVAL")
    try:
        env_interval = int(env_interval_raw) if env_interval_raw else None
    except ValueError:
        env_interval = None
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=env_interval if env_interval and env_interval > 0 else POLL_INTERVAL,
        help=(
            f"Poll interval in seconds (default {POLL_INTERVAL}; "
            "override via $WHATSAPP_POLL_INTERVAL or --interval)"
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="Max messages per poll (default 50)"
    )
    parser.add_argument(
        "--include-from-me",
        action="store_true",
        help="Also dispatch messages flagged from_me (local-test only — in production"
        " Phantom must not reply to its own outbound)",
    )
    args = parser.parse_args(argv)

    base = _gateway_url()
    token = _gateway_token()
    last_seq, last_epoch = _load_cursor()

    logger.info(
        "WhatsApp Ninja Monitor started "
        "(gateway=%s poll=%ss jitter=%ss cursor_seq=%s cursor_epoch=%s)",
        base,
        args.interval,
        POLL_JITTER,
        last_seq,
        last_epoch,
    )

    start_time = time.time()
    last_idle_log = 0.0
    welcome_attempted_jid: Optional[str] = None
    # Cold start (no persisted cursor): skip whatever Baileys backfilled into the
    # gateway inbox — operator doesn't want replies to pre-monitor messages.
    cold_start_skip_done = last_seq != 0

    try:
        while True:
            if time.time() - start_time >= MAX_RUNTIME:
                logger.info("Max runtime reached. Exiting.")
                return 0

            st = _get_status(base, token)
            if not st:
                if time.time() - last_idle_log > IDLE_LOG_EVERY:
                    logger.info("gateway not reachable at %s; retrying", base)
                    last_idle_log = time.time()
                time.sleep(args.interval)
                continue

            bound_chat_jid = st.get("bound_chat_jid")
            bound_via = st.get("bound_via")
            ninja_state = st.get("ninja_state") or st.get("connection")
            if not bound_chat_jid:
                if time.time() - last_idle_log > IDLE_LOG_EVERY:
                    logger.info(
                        "not bound yet (ninja_state=%s); "
                        "show dashboard QR + pairing code to operator",
                        ninja_state,
                    )
                    last_idle_log = time.time()
                time.sleep(args.interval)
                continue

            if bound_chat_jid != welcome_attempted_jid:
                welcome_attempted_jid = bound_chat_jid
                if not is_feature_enabled(WHATSAPP_SHARED_MONITOR_FLAG, default=False):
                    try:
                        from agents_config import AGENTS
                        from core.config import load_agent_config
                        from messaging.whatsapp.interface import WhatsAppInterface
                        from services.monitor_service import (
                            build_welcome_message,
                            build_welcome_signature,
                        )

                        cfg = load_agent_config()
                        agent_id = cfg.get("default_agent", "ninja")
                        agent = {
                            **AGENTS.get(agent_id, AGENTS["ninja"]),
                            "id": agent_id,
                        }
                        posted = WhatsAppInterface().post_welcome_if_needed(
                            agent,
                            build_welcome_message(agent),
                            build_welcome_signature(agent),
                        )
                        if posted:
                            logger.info("Posted standard welcome to bound chat")
                    except Exception as e:
                        logger.warning("Welcome skipped: %s", e)

            # Cursor invalidation on epoch change (gateway restart / relink).
            server_epoch_raw = st.get("inbox_epoch")
            server_epoch = (
                int(server_epoch_raw)
                if isinstance(server_epoch_raw, (int, float)) and server_epoch_raw > 0
                else None
            )
            if (
                last_epoch is not None
                and server_epoch is not None
                and server_epoch != last_epoch
            ):
                logger.info(
                    "inbox_epoch changed (%s -> %s); "
                    "resetting cursor and re-arming cold-start skip",
                    last_epoch,
                    server_epoch,
                )
                last_seq = 0
                last_epoch = server_epoch
                cold_start_skip_done = False

            payload = _get_messages(base, token, last_seq, args.limit)
            if payload is None:
                time.sleep(args.interval)
                continue

            raw_items = payload.get("items") or []
            latest_seq = int(payload.get("latest_seq") or last_seq)

            # First bound poll with no persisted cursor: jump to latest_seq so
            # we don't dispatch the entire Baileys-backfilled history.
            if not cold_start_skip_done:
                cold_start_skip_done = True
                if should_cold_start_skip_backlog(
                    cold_start_done=False, latest_seq=latest_seq, last_seq=last_seq
                ):
                    skipped = len(raw_items)
                    last_seq = latest_seq
                    response_epoch_for_save = payload.get("inbox_epoch")
                    if (
                        isinstance(response_epoch_for_save, (int, float))
                        and response_epoch_for_save > 0
                    ):
                        last_epoch = int(response_epoch_for_save)
                    _save_cursor(last_seq, last_epoch)
                    logger.info(
                        "cold start — skipping %s backlog message(s); "
                        "cursor jumped to seq=%s. Send a new message to test.",
                        skipped,
                        last_seq,
                    )
                time.sleep(args.interval)
                continue
            response_epoch_raw = payload.get("inbox_epoch")
            response_epoch = (
                int(response_epoch_raw)
                if isinstance(response_epoch_raw, (int, float))
                and response_epoch_raw > 0
                else None
            )

            include_from_me = resolve_include_from_me(
                bound_via=bound_via, cli_include=args.include_from_me
            )
            pending = filter_inbox_for_dispatch(
                raw_items,
                include_from_me=include_from_me,
                bound_via=bound_via,
            )

            if pending and is_feature_enabled(
                WHATSAPP_SHARED_MONITOR_FLAG, default=False
            ):
                logger.info(
                    "shadow[legacy]: would_dispatch batch=%s",
                    [m.get("message_key") for m in pending],
                )
                last_seq = latest_seq
                if response_epoch is not None:
                    last_epoch = response_epoch
                _save_cursor(last_seq, last_epoch)
                time.sleep(args.interval + random.uniform(0, POLL_JITTER))
                continue

            if pending:
                # Surface inbound→dispatch latency so we can spot whether
                # the wait was poll-cadence (gateway→monitor) or model-bound
                # (Claude CLI). Uses the oldest pending msg's ts as anchor.
                oldest_ts_ms = min((p.get("timestamp") or 0) for p in pending) or 0
                pending_age_s = (
                    (time.time() * 1000 - oldest_ts_ms) / 1000.0
                    if oldest_ts_ms
                    else 0.0
                )
                dispatch_started = time.time()
                logger.info(
                    "dispatching %s WhatsApp message(s) to Claude "
                    "(inbound->dispatch=%.1fs)",
                    len(pending),
                    pending_age_s,
                )

                # Mirror Slack's ghost-ack: 👀 every inbound before dispatch,
                # swap to 🥷 when claude-wrapper exits cleanly. Best-effort —
                # gateway/network errors are swallowed and never block dispatch.
                ack_emoji = os.environ.get("PHANTOM_AGENT_EMOJI", "👀").strip()
                done_emoji = resolve_agent_done_emoji()
                acked: list[dict[str, Any]] = []
                if ack_emoji:
                    for msg in pending:
                        key = msg.get("message_key")
                        if not key:
                            continue
                        if _gateway_react(
                            base,
                            token,
                            key,
                            ack_emoji,
                            from_me=bool(msg.get("from_me")),
                            participant=msg.get("participant"),
                        ):
                            acked.append(
                                {
                                    "message_key": key,
                                    "from_me": bool(msg.get("from_me")),
                                    "participant": msg.get("participant"),
                                }
                            )
                    logger.info("%s acked %s message(s)", ack_emoji, len(acked))

                # MUST stay in main loop — moving to workers would race insertion order across the pool.
                _record_pending(pending)
                heavy_turn = _is_heavy(pending)
                if TRUE_2SESSION:
                    # Two roles, two Claude sessions, concurrent:
                    #  - heavy turn -> background worker on the PR session
                    #  - light turn -> foreground on the OPS session, which runs
                    #    AT THE SAME TIME as any in-flight PR build (distinct
                    #    session ids, no `-c` collision). So a light question is
                    #    answered even while a heavy build is still running.
                    _start_heavy_worker()
                    if heavy_turn:
                        _gateway_send(
                            base,
                            token,
                            bound_chat_jid,
                            "\U0001f977 Ninja: \u23f3 Heavy task \u2014 running it in the "
                            "background; I'll keep answering you meanwhile and report "
                            "when it's done.",
                        )
                        _HEAVY_Q.put(
                            {
                                "pending": pending,
                                "acked": acked,
                                "base": base,
                                "token": token,
                                "chat": bound_chat_jid,
                                "done_emoji": done_emoji,
                                "session": PR_SESSION_ID,
                                "tag": "pr",
                            }
                        )
                        logger.info(
                            "queued heavy turn (pr session, qsize=%s)",
                            _HEAVY_Q.qsize(),
                        )
                    elif LIGHT_POOL:
                        # Idle-pick: hand the light turn to the pool and keep
                        # polling. Whichever worker session is free answers it,
                        # concurrently with any pr build AND with other light
                        # turns. Main loop never blocks here.
                        _start_light_pool()
                        label, all_busy = _enqueue_light(
                            {
                                "pending": pending,
                                "acked": acked,
                                "base": base,
                                "token": token,
                                "chat": bound_chat_jid,
                                "done_emoji": done_emoji,
                            }
                        )
                        if all_busy:
                            _gateway_send(
                                base,
                                token,
                                bound_chat_jid,
                                "\U0001f977 Ninja: ⏳ All sessions busy — your "
                                "message is queued, answering shortly.",
                            )
                        free = sum(1 for e in _LIGHT_BUSY if not e.is_set())
                        logger.info(
                            "queued light turn -> %s (free=%s/%s all_busy=%s)",
                            label,
                            free,
                            len(_LIGHT_BUSY),
                            all_busy,
                        )
                    else:
                        # Rollback path (WHATSAPP_LIGHT_POOL=0): single ops
                        # session, foreground (blocks the loop = old behaviour).
                        claude_started = time.time()
                        same = _read_last_handler(bound_chat_jid) == "ops"
                        texts = [m.get("text", "") for m in pending if m.get("text")]
                        task_id = str(uuid.uuid4())
                        combined = " | ".join(texts)
                        conversation_id = get_thread_id()
                        title = (
                            (
                                generate_task_title(
                                    combined,
                                    task_id=task_id,
                                    conversation_id=conversation_id,
                                )
                                if combined
                                else None
                            )
                            or (combined[:50] + "…" if len(combined) > 50 else combined)
                            or DEFAULT_TASK_TITLE
                        )
                        custom_headers = build_custom_headers(
                            task_id, title, conversation_id
                        )
                        prompt = _build_prompt(
                            pending,
                            bound_chat_jid,
                            current_tag="ops",
                            same_session_continuation=same,
                        )
                        ok, err_msg, parsed = _dispatch_to_claude(
                            prompt,
                            task_id=task_id,
                            session=OPS_SESSION_ID,
                            tag="ops",
                            bound_chat_jid=bound_chat_jid,
                            extra_env={"ANTHROPIC_CUSTOM_HEADERS": custom_headers},
                        )
                        cost = parsed.total_cost_usd if parsed else 0.0
                        model = primary_model(parsed) if parsed else ""
                        threading.Thread(
                            target=record_task_cost,
                            args=(texts, title, cost),
                            kwargs={
                                "model": model,
                                "task_id": task_id,
                                "conversation_id": conversation_id,
                            },
                            daemon=True,
                        ).start()
                        if ok:
                            _set_last_handler(bound_chat_jid, "ops")
                            _bump_session_turn_count(OPS_SESSION_ID)
                            _maybe_compact_session(
                                OPS_SESSION_ID, "ops", bound_chat_jid
                            )
                        logger.info(
                            "ops turn %.1fs (concurrent with pr=%s)",
                            time.time() - claude_started,
                            _WORKER_BUSY.is_set(),
                        )
                        _handle_dispatch_result(
                            ok, err_msg, acked, base, token, bound_chat_jid, done_emoji
                        )
                elif ASYNC_HEAVY:
                    # Non-blocking serial dispatch: hand every turn to the
                    # background worker so the main loop stays free to poll and
                    # instantly ack. The worker runs ONE turn at a time — never
                    # two concurrent `claude -c` (that would corrupt the resumed
                    # session). So nothing blocks the loop, nothing times out,
                    # nothing is lost; turns are just answered in order.
                    _start_heavy_worker()
                    busy = _WORKER_BUSY.is_set() or not _HEAVY_Q.empty()
                    if _is_heavy(pending):
                        _gateway_send(
                            base,
                            token,
                            bound_chat_jid,
                            "🥷 Ninja: ⏳ Heavy task — running it in the background; "
                            "I'll report when it's done.",
                        )
                    elif busy:
                        _gateway_send(
                            base,
                            token,
                            bound_chat_jid,
                            "🥷 Ninja: ⏳ I'm mid-task — your message is queued, "
                            "I'll answer it right after.",
                        )
                    _HEAVY_Q.put(
                        {
                            "pending": pending,
                            "acked": acked,
                            "base": base,
                            "token": token,
                            "chat": bound_chat_jid,
                            "done_emoji": done_emoji,
                        }
                    )
                    logger.info(
                        "queued turn to dispatch worker (busy=%s qsize=%s)",
                        busy,
                        _HEAVY_Q.qsize(),
                    )
                else:
                    # Synchronous fallback (WHATSAPP_ASYNC_HEAVY=0): old behaviour.
                    claude_started = time.time()
                    texts = [m.get("text", "") for m in pending if m.get("text")]
                    task_id = str(uuid.uuid4())
                    combined = " | ".join(texts)
                    conversation_id = get_thread_id()
                    title = (
                        (
                            generate_task_title(
                                combined,
                                task_id=task_id,
                                conversation_id=conversation_id,
                            )
                            if combined
                            else None
                        )
                        or (combined[:50] + "…" if len(combined) > 50 else combined)
                        or DEFAULT_TASK_TITLE
                    )
                    custom_headers = build_custom_headers(
                        task_id, title, conversation_id
                    )
                    prompt = _build_prompt(pending, bound_chat_jid)
                    ok, err_msg, parsed = _dispatch_to_claude(
                        prompt,
                        task_id=task_id,
                        bound_chat_jid=bound_chat_jid,
                        extra_env={"ANTHROPIC_CUSTOM_HEADERS": custom_headers},
                    )
                    cost = parsed.total_cost_usd if parsed else 0.0
                    model = primary_model(parsed) if parsed else ""
                    threading.Thread(
                        target=record_task_cost,
                        args=(texts, title, cost),
                        kwargs={
                            "model": model,
                            "task_id": task_id,
                            "conversation_id": conversation_id,
                        },
                        daemon=True,
                    ).start()
                    claude_elapsed = time.time() - claude_started
                    total_elapsed = time.time() - dispatch_started
                    logger.info(
                        "claude=%.1fs total=%.1fs (inbound->reply~%.1fs)",
                        claude_elapsed,
                        total_elapsed,
                        pending_age_s + total_elapsed,
                    )
                    _handle_dispatch_result(
                        ok, err_msg, acked, base, token, bound_chat_jid, done_emoji
                    )
                last_idle_log = 0.0  # reset idle log timing after activity

            # Always advance cursor up to the gateway's latest, regardless
            # of whether we had pending items (e.g. fromMe traffic).
            if raw_items and latest_seq > last_seq:
                last_seq = latest_seq
                if response_epoch is not None:
                    last_epoch = response_epoch
                _save_cursor(last_seq, last_epoch)

            time.sleep(args.interval + random.uniform(0, POLL_JITTER))

    except KeyboardInterrupt:
        logger.info("Monitor stopped")
        _save_cursor(last_seq, last_epoch)
        return 0


if __name__ == "__main__":
    sys.exit(main())
