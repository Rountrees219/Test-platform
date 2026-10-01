"""WhatsApp prompt strategy — provides WhatsApp-specific prompt variables."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from messaging.pending import PendingMessage
from processes.base import PromptStrategy, PromptVars
from services.whatsapp.monitor_service import (
    _build_prompt,
    _gateway_token,
    _gateway_url,
    _get_status,
    _record_pending,
    _render_system_prompt,
)

# Present while a turn runs.  services.whatsapp.queued_ack watches this and
# auto-replies "message queued" to messages that arrive mid-turn.  Path MUST
# match services/whatsapp/queued_ack.py:BUSY_MARKER.
BUSY_MARKER = Path("/workspace/.ninja-turn-busy")


class WhatsAppPromptStrategy(PromptStrategy):
    """WhatsApp channel prompt variable provider."""

    def get_prompt_vars(self, config: dict) -> PromptVars:
        wa = config.get("whatsapp") if isinstance(config.get("whatsapp"), dict) else {}
        channel = wa.get("channel_label") or wa.get("bound_chat_jid") or "(unbound)"
        return PromptVars(
            channel=channel,
            default_task=(
                f"Check WhatsApp ({channel}) for new requests via "
                "`cd /workspace && python -m ninja.whatsapp_interface read --json`, "
                "do your work, reply with "
                '`cd /workspace && python -m ninja.whatsapp_interface say "<text>" '
                "--group-jid <jid>` (or `--to <jid>` for DMs), then reflect per "
                "agent-docs/ORCHESTRATOR.md."
            ),
            interface_doc=(
                "3. **WhatsApp Interface Docs:** "
                "`cat agent-docs/WHATSAPP_INTERFACE.md`"
            ),
        )

    def build_batch_prompt(
        self, agent: dict, pending_messages: list[PendingMessage]
    ) -> str:
        st = _get_status(_gateway_url(), _gateway_token()) or {}
        bound_chat_jid = st.get("bound_chat_jid") or ""
        legacy_pending = [dict(message.raw) for message in pending_messages]
        _record_pending(legacy_pending)
        return _build_prompt(
            legacy_pending,
            bound_chat_jid,
            same_session_continuation=True,
        )

    def resolve_system_prompt(self) -> Optional[Path]:
        st = _get_status(_gateway_url(), _gateway_token()) or {}
        bound_chat_jid = st.get("bound_chat_jid")
        if not bound_chat_jid:
            return None
        return _render_system_prompt(bound_chat_jid)
