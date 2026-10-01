"""Ninja WhatsApp CLI and MessagingInterface adapter.

Run from ``src/ninja`` with PYTHONPATH set:

    python messaging/whatsapp/interface.py onboard --gateway-url ... --to 15551234567
    python messaging/whatsapp/interface.py say "hello"
    python messaging/whatsapp/interface.py read --limit 20

Talks only to the local gateway. Bearer token from --gateway-token,
$WHATSAPP_GATEWAY_TOKEN, or whatsapp sub-object in ~/.agent_settings.json.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from clients.posthog_client import is_feature_enabled
from constants import (
    LIVE_PROGRESS_MAX_ACTIONS,
    MONITOR_MINUTES_THRESHOLD,
    WHATSAPP_SHARED_MONITOR_FLAG,
    WHATSAPP_SQLITE_CACHE_FLAG,
)
from core import live_progress
from core.config import load_agent_messages, save_agent_messages
from core.logging import get_logger
from core.metadata import load_sandbox_metadata
from messaging.base import MessagingInterface
from messaging.cache_identity import Member
from messaging.message_utils import convert_sandbox_urls
from messaging.monitoring import MessageStateBatch, clear_live_progress
from messaging.pending import PendingMessage
from messaging.whatsapp._config import gateway_token as _gwtok
from messaging.whatsapp._config import gateway_url as _gwurl
from messaging.whatsapp.routing import resolve_whatsapp_to
from services.whatsapp.recent_context import clear_recent_messages, record_message

logger = get_logger("whatsapp")

SETTINGS_PATH = Path.home() / ".agent_settings.json"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8090"
WELCOME_HISTORY_LIMIT = 50


def markdown_to_whatsapp(text: str) -> str:
    """Convert Slack-style Markdown to WhatsApp client formatting."""
    links: list[str] = []
    bolds: list[str] = []

    def _link(match: re.Match[str]) -> str:
        label = re.sub(r"\*\*(.+?)\*\*", r"*\1*", match.group(1))
        target = match.group(2)
        # WhatsApp has no link syntax, so a link becomes "label\ntarget" and the
        # client auto-links the target. When the target only restates the label
        # — [support@x.ai](mailto:support@x.ai) — that prints the address twice,
        # so keep the label alone and let the client linkify it.
        if target == label or target == f"mailto:{label}":
            links.append(label)
        else:
            links.append(f"{label}\n{target}")
        return f"\x00L{len(links) - 1}\x00"

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, text)

    def _bold(match: re.Match[str]) -> str:
        bolds.append(match.group(1))
        return f"\x00B{len(bolds) - 1}\x00"

    text = re.sub(r"\*\*(.+?)\*\*", _bold, text)
    text = re.sub(r"\*(.+?)\*", r"_\1_", text)
    text = re.sub(r"__(.+?)__", r"_\1_", text)
    for index, content in enumerate(bolds):
        text = text.replace(f"\x00B{index}\x00", f"*{content}*")
    for index, link in enumerate(links):
        text = text.replace(f"\x00L{index}\x00", link)
    text = re.sub(
        r"^\*(💼|🧰|💬|👁)([^*\n]+)\*\n(- )",
        r"*\1\2*\n\n\3",
        text,
        flags=re.MULTILINE,
    )
    return text.strip()


# ---------- settings ----------


def _read_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return {}


def _write_whatsapp_settings(whatsapp: dict[str, Any]) -> None:
    """Merge the `whatsapp` sub-object only; never touch top-level keys."""
    settings = _read_settings()
    existing = (
        settings.get("whatsapp") if isinstance(settings.get("whatsapp"), dict) else {}
    )
    merged = {**existing, **whatsapp}
    settings["whatsapp"] = merged
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def _settings_whatsapp() -> dict[str, Any]:
    s = _read_settings().get("whatsapp")
    return s if isinstance(s, dict) else {}


def _stored_read_seq() -> int:
    """Persisted inbox cursor (unified or legacy monitor)."""
    wa = _settings_whatsapp()
    raw = wa.get("monitor_last_read_seq", wa.get("last_read_seq"))
    if isinstance(raw, (int, float)) and raw >= 0:
        return int(raw)
    return 0


# ---------- gateway client ----------


def _gateway_url(args: argparse.Namespace) -> str:
    return _gwurl(getattr(args, "gateway_url", None))


def _gateway_token(args: argparse.Namespace) -> Optional[str]:
    return _gwtok(getattr(args, "gateway_token", None))


def _request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 15.0,
) -> tuple[int, bytes, dict[str, str]]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except urllib.error.URLError as e:
        # status=0 → "not reachable" (refused/DNS/timeout)
        return 0, f"connection failed: {e.reason}".encode("utf-8"), {}


def _header_get(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup."""
    want = name.lower()
    for key, value in headers.items():
        if key.lower() == want:
            return value
    return ""


def _json_or_die(status: int, raw: bytes) -> dict[str, Any]:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        sys.stderr.write(
            f"gateway returned non-JSON (status {status}): {raw[:200]!r}\n"
        )
        sys.exit(2)


def _normalize_conversation_id(raw: str) -> str:
    """Strip non-digits per colon-segment for exact channel_id match; preserve 'g' marker."""
    parts = (raw or "").split(":")
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p == "g":
            out.append("g")
        else:
            digits = "".join(c for c in p if c.isdigit())
            out.append(digits)
    return ":".join(out)


def _raw_chat_jid(conversation_id: str) -> Optional[str]:
    """JID the gateway stores for messages delivered before it knows its own
    number (offline backlog at reconnect). Not derivable for ``@lid`` DMs."""
    parts = (conversation_id or "").split(":")
    if len(parts) == 3 and parts[1] == "g" and parts[2]:
        return f"{parts[2]}@g.us"
    if len(parts) == 2 and parts[1]:
        return f"{parts[1]}@s.whatsapp.net"
    return None


def _in_conversation(
    item: dict[str, Any], conversation_id: str
) -> Optional[dict[str, Any]]:
    """``item`` if it belongs to the conversation, else None. Raw-JID rows
    come back with the canonical ``channel_id``."""
    row = str(item.get("channel_id") or "")
    if not row:
        return None
    if _normalize_conversation_id(row) == conversation_id:
        return item
    if row == _raw_chat_jid(conversation_id):
        return {**item, "channel_id": conversation_id}
    return None


def _filter_conversation(
    items: List[dict[str, Any]], conversation_id: str
) -> List[dict[str, Any]]:
    out = []
    for item in items:
        matched = _in_conversation(item, conversation_id)
        if matched is not None:
            out.append(matched)
    return out


def _jid_to_conversation_id(self_e164: str, chat_jid: str) -> str:
    """Map a bound WhatsApp JID to the gateway conversation_id shape."""
    if chat_jid.endswith("@g.us"):
        local = chat_jid[: -len("@g.us")]
        return f"{self_e164}:g:{local}"
    head = chat_jid.split("@", 1)[0]
    peer_digits = "".join(c for c in head.split(":")[0] if c.isdigit())
    return f"{self_e164}:{peer_digits}"


def _resolve_whatsapp_unique_channel() -> str:
    """Derive x-ninja-integration-channel-id for Pipedream Connect.

    Format: ``whatsapp-{self_e164}.{conversation_id}`` once linked and bound.
    Before bind, falls back to ``whatsapp-thread.{thread_id}`` so the
    integrations dashboard (port 9020) can mint a Connect link during setup.
    """
    wa_settings = _settings_whatsapp()
    self_e164 = (wa_settings.get("self_e164") or "").strip() or None
    conversation_id = (wa_settings.get("conversation_id") or "").strip() or None

    st = _gateway_status(_gwurl(), _gwtok())
    if st:
        self_e164 = (st.get("self_e164") or self_e164 or "").strip() or None
        bound_chat_jid = (st.get("bound_chat_jid") or "").strip()
        if self_e164 and bound_chat_jid:
            conversation_id = _jid_to_conversation_id(self_e164, bound_chat_jid)

    if self_e164 and conversation_id:
        return f"whatsapp-{self_e164}.{conversation_id}"

    thread_id = (load_sandbox_metadata().get("thread_id") or "").strip()
    if thread_id:
        return f"whatsapp-thread.{thread_id}"

    raise ValueError(
        "Cannot derive unique_channel: WhatsApp is not linked/bound and "
        "no thread_id is available in sandbox metadata."
    )


