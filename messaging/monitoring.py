"""Contains functions used for monitoring the agent's message handling"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from clients.agent_event_cache_client import (
    AgentEventCacheClient,
    is_event_cache_enabled,
)
from clients.agent_event_cache_models import (
    CompleteActivityRequest,
    MessageProvider,
    MessageStateReport,
    ReportMessageStatesRequest,
    ReportRepliedRequest,
)
from constants import LIVE_PROGRESS_ANSWER_SECONDS
from core import live_progress
from messaging.pending import PendingMessage

logger = logging.getLogger(__name__)


class MessageStateBatch:
    """Triage decisions from one poll — the interface adapters code against."""

    def __len__(self) -> int:
        return 0

    def __iter__(self) -> Iterator[MessageStateReport]:
        return iter(())

    def seen(self, message_id: str, thread_id: str | None, will_reply: bool) -> None:
        """Note that ``message_id`` was seen, and whether it will be replied to."""
        return

    def publish(self) -> None:
        """Report the batch upstream."""
        return


# Inert and stateless, so one shared instance is safe.
NO_MESSAGE_STATES = MessageStateBatch()


@dataclass
class EventCacheMessageStateBatch(MessageStateBatch):
    """Records decisions and reports them to the event cache."""

    workspace_id: str | None = None
    channel_id: str | None = None
    _states: list[MessageStateReport] = field(default_factory=list, repr=False)

    def __len__(self) -> int:
        return len(self._states)

    def __iter__(self) -> Iterator[MessageStateReport]:
        return iter(self._states)

    def seen(self, message_id: str, thread_id: str | None, will_reply: bool) -> None:
        """Record that ``message_id`` was seen, and whether it will be replied to."""
        self._states.append(
            MessageStateReport(
                message_id=message_id,
                thread_id=thread_id,
                will_reply=will_reply,
            )
        )

    def publish(self) -> None:
        """Report the batch upstream. No-op when empty; never raises."""
        if not self._states:
            return
        if not self.workspace_id or not self.channel_id:
            # Misconfiguration, not a normal skip: without both ids nothing can
            # ever be published, so every message this poll saw stays pending.
            logger.warning(
                f"Dropping {len(self._states)} message states: missing ids "
                f"(workspace={self.workspace_id!r}, channel={self.channel_id!r}). "
                f"Reporting is disabled until both are configured as platform ids."
            )
            return
        try:
            if not is_event_cache_enabled():
                logger.debug(
                    f"Event cache disabled — dropping {len(self._states)} "
                    f"message states"
                )
                return
            result = AgentEventCacheClient().report_message_states(
                ReportMessageStatesRequest(
                    workspace_id=self.workspace_id,
                    channel_id=self.channel_id,
                    states=self._states,
                )
            )
            logger.info(
                f"Reported {len(self._states)} message states: "
                f"updated={result.updated}, deleted={result.deleted}, "
                f"not_found={result.not_found}"
            )
            if result.not_found == len(self._states):
                # Every id unmatched is the signature of a workspace/channel id
                # mismatch, which makes the whole feature a silent no-op.
                logger.warning(
                    f"No pending rows matched any of {len(self._states)} reported "
                    f"states (workspace={self.workspace_id}, "
                    f"channel={self.channel_id}) — check that these are platform "
                    f"ids, not display names"
                )
        except Exception as exc:
            logger.error(f"Failed to report {len(self._states)} message states: {exc}")


def clear_live_progress() -> None:
    """Complete the progress bar, if one is up. The answer makes it redundant.

    Runs in the ``say``/``upload`` process, so the addressing comes off disk —
    provider included, which is why one function serves every adapter. Errors
    propagate: the caller logs and leaves the state for the monitor's own
    end-of-run cleanup to retry.
    """
    # No is_event_cache_enabled() gate: the activity endpoints never had one,
    # and a sandbox with the cache off still shows the bar.
    state = live_progress.read_state()
    if state.get("activity_started"):
        provider = state.get("provider")
        if provider == "whatsapp":
            # Lazy: the WhatsApp adapter imports this module.
            from messaging.whatsapp.interface import delete_sent_message

            if not delete_sent_message(state.get("activity_id") or ""):
                return
        else:
            workspace_id = state.get("workspace_id")
            channel = state.get("channel")
            thread_id = state.get("thread_ts")
            if not all([provider, workspace_id, channel, thread_id]):
                return
            AgentEventCacheClient().complete_activity(
                CompleteActivityRequest(
                    provider=MessageProvider(provider),
                    workspace_id=workspace_id,
                    channel_id=channel,
                    thread_id=thread_id,
                )
            )
        # Only once the bar is up: an early "on it" must not disarm the run.
        live_progress.clear_state()
    elif live_progress.seconds_running(state) >= LIVE_PROGRESS_ANSWER_SECONDS:
        live_progress.clear_state()


def publish_message_handled(
    workspace_id: str | None,
    channel_id: str | None,
    thread_id: str | None = None,
    reply_message_id: str | None = None,
) -> None:
    """Mark a thread handled upstream."""
    try:
        if not is_event_cache_enabled():
            logger.debug(
                f"Event cache disabled — not publishing handled for "
                f"thread {thread_id}"
            )
            return
        if not workspace_id or not channel_id:
            logger.warning(
                f"Cannot publish handled for thread {thread_id}: missing ids "
                f"(workspace={workspace_id!r}, channel={channel_id!r}). Reporting "
                f"is disabled until both are configured as platform ids."
            )
            return
        AgentEventCacheClient().report_replied(
            ReportRepliedRequest(
                workspace_id=workspace_id,
                channel_id=channel_id,
                thread_id=thread_id,
                reply_message_id=reply_message_id,
            )
        )
    except Exception as exc:
        logger.warning(f"Failed to publish handled for thread {thread_id}: {exc}")


def _unique_thread_ids(
    pending_messages: list[PendingMessage],
) -> Iterator[str | None]:
    seen: set[str | None] = set()
    for msg in pending_messages:
        if msg.type == "cron":
            continue
        # Normalise "" to None so both spellings of "no thread" dedupe into a
        # single channel-wide call.
        thread_id = msg.thread_id or None
        if thread_id in seen:
            continue
        seen.add(thread_id)
        yield thread_id


def publish_all_handled(
    workspace_id: str | None,
    channel_id: str | None,
    pending_messages: list[PendingMessage],
) -> None:
    """Publish every message as handled"""
    for thread_id in _unique_thread_ids(pending_messages):
        publish_message_handled(workspace_id, channel_id, thread_id)
    # All messages in the channel are maked handled as well
    publish_message_handled(workspace_id, channel_id)
