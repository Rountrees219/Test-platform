"""
MessagingInterface — channel-agnostic ABC.

Each channel adapter (slack/, whatsapp/, teams/) must implement this interface.
Internal code should import only from this module, never from a specific channel.

The contract here is exactly what ``processes/monitor.py`` calls directly.
Everything else (react, get_replies, is_own_message, is_bot_message,
is_human_message, has_audio_attachment, upload_file, is_connected, …) is an
internal implementation detail of each adapter — not part of this contract.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from clients.posthog_client import capture
from clients.super_ninja_client import get_thread_id
from messaging.monitoring import MessageStateBatch
from messaging.pending import FileAttachment, PendingMessage


class MessagingInterface(ABC):
    """Abstract base class for all messaging channel adapters."""

    def capture_welcome_message(self, channel: str) -> None:
        """Emit the ninja_welcome_message PostHog event; never raises."""
        try:
            capture(
                "ninja_welcome_message",
                {"channel": channel, "thread_id": get_thread_id()},
            )
        except Exception:
            print("⚠️ Failed to capture welcome message event", file=sys.stderr)

    # ------------------------------------------------------------------
    # Connection / health
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the channel is reachable (tokens present, etc.)."""

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    @abstractmethod
    def say(
        self,
        message: str,
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
        username: Optional[str] = None,
        icon_emoji: Optional[str] = None,
        icon_url: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a text message.

        Args:
            message:    Message body (Markdown supported where the channel allows).
            channel:    Override the default channel/conversation.
            thread_ts:  Thread / reply-to identifier (channel-specific format).
            username:   Display name override for this message.
            icon_emoji: Emoji avatar override.
            icon_url:   Image URL avatar override.
            agent:      Named agent whose configured identity to use.

        Returns:
            Channel API response dict.
        """

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @abstractmethod
    def upload_file(
        self,
        file_path: str,
        channel: Optional[str] = None,
        title: Optional[str] = None,
        comment: Optional[str] = None,
        thread_ts: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a file and share it in the channel.

        Args:
            file_path: Local path to the file.
            channel:   Override the default channel/conversation.
            title:     Optional display title for the file.
            comment:   Optional text to accompany the file.
            thread_ts: Thread / reply-to identifier.
            agent:     Named agent whose identity to post under.

        Returns:
            Channel API response dict.
        """

    @abstractmethod
    def get_history(
        self,
        channel: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Retrieve recent messages from a channel.

        Args:
            channel: Override the default channel/conversation.
            limit:   Maximum number of messages to return.

        Returns:
            List of message dicts (structure is channel-specific).
        """

    # ------------------------------------------------------------------
    # Monitor integration
    # ------------------------------------------------------------------

    @abstractmethod
    def collect_pending(
        self,
        msg: Dict[str, Any],
        agent_mentions: List[str],
        seen_messages: set,
        agent_data: dict,
        message_states: MessageStateBatch | None = None,
        *,
        shadow_mode: bool = False,
    ) -> List[PendingMessage]:
        """Process a single raw message from get_history().

        For each unseen, non-own message this method:
          1. Reacts with a channel-native ack when warranted.
          2. Returns the message as a ``PendingMessage`` if the agent should respond.
          3. Scans for unseen thread replies and returns those too.
          4. Appends triage decisions to ``message_states`` (if provided) for
             upstream pending-message tracking (SN-3996).

        Mutates ``seen_messages``, ``agent_data``, and ``message_states`` in
        place. Returns the actionable messages rather than mutating a
        caller-provided list — the caller accumulates them across calls.

        The ack emoji, thread reply schema, and message classification
        (own / bot / human / audio) are all internal to each adapter.

        Args:
            msg:              A single message dict from ``get_history()``.
            agent_mentions:   List of keyword strings that trigger a response
                              (e.g. ["ninja", "@ninja"]).
            seen_messages:    Set of already-processed message IDs. Updated in place.
            agent_data:       Persistent state dict (seen_replies, welcomed, …).
                              Updated in place.
            message_states:   Batch to record triage decisions on via
                              ``seen()``. Updated in place. ``None`` when
                              tracking is disabled.

        Returns:
            Actionable messages from this call (the message itself and/or its
            unseen thread replies). Empty when nothing needs a response.
        """

    @abstractmethod
    def post_welcome_if_needed(
        self, agent: dict, welcome_text: str, welcome_signature: str
    ) -> bool:
        """Post ``welcome_text`` if the channel has no prior human messages.

        Idempotent — checks both a persisted flag in ``agent_data`` and a
        signature string in channel history before posting. Should never raise;
        errors are swallowed so they do not block monitor startup.

        Args:
            agent:        Agent config dict (name, emoji, role, mentions, …).
            welcome_text: The fully-rendered welcome message to post.

        Returns:
            True if the message was actually posted this call, False otherwise.
        """

    def prime_seen_state(
        self,
        seen_messages: set,
        agent_data: dict,
        message_states: MessageStateBatch | None = None,
    ) -> None:
        """Initialize the seen-message state"""
        return

    # ------------------------------------------------------------------
    # Health service integration
    # ------------------------------------------------------------------

    @abstractmethod
    def check_messaging_health(self) -> Dict[str, Any]:
        """Validate the messaging channel credentials and connectivity.

        Called by ``processes/health_service.py`` on each health check cycle.
        Should never raise — all errors must be caught and returned as a status
        dict so the health service can continue with the remaining checks.

        Returns:
            Dict with at minimum:
                ``service``  — channel name string (e.g. "slack", "whatsapp")
                ``status``   — one of: "ok", "rotated", "missing", "invalid", "error"
            Optional keys:
                ``message``  — human-readable error detail
                ``team``     — workspace / account name on success (if available)
        """

    # ------------------------------------------------------------------
    # Configuration helpers (optional — channels may override)
    # ------------------------------------------------------------------

    @property
    def default_channel(self) -> Optional[str]:
        """Return the configured default channel identifier, if any."""
        return None

    @property
    def default_channel_name(self) -> Optional[str]:
        """Return the human-readable default channel name, if any."""
        return None

    def reporting_ids(self) -> tuple[str | None, str | None]:
        """Return ``(workspace_id, channel_id)`` for pending-message reports.

        Strict platform IDs only — never display names. The cache service
        matches ``pending_messages`` rows on IDs, so a name like ``#general``
        matches nothing and every report silently no-ops. Adapters that cannot
        supply both IDs return ``(None, None)``, which callers read as
        "reporting unavailable" and skip.
        """
        return None, None

    # Check this before posting anything you plan to edit or delete later.
    supports_message_edit: bool = False

    def update_message(
        self,
        ts: str,
        text: str,
        blocks: Optional[list] = None,
        channel: Optional[str] = None,
    ) -> bool:
        """Rewrite a message this agent posted. False when unsupported.

        Not abstract on purpose — only Slack implements it, and the others just
        report failure rather than break.
        """
        return False

    def delete_message(self, ts: str, channel: Optional[str] = None) -> bool:
        """Delete a message this agent posted. False when unsupported."""
        return False

    # ------------------------------------------------------------------
    # Activity progress (server-side progress bar)
    # ------------------------------------------------------------------

    # Provider identifier for activity progress state (e.g. "slack", "teams").
    # Adapters that support activity progress must set this.
    activity_provider: Optional[str] = None

    # Check this before starting activity progress for a run.
    supports_activity_progress: bool = False

    def activity_workspace_id(self) -> Optional[str]:
        """Workspace id for activity progress. None when unsupported.

        Resolved, not read raw: a configured id is often absent, and an
        unresolved one addresses an activity the server cannot find.
        """
        return None

    def start_activity(
        self,
        thread_id: str,
        username: Optional[str] = None,
        icon_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Start a progress bar in the thread. None when unsupported."""
        return None

    def add_completed_action(self, thread_id: str, label: str) -> bool:
        """Report a completed step to the progress bar. False when unsupported."""
        return False

    def tick_activity(self, thread_id: str) -> bool:
        """Heartbeat to advance the progress bar. False when unsupported."""
        return False

    def complete_activity(self, thread_id: str) -> bool:
        """Remove the progress bar from the thread. False when unsupported."""
        return False

    @abstractmethod
    def get_unique_channel(self) -> str:
        """Return a stable, workspace-scoped identifier for Pipedream Connect.

        Used as the ``x-ninja-integration-channel-id`` header value sent to the
        ninja-integrations-gateway. Must be stable for the lifetime of the
        workspace — changing it creates a new empty gateway identity and
        loses all connected apps.

        The value should be prefixed with the messaging adapter type, e.g.
        ``<adapter>-<workspace_id>.<channel_id>``.

        Raises:
            ValueError: If the required identity fields are not configured.
        """