def _conversation_id_to_group_jid(conversation_id: str) -> Optional[str]:
    """Parse `{self}:g:{local}` → `{local}@g.us`."""
    raw = (conversation_id or "").strip()
    parts = raw.split(":")
    if len(parts) != 3 or parts[1].strip() != "g":
        return None
    local = parts[2].strip()
    if not local:
        return None
    # Group local ids are numeric; reject doc placeholders (120363XXXXXXXXX).
    if any(c.upper() == "X" for c in local) or not local.isdigit():
        return None
    if len(local) < 12:
        return None
    return f"{local}@g.us"


def resolve_group_target(args: argparse.Namespace) -> Optional[str]:
    """Resolve an explicit group JID from say flags, or None for DM send."""
    group_jid = (getattr(args, "group_jid", None) or "").strip()
    group = (getattr(args, "group", None) or "").strip()
    conversation = (getattr(args, "conversation", None) or "").strip()
    flags = [x for x in [group_jid, group, conversation] if x]
    if len(flags) > 1:
        raise ValueError("use only one of --group-jid, --group, or --conversation")
    if group_jid:
        if group_jid.endswith("@g.us"):
            return group_jid
        local = "".join(c for c in group_jid if c.isdigit()) or group_jid
        return f"{local}@g.us"
    if group:
        if group.lower() == "last":
            last = _settings_whatsapp().get("last_group_jid")
            if not last or not isinstance(last, str):
                raise ValueError("no last_group_jid in settings; create a group first")
            return last.strip()
        if group.endswith("@g.us"):
            return group
        local = "".join(c for c in group if c.isdigit()) or group
        return f"{local}@g.us"
    if conversation:
        jid = _conversation_id_to_group_jid(conversation)
        if not jid:
            raise ValueError(
                "conversation is not a valid group conversation_id "
                "(expected {self}:g:{numeric_group_id}; use `say --group last` or jq .whatsapp.last_group_conversation_id)"
            )
        return jid
    return None


def _iso_utc(ts_ms: Any) -> str:
    """Format a millisecond epoch as ISO-8601 UTC (e.g. 2026-06-04T17:42:13Z)."""
    try:
        n = int(ts_ms)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "?"
    return datetime.fromtimestamp(n / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------- commands ----------


def cmd_onboard(args: argparse.Namespace) -> int:
    base = _gateway_url(args)
    token = _gateway_token(args)

    # 1. Preflight /health
    status, raw, _ = _request("GET", f"{base}/health")
    if status != 200:
        sys.stderr.write(f"health check failed: status={status} body={raw[:200]!r}\n")
        return 2
    print(f"gateway reachable at {base}")

    # 2. Poll /qr and /status until connection=open
    qr_path = Path(args.qr_out) if args.qr_out else Path.cwd() / "whatsapp-qr.png"
    last_state: Optional[str] = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        s_status, s_raw, _ = _request("GET", f"{base}/status", token=token)
        if s_status == 401:
            sys.stderr.write(
                "unauthorized — provide --gateway-token or set WHATSAPP_GATEWAY_TOKEN\n"
            )
            return 2
        if s_status != 200:
            sys.stderr.write(f"status failed: {s_status} {s_raw[:200]!r}\n")
            return 2
        st = _json_or_die(s_status, s_raw)
        conn = st.get("connection")
        if conn != last_state:
            print(f"state: {conn}")
            last_state = conn
        if conn == "open":
            self_e164 = st.get("self_e164")
            print(f"linked as {self_e164}")
            _persist_onboard(args, base, token, self_e164)
            return 0
        if conn in ("qr", "connecting", "starting"):
            q_status, q_raw, q_hdrs = _request("GET", f"{base}/qr", token=token)
            ctype = _header_get(q_hdrs, "Content-Type")
            if q_status == 200 and ctype.startswith("image/png"):
                qr_path.write_bytes(q_raw)
                print(f"QR refreshed: {qr_path}")
            elif q_status == 404:
                pass  # QR not ready yet; keep polling
            elif q_status != 200:
                sys.stderr.write(
                    f"qr fetch failed: status={q_status} body={q_raw[:200]!r}\n"
                )
        time.sleep(args.poll_interval)

    sys.stderr.write("onboard timed out before connection=open\n")
    return 1


def _persist_onboard(
    args: argparse.Namespace,
    base: str,
    token: Optional[str],
    self_e164: Optional[str],
) -> None:
    to = (args.to or "").strip()
    digits_to = "".join(c for c in to if c.isdigit())
    conv: Optional[str] = None
    if self_e164 and digits_to:
        conv = f"{self_e164}:{digits_to}"
    payload: dict[str, Any] = {"gateway_url": base}
    if token:
        payload["gateway_token"] = token
    if digits_to:
        payload["default_to"] = digits_to
    if conv:
        payload["conversation_id"] = conv
    if self_e164:
        payload["self_e164"] = self_e164
    _write_whatsapp_settings(payload)
    print(f"wrote whatsapp settings to {SETTINGS_PATH}")


def cmd_say(args: argparse.Namespace) -> int:
    base = _gateway_url(args)
    token = _gateway_token(args)
    try:
        group_jid = resolve_group_target(args)
    except ValueError as e:
        sys.stderr.write(f"group target error: {e}\n")
        return 2
    if group_jid and getattr(args, "to", None):
        sys.stderr.write(
            "use either --to (DM) or a group flag (--group-jid, --group, --conversation), not both\n"
        )
        return 2
    # Prefix is applied here (not at the gateway) so manual ops sends stay literal.
    # WHATSAPP_SESSION_TAG is for monitor logs / recent_context only — not user text.
    message = args.message
    if getattr(args, "ninja_prefix", False):
        prefix = "🥷 Ninja:"
        if not message.startswith("🥷 Ninja"):
            message = f"{prefix} {message}"
    if group_jid:
        req_body: dict[str, Any] = {"group_jid": group_jid, "text": message}
    else:
        try:
            to = resolve_whatsapp_to(args.to)
        except ValueError as e:
            sys.stderr.write(f"routing error: {e}\n")
            return 2
        req_body = {"to": to, "text": message}
    status, raw, _ = _request(
        "POST",
        f"{base}/send",
        token=token,
        body=req_body,
    )
    if status == 0:
        sys.stderr.write(
            f"gateway not reachable at {base}; is `npm run dev` running?\n  {raw.decode('utf-8', 'replace')}\n"
        )
        return 2
    if status == 401:
        sys.stderr.write(
            "unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n"
        )
        return 2
    if status == 409:
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    body = _json_or_die(status, raw)
    if status != 200 or not body.get("ok"):
        detail = str(body.get("detail", ""))
        if (
            status == 500
            and "bad-request" in detail.lower()
            and req_body.get("group_jid")
        ):
            sys.stderr.write(
                f"send failed: status={status} body={body}\n"
                "hint: group JID may be invalid or you may not be in that group; "
                "use `group list`, `say --group last`, or jq .whatsapp.last_group_conversation_id\n"
            )
        else:
            sys.stderr.write(f"send failed: status={status} body={body}\n")
        return 1
    # Record bot replies in the shared rolling buffer so every pool session
    # sees what was answered. Best-effort.
    if getattr(args, "ninja_prefix", False):
        try:
            record_message(
                "ninja",
                args.message,
                tag=(os.environ.get("WHATSAPP_SESSION_TAG") or "").strip() or None,
            )
        except Exception:
            pass
    # Best-effort: never let cleanup fail a message we already delivered.
    try:
        clear_live_progress()
    except Exception as e:
        logger.warning(f"Live progress cleanup after say failed: {e}")
    print(json.dumps(body))
    return 0


def cmd_group_create(args: argparse.Namespace) -> int:
    base = _gateway_url(args)
    token = _gateway_token(args)
    participants = [p.strip() for p in args.participants if p.strip()]
    if not participants:
        sys.stderr.write("--participants must include at least one number\n")
        return 2
    body: dict[str, Any] = {"subject": args.subject, "participants": participants}
    if args.welcome:
        body["welcome"] = args.welcome
    status, raw, _ = _request("POST", f"{base}/groups/create", token=token, body=body)

    if status == 0:
        sys.stderr.write(
            f"gateway not reachable at {base}; is `npm run dev` running?\n  {raw.decode('utf-8', 'replace')}\n"
        )
        return 2
    if status == 401:
        sys.stderr.write(
            "unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n"
        )
        return 2

    parsed = _json_or_die(status, raw)

    if status == 403 and parsed.get("error") == "group_create_disabled":
        sys.stderr.write(
            "group create is disabled. The GATEWAY process must be restarted with\n"
            "  WHATSAPP_ALLOW_GROUP_CREATE=1\n"
            "(this flag is on the gateway, not the CLI/Python process).\n"
        )
        return 1
    if status == 409 and parsed.get("error") == "not_linked":
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    if status == 500 and "no valid participants" in str(parsed.get("detail", "")):
        sys.stderr.write(
            "all participants failed onWhatsApp validation; verify they are active WhatsApp numbers.\n"
        )
        return 1
    if status != 200 or not parsed.get("ok"):
        sys.stderr.write(f"group create failed: status={status} body={parsed}\n")
        return 1

    # Persist last_group_* for later `read --conversation` use.
    persist: dict[str, Any] = {}
    if parsed.get("group_jid"):
        persist["last_group_jid"] = parsed["group_jid"]
    if parsed.get("conversation_id"):
        persist["last_group_conversation_id"] = parsed["conversation_id"]
    if persist:
        _write_whatsapp_settings(persist)

    print(json.dumps(parsed))
    return 0


def cmd_group_list(args: argparse.Namespace) -> int:
    base = _gateway_url(args)
    token = _gateway_token(args)
    status, raw, _ = _request("GET", f"{base}/groups", token=token)

    if status == 0:
        sys.stderr.write(
            f"gateway not reachable at {base}; is `npm run dev` running?\n  {raw.decode('utf-8', 'replace')}\n"
        )
        return 2
    if status == 401:
        sys.stderr.write(
            "unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n"
        )
        return 2
    if status == 409:
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    if status != 200:
        sys.stderr.write(f"group list failed: status={status} body={raw[:200]!r}\n")
        return 1

    payload = _json_or_die(status, raw)
    if not payload.get("ok"):
        sys.stderr.write(f"group list failed: {payload}\n")
        return 1

    groups = payload.get("groups") or []
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    if not groups:
        print("no participating groups")
        return 0

    print(f"{len(groups)} group(s):\n")
    for g in groups:
        subject = g.get("subject") or "?"
        jid = g.get("group_jid") or "?"
        conv = g.get("conversation_id") or "?"
        count = g.get("participant_count", "?")
        print(f"  {subject}")
        print(f"    jid:  {jid}")
        print(f"    conv: {conv}")
        print(f"    members: {count}")
        print()
    return 0


def _read_messages_once(
    base: str, token: Optional[str], since: int, limit: int
) -> tuple[int, dict[str, Any], bytes]:
    url = f"{base}/messages?since={since}&limit={limit}"
    status, raw, _ = _request("GET", url, token=token)
    if status != 200:
        return status, {}, raw
    return status, _json_or_die(status, raw), raw


def _read_db_messages_once(
    base: str,
    token: Optional[str],
    limit: int,
    channel_id: Optional[str] = None,
) -> tuple[int, dict[str, Any], bytes]:
    """Newest ``limit`` items from the SQLite mirror. No cursor: ``seq``
    restarts per process; dedupe is on ``message_key``. 503 = no cache."""
    url = f"{base}/db/messages?limit={limit}"
    if channel_id:
        url += f"&channel_id={urllib.parse.quote(channel_id)}"
    status, raw, _ = _request("GET", url, token=token)
    if status != 200:
        return status, {}, raw
    return status, _json_or_die(status, raw), raw


def _gateway_status(base: str, token: Optional[str]) -> Optional[dict[str, Any]]:
    status, raw, _ = _request("GET", f"{base}/status", token=token)
    if status != 200:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, AttributeError):
        return None


def _send_to_bound_chat(
    base: str, token: Optional[str], chat_jid: str, text: str
) -> Optional[dict[str, Any]]:
    """The gateway's reply (it carries the message id), or None on failure."""
    if not chat_jid or not text:
        return None
    if chat_jid.endswith("@g.us"):
        body: dict[str, Any] = {"group_jid": chat_jid, "text": text}
    else:
        digits = chat_jid.split("@", 1)[0].split(":", 1)[0]
        if not digits.isdigit():
            return None
        body = {"to": digits, "text": text}
    status, raw, _ = _request("POST", f"{base}/send", token=token, body=body)
    return json.loads(raw) if status == 200 else None


def render_progress(labels: list[str], seconds: float) -> str:
    """The live progress message: Slack's layout, elapsed in whole minutes.

    Whole minutes, so the text (and with it an edit) only changes when a step
    lands or the bar moves a cell.
    """
    lines = ["\U0001f977 *Working on it\u2026*"]
    lines += [f"\u2705 {label}" for label in labels[-LIVE_PROGRESS_MAX_ACTIONS:]] or [
        "\u23f3 Getting started"
    ]
    filled = round(min(1.0, seconds / (MONITOR_MINUTES_THRESHOLD * 60)) * 10)
    bar = "\u2593" * filled + "\u2591" * (10 - filled)
    counted = f"{len(labels)} action{'s' if len(labels) != 1 else ''}"
    lines.append(f"`{bar}`  {counted} \u00b7 {int(seconds // 60)}m elapsed")
    return "\n".join(lines)


def delete_sent_message(message_id: str) -> bool:
    """Delete a message the gateway sent, for us and then for everyone."""
    status, _, _ = _request(
        "POST", f"{_gwurl()}/delete", token=_gwtok(), body={"id": message_id}
    )
    return status == 200


def _recent_gateway_texts(
    base: str, token: Optional[str], *, limit: int = WELCOME_HISTORY_LIMIT
) -> list[str]:
    """Text from up to ``limit`` most recent gateway inbox items."""
    status, payload, _ = _read_messages_once(base, token, since=0, limit=1)
    if status != 200:
        return []
    latest = int(payload.get("latest_seq") or 0)
    since = max(0, latest - limit)
    status, payload, _ = _read_messages_once(base, token, since=since, limit=limit)
    if status != 200:
        return []
    items = payload.get("items") or []
    return [str(m.get("text") or "") for m in items]


def cmd_bind(args: argparse.Namespace) -> int:
    """Bind/inspect single-chat binding. --status (or no flags) prints state;
    --chat-jid POSTs /bind to set the allowed chat."""
    base = _gateway_url(args)
    token = _gateway_token(args)

    chat_jid = (getattr(args, "chat_jid", None) or "").strip()
    show_status = getattr(args, "status", False) or not chat_jid

    if chat_jid:
        if not (chat_jid.endswith("@g.us") or chat_jid.endswith("@s.whatsapp.net")):
            sys.stderr.write("chat-jid must end with @g.us or @s.whatsapp.net\n")
            return 2
        status, raw, _ = _request(
            "POST", f"{base}/bind", token=token, body={"chat_jid": chat_jid}
        )
        if status == 0:
            sys.stderr.write(f"gateway not reachable at {base}\n")
            return 2
        if status == 401:
            sys.stderr.write(
                "unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n"
            )
            return 2
        body = _json_or_die(status, raw)
        if status != 200 or not body.get("ok"):
            sys.stderr.write(f"bind failed: status={status} body={body}\n")
            return 1
        print(json.dumps(body))

    if show_status:
        s_status, s_raw, _ = _request("GET", f"{base}/status", token=token)
        if s_status == 401:
            sys.stderr.write(
                "unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n"
            )
            return 2
        if s_status != 200:
            sys.stderr.write(f"status failed: {s_status} {s_raw[:200]!r}\n")
            return 1
        st = _json_or_die(s_status, s_raw)
        # Pairing-code fetch is best-effort.
        p_status, p_raw, _ = _request("GET", f"{base}/pairing_code", token=token)
        pairing = _json_or_die(p_status, p_raw) if p_status == 200 else {}
        if args.json:
            out = {
                "connection": st.get("connection"),
                "ninja_state": st.get("ninja_state"),
                "bound_chat_jid": st.get("bound_chat_jid"),
                "bound_via": st.get("bound_via"),
                "bound_at": st.get("bound_at"),
                "pairing_code": pairing.get("code"),
                "pairing_code_expires_at": pairing.get("expires_at"),
                "bind_method": st.get("bind_method"),
                "bind_method_source": st.get("bind_method_source"),
                "last_active_method": st.get("last_active_method"),
                "last_error": st.get("last_error"),
                "invite_code": st.get("invite_code"),
                "invite_code_error": st.get("invite_code_error"),
                "grace_remaining_ms": st.get("grace_remaining_ms"),
            }
            print(json.dumps(out, indent=2))
        else:
            print(f"connection:    {st.get('connection')}")
            print(f"ninja_state:   {st.get('ninja_state')}")
            print(f"bound_chat:    {st.get('bound_chat_jid') or '(none)'}")
            if st.get("bound_via"):
                print(f"bound_via:     {st.get('bound_via')}")
            if st.get("bind_method"):
                print(
                    f"bind_method:   {st.get('bind_method')} (source={st.get('bind_method_source')})"
                )
            if st.get("invite_code"):
                print(
                    f"invite_link:   https://chat.whatsapp.com/{st.get('invite_code')}"
                )
            elif st.get("invite_code_error"):
                print(f"invite_link:   (unavailable — {st.get('invite_code_error')})")
            if st.get("last_error"):
                print(f"last_error:    {st.get('last_error')}")
            if pairing.get("code"):
                print(f"pairing_code:  {pairing.get('code')}")
                print(f"  expires_at:  {_iso_utc(pairing.get('expires_at'))}")
            elif (
                not st.get("bound_chat_jid")
                and st.get("ninja_state") == "waiting_for_chat_pairing"
            ):
                print("pairing_code:  (none — gateway not yet open or already expired)")
    return 0


def react(
    base: str,
    token: Optional[str],
    message_key: str,
    emoji: str,
    *,
    from_me: bool = False,
    participant: Optional[str] = None,
    timeout: float = 10.0,
) -> bool:
    """POST /react. Best-effort: returns False on any error, never raises."""
    if not message_key or not emoji:
        return False
    body: dict[str, Any] = {
        "message_key": message_key,
        "emoji": emoji,
        "from_me": from_me,
    }
    if participant:
        body["participant"] = participant
    try:
        status, _raw, _ = _request(
            "POST", f"{base}/react", token=token, body=body, timeout=timeout
        )
        return status == 200
    except Exception:
        return False


def cmd_react(args: argparse.Namespace) -> int:
    base = _gateway_url(args)
    token = _gateway_token(args)
    ok = react(
        base,
        token,
        args.message_key,
        args.emoji,
        from_me=bool(args.from_me),
        participant=args.participant,
    )
    if not ok:
        sys.stderr.write(f"react failed for message_key={args.message_key}\n")
        return 1
    print(json.dumps({"ok": True}))
    return 0


def cmd_fetch_media(args: argparse.Namespace) -> int:
    """Download decrypted media bytes from GET /media/:id to a local file."""
    base = _gateway_url(args)
    token = _gateway_token(args)
    media_id = (args.media_id or "").strip()
    if not media_id:
        sys.stderr.write("--media-id required\n")
        return 2
    out_path = (
        Path(args.out).expanduser() if args.out else Path.cwd() / f"wa_{media_id}"
    )
    url = f"{base}/media/{media_id}"
    headers: dict[str, str] = {"Accept": "*/*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            status = resp.status
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        sys.stderr.write(
            f"fetch-media failed: status={e.code} body={e.read()[:200]!r}\n"
        )
        return 1
    except urllib.error.URLError as e:
        sys.stderr.write(f"gateway not reachable at {base}: {e.reason}\n")
        return 2
    if status != 200:
        sys.stderr.write(f"fetch-media failed: status={status}\n")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(body)
    try:
        out_path.chmod(0o600)
    except OSError:
        pass
    print(
        json.dumps(
            {"ok": True, "path": str(out_path), "bytes": len(body), "mimetype": ctype}
        )
    )
    return 0


def _multipart_encode(
    fields: list[tuple[str, str]],
    file_field: str,
    file_name: str,
    file_mime: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    """Hand-roll a multipart/form-data body. Avoids extra deps."""
    import secrets

    boundary = "----PhantomBoundary" + secrets.token_hex(8)
    crlf = b"\r\n"
    out = bytearray()
    for name, value in fields:
        out += f"--{boundary}".encode() + crlf
        out += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        out += value.encode("utf-8") + crlf
    out += f"--{boundary}".encode() + crlf
    out += (
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{file_name}"'
    ).encode() + crlf
    out += f"Content-Type: {file_mime}".encode() + crlf + crlf
    out += file_bytes + crlf
    out += f"--{boundary}--".encode() + crlf
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def cmd_upload(args: argparse.Namespace) -> int:
    """Send a local file as an image or document via POST /send_media."""
    base = _gateway_url(args)
    token = _gateway_token(args)

    kind = (args.kind or "").lower().strip()
    if kind not in ("image", "document"):
        sys.stderr.write("--kind must be 'image' or 'document'\n")
        return 2
    file_path = Path(args.file).expanduser()
    if not file_path.exists() or not file_path.is_file():
        sys.stderr.write(f"file not found: {file_path}\n")
        return 2
    file_bytes = file_path.read_bytes()

    # Mimetype: --mimetype > python's mimetypes guess > sensible default.
    mimetype = (args.mimetype or "").strip()
    if not mimetype:
        guess, _ = mimetypes.guess_type(str(file_path))
        mimetype = guess or (
            "image/jpeg" if kind == "image" else "application/octet-stream"
        )

    # --ninja-prefix: synthesize a notice if no caption supplied; else prepend prefix.
    caption = args.caption if args.caption is not None else ""
    if args.ninja_prefix:
        if not caption:
            caption = f"🥷 Ninja: shared {'an' if kind == 'image' else 'a'} {kind}"
        elif not caption.startswith("🥷 Ninja:"):
            caption = f"🥷 Ninja: {caption}"

    # Route resolution mirrors `cmd_say`.
    try:
        group_jid = resolve_group_target(args)
    except ValueError as e:
        sys.stderr.write(f"group target error: {e}\n")
        return 2
    if group_jid and getattr(args, "to", None):
        sys.stderr.write(
            "use either --to (DM) or a group flag (--group-jid, --group, --conversation), not both\n"
        )
        return 2

    fields: list[tuple[str, str]] = [("kind", kind), ("mimetype", mimetype)]
    if caption:
        fields.append(("caption", caption))
    # filename: explicit > basename. Required server-side for documents.
    filename = (args.filename or file_path.name).strip()
    fields.append(("filename", filename))
    if group_jid:
        fields.append(("group_jid", group_jid))
    else:
        try:
            to = resolve_whatsapp_to(args.to)
        except ValueError as e:
            sys.stderr.write(f"routing error: {e}\n")
            return 2
        fields.append(("to", to))

    body, ctype = _multipart_encode(fields, "file", filename, mimetype, file_bytes)
    headers = {"Content-Type": ctype, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base}/send_media", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
    except urllib.error.URLError as e:
        sys.stderr.write(f"gateway not reachable at {base}: {e.reason}\n")
        return 2
    if status == 401:
        sys.stderr.write(
            "unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n"
        )
        return 2
    if status == 409:
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    parsed = _json_or_die(status, raw)
    if status != 200 or not parsed.get("ok"):
        sys.stderr.write(f"upload failed: status={status} body={parsed}\n")
        return 1
    try:
        clear_live_progress()
    except Exception as e:
        logger.warning(f"Live progress cleanup after upload failed: {e}")
    print(json.dumps(parsed))
    return 0


def cmd_unbind(args: argparse.Namespace) -> int:
    base = _gateway_url(args)
    token = _gateway_token(args)
    status, raw, _ = _request("POST", f"{base}/unbind", token=token)
    if status == 0:
        sys.stderr.write(f"gateway not reachable at {base}\n")
        return 2
    if status == 401:
        sys.stderr.write(
            "unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n"
        )
        return 2
    body = _json_or_die(status, raw)
    if status != 200 or not body.get("ok"):
        sys.stderr.write(f"unbind failed: status={status} body={body}\n")
        return 1
    print(json.dumps(body))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    base = _gateway_url(args)
    token = _gateway_token(args)

    settings = _settings_whatsapp()
    stored_seq_raw = settings.get("last_read_seq")
    stored_seq = (
        int(stored_seq_raw)
        if isinstance(stored_seq_raw, (int, float)) and stored_seq_raw >= 0
        else 0
    )
    stored_epoch_raw = settings.get("last_read_inbox_epoch")
    stored_epoch: Optional[int] = (
        int(stored_epoch_raw)
        if isinstance(stored_epoch_raw, (int, float)) and stored_epoch_raw > 0
        else None
    )

    since_override = args.since is not None
    if since_override:
        since = max(0, int(args.since))
    else:
        since = stored_seq

    limit = max(1, min(500, int(args.limit)))
    status, payload, raw = _read_messages_once(base, token, since, limit)

    if status == 0:
        sys.stderr.write(
            f"gateway not reachable at {base}; is `npm run dev` running?\n  {raw.decode('utf-8', 'replace')}\n"
        )
        return 2
    if status == 401:
        sys.stderr.write(
            "unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n"
        )
        return 2
    if status == 409:
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    if status != 200:
        sys.stderr.write(f"read failed: status={status} body={raw[:200]!r}\n")
        return 1

    response_epoch_raw = payload.get("inbox_epoch")
    response_epoch: Optional[int] = (
        int(response_epoch_raw)
        if isinstance(response_epoch_raw, (int, float)) and response_epoch_raw > 0
        else None
    )
    latest_seq_raw = payload.get("latest_seq")
    try:
        latest_seq_int = int(latest_seq_raw)
    except (TypeError, ValueError):
        latest_seq_int = since

    # Cursor invalidation: epoch mismatch (primary) or seq overrun (fallback);
    # only when caller didn't pass --since explicitly.
    needs_reset = False
    if not since_override and since > 0:
        if (
            response_epoch is not None
            and stored_epoch is not None
            and response_epoch != stored_epoch
        ):
            needs_reset = True
        elif stored_seq > latest_seq_int:
            needs_reset = True
    if needs_reset:
        print("cursor reset (gateway inbox restarted)")
        since = 0
        status, payload, raw = _read_messages_once(base, token, since, limit)
        if status != 200:
            sys.stderr.write(
                f"read failed after reset: status={status} body={raw[:200]!r}\n"
            )
            return 1
        latest_seq_raw = payload.get("latest_seq")
        try:
            latest_seq_int = int(latest_seq_raw)
        except (TypeError, ValueError):
            latest_seq_int = 0
        response_epoch_raw = payload.get("inbox_epoch")
        response_epoch = (
            int(response_epoch_raw)
            if isinstance(response_epoch_raw, (int, float)) and response_epoch_raw > 0
            else None
        )

    raw_items = payload.get("items") or []

    # Client-side filters
    items = list(raw_items)
    if args.conversation:
        items = _filter_conversation(
            items, _normalize_conversation_id(args.conversation)
        )
    if args.no_self:
        items = [it for it in items if not it.get("from_me")]

    if args.json:
        out: dict[str, Any] = {"items": items, "latest_seq": latest_seq_int}
        if response_epoch is not None:
            out["inbox_epoch"] = response_epoch
        print(json.dumps(out, indent=2))
    elif not items:
        print(f"no new messages (cursor={since})")
    else:
        for it in items:
            seq = it.get("seq", "?")
            ts = _iso_utc(it.get("ts"))
            chan = it.get("channel_id") or "?"
            from_label = "me" if it.get("from_me") else (it.get("user_id") or "?")
            text = (it.get("text") or "").rstrip("\n")
            print(f"seq={seq}  {ts}  from:{from_label}  chan:{chan}")
            for line in text.split("\n"):
                print(f"    {line}")

    # Persist cursor whenever gateway returned items (filter-independent).
    # --no-save overrides.
    if raw_items and not args.no_save:
        persist: dict[str, Any] = {"last_read_seq": latest_seq_int}
        if response_epoch is not None:
            persist["last_read_inbox_epoch"] = response_epoch
        _write_whatsapp_settings(persist)

    return 0


# ---------- argparse ----------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whatsapp_interface")
    p.add_argument(
        "--gateway-url", help=f"gateway base URL (default {DEFAULT_GATEWAY_URL})"
    )
    p.add_argument("--gateway-token", help="bearer token for gateway")

    sub = p.add_subparsers(dest="cmd", required=True)

    p_on = sub.add_parser(
        "onboard", help="link a WhatsApp account by polling QR + status"
    )
    p_on.add_argument(
        "--to", help="default test contact (E.164); persisted to settings"
    )
    p_on.add_argument(
        "--qr-out", help="path to write QR PNG (default ./whatsapp-qr.png)"
    )
    p_on.add_argument("--poll-interval", type=float, default=2.0)
    p_on.add_argument("--timeout", type=float, default=180.0)
    p_on.set_defaults(func=cmd_onboard)

    p_say = sub.add_parser("say", help="send a text message (DM or group)")
    p_say.add_argument("message")
    p_say.add_argument("--to", help="explicit DM destination E.164")
    p_say.add_argument("--group-jid", help="full group JID (120363…@g.us)")
    p_say.add_argument(
        "--ninja-prefix",
        action="store_true",
        help="prepend '🥷 Ninja: ' to the message so recipients see it as a bot reply "
        "(used by monitor_whatsapp.py; manual sends stay literal by default)",
    )
    p_say.add_argument(
        "--group",
        help="group local id, or 'last' for whatsapp.last_group_jid from settings",
    )
    p_say.add_argument(
        "--conversation",
        help="group conversation_id ({self}:g:{group_local}) — DM flags use --to instead",
    )
    p_say.set_defaults(func=cmd_say)

    p_read = sub.add_parser(
        "read",
        help="read inbox messages from the gateway (one-shot; cursor in ~/.agent_settings.json)",
    )
    p_read.add_argument(
        "--since",
        type=int,
        default=None,
        help="override cursor (default: whatsapp.last_read_seq from settings, else 0)",
    )
    p_read.add_argument(
        "--limit", type=int, default=50, help="max items (1..500, default 50)"
    )
    p_read.add_argument(
        "--conversation",
        help="filter by channel_id (DM: {self}:{peer}, group: {self}:g:{group_local})",
    )
    p_read.add_argument("--no-self", action="store_true", help="hide from_me messages")
    p_read.add_argument(
        "--json", action="store_true", help="print raw JSON instead of human lines"
    )
    p_read.add_argument(
        "--no-save", action="store_true", help="do not advance the stored cursor"
    )
    p_read.set_defaults(func=cmd_read)

    p_bind = sub.add_parser(
        "bind",
        help="set or inspect the single-chat binding (Ninja mode)",
    )
    p_bind.add_argument(
        "--chat-jid",
        help="WhatsApp chat JID to bind (e.g. 120363…@g.us or 1234567890@s.whatsapp.net)",
    )
    p_bind.add_argument(
        "--status",
        action="store_true",
        help="print current binding + pairing code (default when --chat-jid is omitted)",
    )
    p_bind.add_argument(
        "--json", action="store_true", help="print raw JSON instead of human lines"
    )
    p_bind.set_defaults(func=cmd_bind)

    p_unbind = sub.add_parser(
        "unbind", help="clear the single-chat binding (Ninja mode)"
    )
    p_unbind.set_defaults(func=cmd_unbind)

    p_fm = sub.add_parser(
        "fetch-media",
        help="download decrypted media bytes for a media_id (writes 0600 file)",
    )
    p_fm.add_argument(
        "--media-id", required=True, help="media_id from `read --json` (sha-prefix)"
    )
    p_fm.add_argument("--out", help="output path (default ./wa_<media_id>)")
    p_fm.set_defaults(func=cmd_fetch_media)

    p_up = sub.add_parser(
        "upload",
        help="send a local file as image or document (multipart POST /send_media)",
    )
    p_up.add_argument("--kind", required=True, help="image | document")
    p_up.add_argument("--file", required=True, help="path to the local file to upload")
    p_up.add_argument("--caption", help="caption text (image/pdf). Optional.")
    p_up.add_argument(
        "--filename", help="override the filename sent to WhatsApp (default basename)"
    )
    p_up.add_argument(
        "--mimetype", help="override mimetype (default: guess from extension)"
    )
    p_up.add_argument("--to", help="DM destination E.164")
    p_up.add_argument("--group-jid", help="full group JID (120363…@g.us)")
    p_up.add_argument("--group", help="group local id, or 'last'")
    p_up.add_argument(
        "--conversation", help="group conversation_id ({self}:g:{group_local})"
    )
    p_up.add_argument(
        "--ninja-prefix",
        action="store_true",
        help="prefix the caption with '🥷 Ninja:' (synthesizes one if --caption is empty)",
    )
    p_up.set_defaults(func=cmd_upload)

    p_react = sub.add_parser("react", help="add/replace a reaction on an inbox message")
    p_react.add_argument(
        "--message-key",
        required=True,
        help="message_key from `read --json` (e.g. 1234@s.whatsapp.net:3EB0...)",
    )
    p_react.add_argument(
        "--emoji", required=True, help="reaction emoji (use empty to remove)"
    )
    p_react.add_argument(
        "--from-me",
        action="store_true",
        help="original message was sent by us (set for from_me items)",
    )
    p_react.add_argument(
        "--participant",
        help="sender JID inside a group bind (required for group reactions; from inbox.participant)",
    )
    p_react.set_defaults(func=cmd_react)

    p_group = sub.add_parser("group", help="group operations")
    g_sub = p_group.add_subparsers(dest="group_cmd", required=True)
    p_gc = g_sub.add_parser("create", help="create a new group")
    p_gc.add_argument("subject", help="group subject/name")
    p_gc.add_argument("--participants", nargs="+", required=True, help="E.164 numbers")
    p_gc.add_argument("--welcome", help="optional welcome message after create")
    p_gc.set_defaults(func=cmd_group_create)
    p_gl = g_sub.add_parser("list", help="list WhatsApp groups you participate in")
    p_gl.add_argument("--json", action="store_true", help="print raw JSON")
    p_gl.set_defaults(func=cmd_group_list)

    return p


class WhatsAppInterface(MessagingInterface):
    """WhatsApp adapter — CLI module functions plus ABC surface for factory/health.

    monitor_service is imported inside methods only: it imports react from this
    module, so a top-level import would deadlock at startup.
    """

    shadow_flag = WHATSAPP_SHARED_MONITOR_FLAG

    def __init__(self) -> None:
        self._gateway_url: str = _gwurl()
        self._gateway_token: Optional[str] = _gwtok()
        self._bound_via: str | None = None
        self._bound_chat_jid: str | None = None
        self._inbox_epoch: int | None = None
        self._tracked_bound_jid: str | None = None
        self._saw_unbound: bool = False
        self._progress_text = ""
        self._progress_id: Optional[str] = None

    # The gateway has no progress bar of its own, so this adapter renders it
    # straight from the live progress files and edits the message itself.
    supports_activity_progress = True
    activity_provider = "whatsapp"

    def start_activity(self, thread_id, username=None, icon_url=None):
        jid = (self._refresh_gateway_context().get("bound_chat_jid") or "").strip()
        self._progress_text = self._render_progress(live_progress.read_state())
        sent = _send_to_bound_chat(
            self._gateway_url, self._gateway_token, jid, self._progress_text
        )
        self._progress_id = (sent or {}).get("id")
        return {"id": self._progress_id} if self._progress_id else None

    def add_completed_action(self, thread_id, label):
        return True  # Rendering reads the steps file itself.

    def tick_activity(self, thread_id):
        state = live_progress.read_state()
        text = self._render_progress(state)
        if text == self._progress_text:
            return True
        status, _, _ = _request(
            "POST",
            f"{self._gateway_url}/edit",
            token=self._gateway_token,
            body={"id": state.get("activity_id"), "text": text},
        )
        if status != 200:
            return False
        self._progress_text = text
        return True

    def complete_activity(self, thread_id):
        message_id = live_progress.read_state().get("activity_id") or self._progress_id
        return bool(message_id) and delete_sent_message(message_id)

    def _render_progress(self, state: dict) -> str:
        labels = [step["label"] for step in live_progress.read_steps()]
        return render_progress(labels, live_progress.seconds_running(state))

    def _refresh_gateway_context(self) -> dict[str, Any]:
        st = _gateway_status(self._gateway_url, self._gateway_token) or {}
        self._bound_via = st.get("bound_via")
        raw_jid = (st.get("bound_chat_jid") or "").strip()
        self._bound_chat_jid = raw_jid or None
        epoch_raw = st.get("inbox_epoch")
        if isinstance(epoch_raw, (int, float)) and epoch_raw > 0:
            self._inbox_epoch = int(epoch_raw)
        return st

    def _bound_conversation_id(self, st: dict[str, Any]) -> Optional[str]:
        bound_jid = (st.get("bound_chat_jid") or "").strip()
        self_e164 = (st.get("self_e164") or "").strip()
        if not bound_jid or not self_e164:
            return None
        return _normalize_conversation_id(_jid_to_conversation_id(self_e164, bound_jid))

    def _cold_start_skip_inbox(self) -> None:
        """Jump the cursor past a pre-bind backlog. The mirror is purged on
        bind, so there is nothing to skip there."""
        if is_feature_enabled(WHATSAPP_SQLITE_CACHE_FLAG):
            return
        status, payload, _ = _read_messages_once(
            self._gateway_url, self._gateway_token, since=0, limit=50
        )
        if status != 200:
            return
        latest_seq = int(payload.get("latest_seq") or 0)
        patch: dict[str, Any] = {"last_read_seq": latest_seq}
        response_epoch_raw = payload.get("inbox_epoch")
        if isinstance(response_epoch_raw, (int, float)) and response_epoch_raw > 0:
            patch["last_read_inbox_epoch"] = int(response_epoch_raw)
            self._inbox_epoch = int(response_epoch_raw)
        _write_whatsapp_settings(patch)

    def _ensure_poll_ready(self) -> Optional[str]:
        st = self._refresh_gateway_context()
        bound_jid = self._bound_chat_jid
        if not bound_jid:
            self._saw_unbound = True
            return None

        if self._saw_unbound and self._bound_via == "auto_group":
            disk = load_agent_messages()
            if "welcomed" in disk or "welcomed_chat_jid" in disk:
                updated = dict(disk)
                updated.pop("welcomed", None)
                updated.pop("welcomed_chat_jid", None)
                save_agent_messages(updated)
        self._saw_unbound = False

        epoch_raw = st.get("inbox_epoch")
        epoch = (
            int(epoch_raw)
            if isinstance(epoch_raw, (int, float)) and epoch_raw > 0
            else None
        )
        bind_changed = bound_jid != self._tracked_bound_jid
        if bind_changed:
            self._cold_start_skip_inbox()
            clear_recent_messages()
            self._tracked_bound_jid = bound_jid
        elif epoch is not None:
            stored_raw = _settings_whatsapp().get("last_read_inbox_epoch")
            stored_epoch = (
                int(stored_raw)
                if isinstance(stored_raw, (int, float)) and stored_raw > 0
                else None
            )
            if stored_epoch != epoch:
                _write_whatsapp_settings({"last_read_inbox_epoch": epoch})

        return self._bound_conversation_id(st)

    def _include_from_me(self) -> bool:
        from services.whatsapp.monitor_service import resolve_include_from_me

        self._refresh_gateway_context()
        return resolve_include_from_me(bound_via=self._bound_via, cli_include=False)

    def advance_read_cursor(self, *, pending_messages: list) -> None:
        """Advance the ring cursor. The mirror path dedupes on
        ``seen_messages`` instead, like Slack and Teams."""
        if is_feature_enabled(WHATSAPP_SQLITE_CACHE_FLAG):
            return
        max_seq = 0
        for message in pending_messages:
            raw = (message.raw or {}) if isinstance(message, PendingMessage) else {}
            seq = raw.get("seq")
            if isinstance(seq, (int, float)) and seq > max_seq:
                max_seq = int(seq)
        if max_seq <= 0:
            return
        current = int(_settings_whatsapp().get("last_read_seq") or 0)
        if max_seq > current:
            _write_whatsapp_settings({"last_read_seq": max_seq})

    @property
    def is_connected(self) -> bool:
        st = _gateway_status(self._gateway_url, self._gateway_token)
        return bool(st and st.get("linked"))

    def get_unique_channel(self) -> str:
        return _resolve_whatsapp_unique_channel()

    def upload_file(
        self,
        file_path: str,
        channel: Optional[str] = None,
        title: Optional[str] = None,
        comment: Optional[str] = None,
        thread_ts: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> dict[str, Any]:
        path = Path(file_path).expanduser()
        if not path.exists() or not path.is_file():
            raise ValueError(f"file not found: {path}")

        base = self._gateway_url
        token = self._gateway_token
        file_bytes = path.read_bytes()
        mimetype, _ = mimetypes.guess_type(str(path))
        mimetype = mimetype or "application/octet-stream"
        kind = "image" if (mimetype or "").startswith("image/") else "document"
        caption = comment or title or ""

        args = argparse.Namespace(
            kind=kind,
            file=str(path),
            mimetype=mimetype,
            caption=caption,
            filename=path.name,
            ninja_prefix=False,
            to=channel,
            group_jid=None,
            group=None,
            conversation=thread_ts,
            gateway_url=None,
            gateway_token=None,
        )
        try:
            group_jid = resolve_group_target(args)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        fields: list[tuple[str, str]] = [
            ("kind", kind),
            ("mimetype", mimetype),
            ("filename", path.name),
        ]
        if caption:
            fields.append(("caption", caption))
        if group_jid:
            fields.append(("group_jid", group_jid))
        elif channel:
            try:
                fields.append(("to", resolve_whatsapp_to(channel)))
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        else:
            st = _gateway_status(base, token) or {}
            bound_chat_jid = (st.get("bound_chat_jid") or "").strip()
            if not bound_chat_jid:
                raise ValueError("no bound chat — bind WhatsApp before uploading files")
            if bound_chat_jid.endswith("@g.us"):
                fields.append(("group_jid", bound_chat_jid))
            else:
                digits = bound_chat_jid.split("@", 1)[0].split(":", 1)[0]
                fields.append(("to", digits))

        body, ctype = _multipart_encode(fields, "file", path.name, mimetype, file_bytes)
        headers = {"Content-Type": ctype, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"{base}/send_media", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"whatsapp gateway not reachable at {base}: {exc.reason}"
            ) from exc

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, AttributeError) as exc:
            raise RuntimeError(
                f"whatsapp upload returned non-JSON (status {status}): {raw[:200]!r}"
            ) from exc
        if status != 200 or not parsed.get("ok"):
            raise RuntimeError(f"whatsapp upload failed: status={status} body={parsed}")
        return parsed

    def say(
        self,
        message: str,
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
        username: Optional[str] = None,
        icon_emoji: Optional[str] = None,
        icon_url: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> dict[str, Any]:
        message = markdown_to_whatsapp(
            convert_sandbox_urls(message, force_public_urls=True)
        )
        if not channel and not thread_ts:
            st = self._refresh_gateway_context()
            bound_jid = (st.get("bound_chat_jid") or "").strip()
            if bound_jid:
                if not _send_to_bound_chat(
                    self._gateway_url, self._gateway_token, bound_jid, message
                ):
                    raise RuntimeError("whatsapp say failed: bound chat send failed")
                return {"ok": True}

        args = argparse.Namespace(
            message=message,
            to=channel,
            group_jid=None,
            group=None,
            conversation=None,
            ninja_prefix=False,
            gateway_url=None,
            gateway_token=None,
        )
        if thread_ts:
            args.conversation = thread_ts
        code = cmd_say(args)
        if code != 0:
            raise RuntimeError(f"whatsapp say failed with exit code {code}")
        return {"ok": True}

    def get_history(
        self,
        channel: Optional[str] = None,
        limit: int = 10,
    ) -> List[dict[str, Any]]:
        if channel is None:
            channel = self._ensure_poll_ready()
            if channel is None:
                return []

        target = _normalize_conversation_id(channel)

        if is_feature_enabled(WHATSAPP_SQLITE_CACHE_FLAG):
            items = self._db_history(limit, target)
            if items is not None:
                return items

        since = int(_settings_whatsapp().get("last_read_seq") or 0)
        status, payload, _raw = _read_messages_once(
            self._gateway_url, self._gateway_token, since, limit
        )
        if status != 200:
            raise RuntimeError(f"whatsapp get_history failed: status={status}")
        return _filter_conversation(list(payload.get("items") or []), target)

    def _db_history(self, limit: int, target: str) -> Optional[List[dict[str, Any]]]:
        """Read the SQLite mirror, or None on any failure (use /messages)."""
        # Exact-match filter server-side, so raw-JID rows need a second query.
        items: List[dict[str, Any]] = []
        for channel_id in filter(None, (target, _raw_chat_jid(target))):
            try:
                status, payload, _raw = _read_db_messages_once(
                    self._gateway_url, self._gateway_token, limit, channel_id
                )
            except Exception as exc:
                logger.warning(
                    "WhatsApp /db/messages failed", extra={"error": str(exc)}
                )
                return None
            if status != 200:
                logger.info(
                    "WhatsApp /db/messages unavailable, using /messages",
                    extra={"status": status},
                )
                return None
            items.extend(payload.get("items") or [])
        items = _filter_conversation(items, target)
        items.sort(key=lambda item: item.get("ts") or 0)
        return items[-limit:]

    def collect_pending(
        self,
        msg: dict[str, Any],
        agent_mentions: List[str],
        seen_messages: set,
        agent_data: dict,
        message_states: MessageStateBatch | None = None,
        *,
        shadow_mode: bool = False,
    ) -> List[PendingMessage]:
        from services.whatsapp.monitor_service import (
            inbound_item_to_pending,
            should_dispatch_inbound_item,
        )

        msg_id = msg.get("message_key") or str(msg.get("ts") or "")
        if not msg_id or msg_id in seen_messages:
            return []

        bound_channel = self._ensure_poll_ready()
        if bound_channel is None:
            seen_messages.add(msg_id)
            if message_states is not None:
                message_states.seen(msg_id, None, will_reply=False)
            return []

        if msg.get("channel_id"):
            matched = _in_conversation(msg, bound_channel)
            if matched is None:
                seen_messages.add(msg_id)
                if message_states is not None:
                    message_states.seen(msg_id, None, will_reply=False)
                return []
            msg = matched

        if not should_dispatch_inbound_item(
            msg,
            include_from_me=self._include_from_me(),
            bound_via=self._bound_via,
        ):
            seen_messages.add(msg_id)
            if message_states is not None:
                message_states.seen(msg_id, None, will_reply=False)
            return []

        if not shadow_mode:
            emoji = os.environ.get("NINJA_AGENT_EMOJI", "🥷").strip()
            if emoji and msg.get("message_key"):
                react(
                    self._gateway_url,
                    self._gateway_token,
                    str(msg["message_key"]),
                    emoji,
                    from_me=bool(msg.get("from_me")),
                    participant=msg.get("participant"),
                )

        # Mark at collection time like Slack. The mirror ignores the cursor,
        # so without this every poll re-dispatches the same rows.
        seen_messages.add(msg_id)
        if message_states is not None:
            message_states.seen(msg_id, None, will_reply=True)
        raw = inbound_item_to_pending(msg)
        user_id = raw["user"]
        sender_name = raw.get("sender_name") or user_id
        msg_key = str(raw.get("message_key") or raw.get("timestamp") or "")
        return [
            PendingMessage(
                id=msg_key,
                thread_id=raw.get("channel_id") or "",
                channel_id=raw.get("channel_id") or "",
                text=raw.get("text") or "",
                sender=Member(user_id=user_id, display_name=sender_name),
                type="whatsapp",
                raw=raw,
            )
        ]

    def prime_seen_state(
        self,
        seen_messages: set,
        agent_data: dict,
        message_states: MessageStateBatch | None = None,
    ) -> None:
        try:
            st = self._refresh_gateway_context()
            if not self._bound_chat_jid:
                agent_data["primed"] = True
                return
            bound_channel = self._bound_conversation_id(st)
            if is_feature_enabled(WHATSAPP_SQLITE_CACHE_FLAG):
                self._prime_from_cache(seen_messages, message_states, bound_channel)
                agent_data["primed"] = True
                return
            stored_seq = _stored_read_seq()
            status, payload, _ = _read_messages_once(
                self._gateway_url, self._gateway_token, since=0, limit=50
            )
            if status != 200:
                return
            for item in payload.get("items") or []:
                if (
                    bound_channel
                    and item.get("channel_id")
                    and _in_conversation(item, bound_channel) is None
                ):
                    continue
                item_seq = item.get("seq")
                if stored_seq > 0:
                    if (
                        not isinstance(item_seq, (int, float))
                        or int(item_seq) > stored_seq
                    ):
                        continue
                msg_id = item.get("message_key") or str(item.get("ts") or "")
                if msg_id:
                    seen_messages.add(msg_id)
                    if message_states is not None:
                        message_states.seen(msg_id, None, will_reply=False)
            latest_seq = int(payload.get("latest_seq") or 0)
            if stored_seq == 0 and latest_seq > 0:
                _write_whatsapp_settings({"last_read_seq": latest_seq})
            agent_data["primed"] = True
        except Exception as exc:
            logger.warning(
                "WhatsApp prime_seen_state failed", extra={"error": str(exc)}
            )

    def _prime_from_cache(
        self,
        seen_messages: set,
        message_states: MessageStateBatch | None,
        bound_channel: Optional[str],
    ) -> None:
        """Cold start only: mark what the mirror holds as seen, like Teams."""
        for item in self.get_history(channel=bound_channel, limit=50):
            msg_id = item.get("message_key") or str(item.get("ts") or "")
            if not msg_id:
                continue
            seen_messages.add(msg_id)
            if message_states is not None:
                message_states.seen(msg_id, None, will_reply=False)

    def post_welcome_if_needed(
        self, agent: dict, welcome_text: str, welcome_signature: str
    ) -> bool:
        """Post welcome once when bound; signature-only history sniff (limit 50)."""
        agent = {**agent, **load_agent_messages()}

        def _persist_welcomed(*, bound_chat_jid: str) -> None:
            agent["welcomed"] = True
            agent["welcomed_chat_jid"] = bound_chat_jid
            save_agent_messages({**load_agent_messages(), **agent})

        try:
            st = _gateway_status(self._gateway_url, self._gateway_token)
            bound_chat_jid = (st or {}).get("bound_chat_jid")
            if not bound_chat_jid:
                return False

            welcomed_jid = agent.get("welcomed_chat_jid")
            if agent.get("welcomed") and welcomed_jid == bound_chat_jid:
                return False

            same_chat = welcomed_jid in (None, bound_chat_jid)
            if same_chat:
                for text in _recent_gateway_texts(
                    self._gateway_url, self._gateway_token
                ):
                    if welcome_signature in text:
                        _persist_welcomed(bound_chat_jid=bound_chat_jid)
                        return False

            message = markdown_to_whatsapp(
                convert_sandbox_urls(welcome_text, force_public_urls=True)
            )
            if not _send_to_bound_chat(
                self._gateway_url, self._gateway_token, bound_chat_jid, message
            ):
                return False

            _persist_welcomed(bound_chat_jid=bound_chat_jid)
            self.capture_welcome_message("whatsapp")
            return True
        except Exception as e:
            logger.warning("Welcome announcement skipped", extra={"error": str(e)})
            return False

    def check_messaging_health(self) -> dict[str, Any]:
        try:
            status, raw, _ = _request(
                "GET", f"{self._gateway_url}/status", token=self._gateway_token
            )
            if status == 0:
                return {
                    "service": "whatsapp",
                    "status": "error",
                    "message": "gateway not reachable",
                }
            if status == 401:
                return {
                    "service": "whatsapp",
                    "status": "invalid",
                    "message": "gateway token rejected",
                }
            if status != 200:
                return {
                    "service": "whatsapp",
                    "status": "error",
                    "message": f"gateway status={status}",
                }
            body = json.loads(raw.decode("utf-8"))
            linked = bool(body.get("linked"))
            return {
                "service": "whatsapp",
                "status": "ok" if linked else "missing",
                "message": None if linked else "gateway up but not linked",
                "team": body.get("self_e164"),
            }
        except Exception as exc:
            return {"service": "whatsapp", "status": "error", "message": str(exc)}


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
