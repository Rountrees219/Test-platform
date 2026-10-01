"""
Shared process-layer utilities and implementations — not channel-specific.

This module holds:
  - Constants (poll intervals, backoff settings, heartbeat paths)
  - Rate limit handler
  - Heartbeat / orchestrator launch / GitHub token sync helpers
  - ``PollingMonitorStrategy`` — the concrete poll-based monitor loop that
    Slack and Teams inherit from

Per-channel subclasses live in ``processes/<channel>/`` and are pruned at
build time by ``channel_builder``.  This file is never pruned.

Dependency flow::

    base.py          <- ABCs (no deps)
    common.py        <- shared utilities + PollingMonitorStrategy (imports base.py)
    factory.py       <- imports slack/, teams/, whatsapp/ strategies
    slack/           <- imports common.py
    teams/           <- imports common.py
    whatsapp/        <- imports base.py directly
    monitor.py       <- thin entry point (imports factory.py, no one imports it)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from agents_config import AGENTS
from clients.posthog_client import capture, is_feature_enabled
from clients.token_resolver import resolve_github_token
from constants import (
    MONITOR_SERVICE_NAME,
    SANDBOX_METADATA_PATH,
)
from core.config import (
    install_sighup_handler,
    is_orchestrator_enabled,
    load_agent_config,
    load_agent_messages,
    load_seen_messages,
    load_sent_messages,
    refresh_config,
    save_agent_messages,
    save_seen_messages,
)
from core.logging import get_logger
from messaging.cache_identity import Member
from messaging.factory import get_messaging_interface, resolve_messaging_channel
from messaging.message_utils import mark_handled
from messaging.monitoring import (
    EventCacheMessageStateBatch,
    publish_all_handled,
)
from messaging.pending import PendingMessage
from processes.agent_status import AgentStatusReporter
from processes.base import MonitorStrategy
from processes.orchestrator import (
    ORCHESTRATOR_SERVICE,
    count_open_issues,
    is_orchestrator_running,
)
from services.cron_service import claim_cron, get_due_cron_messages
from services.monitor_service import (
    build_welcome_message,
    build_welcome_signature,
    end_live_progress,
    run_batched_response,
    start_live_progress,
)
from services.progress_service import ProgressService
from utils.cost import check_cost_limit
from utils.vision_skill import apply_vision_skill

logger = get_logger(MONITOR_SERVICE_NAME)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_INTERVAL = 60  # base seconds
POLL_JITTER = 5  # random jitter seconds
MAX_RUNTIME = 24 * 60 * 60  # 24 hours in seconds

BACKOFF_INITIAL = 60  # Initial backoff: 1 minute
BACKOFF_MAX = 600  # Max backoff: 10 minutes
BACKOFF_MULTIPLIER = 2

# Liveness heartbeat — overwritten with the current unix timestamp on every poll
# tick. processes/health_service.py reads it to surface monitor liveness to PostHog.
# /workspace/logs (not /tmp) — /tmp is lost on every 25-min Firecracker VM cycle.
MONITOR_HEARTBEAT_FILE = Path("/workspace/logs/ninja_monitor_heartbeat")

# How often the monitor checks the gh session against the rotated mcp-token.
# Infra rotates /dev/shm/mcp-token, but gh's hosts.yml only gets a copy at
# orchestrator startup, so the gh session goes stale after each rotation.
GH_TOKEN_SYNC_INTERVAL = 5 * 60  # seconds


# ---------------------------------------------------------------------------
# Rate limit handler
# ---------------------------------------------------------------------------


class RateLimitHandler:
    """Handles exponential backoff for rate limiting."""

    def __init__(self):
        self.current_backoff = 0
        self.consecutive_rate_limits = 0
        self.last_rate_limit_time = 0

    def on_rate_limit(self):
        self.consecutive_rate_limits += 1
        self.last_rate_limit_time = time.time()
        if self.current_backoff == 0:
            self.current_backoff = BACKOFF_INITIAL
        else:
            self.current_backoff = min(
                self.current_backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX
            )
        print(
            f"⚠️ Rate limited! Backing off for {self.current_backoff}s "
            f"(attempt #{self.consecutive_rate_limits})",
            flush=True,
        )
        return self.current_backoff

    def on_success(self):
        if self.consecutive_rate_limits > 0:
            print(
                f"✅ Rate limit cleared after {self.consecutive_rate_limits} retries",
                flush=True,
            )
        self.current_backoff = 0
        self.consecutive_rate_limits = 0

    def is_backing_off(self) -> bool:
        if self.current_backoff == 0:
            return False
        return (time.time() - self.last_rate_limit_time) < self.current_backoff

    def get_remaining_backoff(self) -> float:
        if not self.is_backing_off():
            return 0
        return max(0, self.current_backoff - (time.time() - self.last_rate_limit_time))


rate_limiter = RateLimitHandler()


def monitor_shadow_mode(iface) -> bool:
    shadow_flag = getattr(iface, "shadow_flag", None)
    if not shadow_flag:
        return False
    return not is_feature_enabled(shadow_flag, default=False)


def should_post_welcome(iface) -> bool:
    shadow_flag = getattr(iface, "shadow_flag", None)
    if shadow_flag:
        return not monitor_shadow_mode(iface)
    return True


_WELCOME_AGENT_KEYS = ("welcomed", "welcomed_chat_jid")


def _agent_messages_for_save(agent_data: dict) -> dict:
    """Merge loop state onto disk without clobbering welcome keys from post_welcome."""
    disk = load_agent_messages()
    merged = {**disk, **agent_data}
    for key in _WELCOME_AGENT_KEYS:
        if key in disk:
            merged[key] = disk[key]
    return merged


def _maybe_advance_read_cursor(iface, pending_messages: list) -> None:
    """WhatsApp-only. Delete once `whatsappSqliteCache` is on everywhere."""
    advance = getattr(iface, "advance_read_cursor", None)
    if callable(advance):
        advance(pending_messages=pending_messages)


def _consume_shadow_batch(
    *,
    iface,
    pending_messages: list,
    seen_messages: set,
    agent_data: dict,
) -> None:
    keys = [m.id for m in pending_messages if m.id]
    logger.info("shadow[new]: would_dispatch batch=%s", keys)
    publish_all_handled(*iface.reporting_ids(), pending_messages)
    mark_handled(seen_messages, agent_data, pending_messages)
    _maybe_advance_read_cursor(iface, pending_messages)


def http_status_of(exc: BaseException) -> Optional[int]:
    """Extract the HTTP status code from an exception, or None.

    Used by the polling loop to distinguish 429 (rate limit — paced via
    RateLimitHandler) from 401/403 (authentication/authorization — logged and
    retried on the next poll, never treated as an empty channel) from any
    other read failure (network, 5xx, contract violation — also retried, but
    never confused with rate limiting).
    """
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code
    return None


# ---------------------------------------------------------------------------
# Orchestrator + heartbeat helpers
# ---------------------------------------------------------------------------


def write_monitor_heartbeat() -> None:
    """Overwrite MONITOR_HEARTBEAT_FILE with the current unix timestamp.

    Called on every poll tick so health_service.py can detect a stalled monitor.
    Best-effort — never raises.
    """
    try:
        MONITOR_HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        MONITOR_HEARTBEAT_FILE.write_text(str(int(time.time())))
    except OSError:
        pass


def _read_model_raw() -> str:
    """Uncached read of the selected model.

    ``get_selected_model()`` is ``@config_cached``, so it returns the startup value forever and would never see a switch.
    """
    try:
        return (
            json.loads(SANDBOX_METADATA_PATH.read_text()).get("litellm_selected_model")
            or ""
        ).strip()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""


def notify_model_change(iface, last_model: str) -> str:
    """Announce a model switch and returns the current model."""
    current = _read_model_raw()
    if not current or current == last_model:
        return last_model or current

    # Without this the notice would lie: get_selected_model() still holds the old value, so dispatch would keep routing to the previous harness.
    refresh_config("sandbox_metadata")
    logger.info(f"Model changed: {last_model or '(none)'} -> {current}")
    # The entrypoint decided this at boot; a switch has to redo it.
    try:
        apply_vision_skill(current)
    except OSError:
        logger.warning("vision-skill sync failed", exc_info=True)
    # An empty last_model means nothing was observed yet (metadata not re-provisioned
    # at startup after Stop/Resume): adopt the model, there is no switch to announce.
    if last_model:
        try:
            iface.say(f"\U0001f9e0 **Model changed:** `{last_model}` → `{current}`")
        except (RuntimeError, ValueError, OSError):
            logger.warning("model-change notice failed", exc_info=True)
    return current


def maybe_sync_github_token(last_sync_check: float) -> float:
    """Periodically resolve the GitHub token via the 3-tier flow.

    Runs at most every GH_TOKEN_SYNC_INTERVAL seconds. Uses
    ``resolve_github_token()`` which checks the cached gh session first,
    then /dev/shm/mcp-token, then the token proxy — propagating up on
    success.

    Skipped entirely for the local channel (canary) — no git repo / mcp-token.
    Returns the timestamp of this check (or last_sync_check if skipped).
    """
    now = time.time()
    if now - last_sync_check < GH_TOKEN_SYNC_INTERVAL:
        return last_sync_check

    if resolve_messaging_channel() == "local":
        return now

    try:
        result = resolve_github_token()
        if result["status"] == "ok":
            print("✅ gh session valid", flush=True)
        else:
            msg = result.get("message", result["status"])
            print(f"⚠️ GitHub token sync failed — {msg}", flush=True)
    except Exception as e:
        print(f"⚠️ GitHub token sync failed: {e}", file=sys.stderr)

    return now


def maybe_launch_orchestrator() -> bool:
    """Launch the orchestrator if there is open work and it isn't already running.

    Always launches via systemd (ninja.service). See agent-docs/LOOP.md.
    """
    if not is_orchestrator_enabled():
        print("Orchestrator disabled via config — skipping launch", flush=True)
        logger.info("Orchestrator disabled via config — skipping launch")
        return False

    if is_orchestrator_running():
        logger.info("Orchestrator already running — skipping launch")
        return False
    open_issues = count_open_issues()
    if open_issues <= 0:
        logger.info("No open issues — skipping orchestrator launch")
        return False

    logger.info(
        f"{open_issues} open issue(s) and orchestrator idle — launching",
    )
    try:
        result = subprocess.run(
            ["systemctl", "start", ORCHESTRATOR_SERVICE],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info(f"Started {ORCHESTRATOR_SERVICE} via systemd")
            return True
        logger.info(
            f"Systemctl start {ORCHESTRATOR_SERVICE} failed "
            f"({result.returncode}): {result.stderr.strip()}",
        )
        return False
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"⚠️ Could not launch {ORCHESTRATOR_SERVICE}: {e}")
        return False


def maybe_emit_heartbeat(
    agent_id: str, start_time: float, last_heartbeat: float
) -> float:
    """Emit a monitor-alive heartbeat metric at most once per minute.

    Also refreshes the /tmp liveness file on every call (cheap, not rate-limited)
    so health_service.py tracks the poll loop even between PostHog emissions.
    """
    # Refresh liveness file every tick regardless of rate-limit window
    write_monitor_heartbeat()

    now = time.time()
    if now - last_heartbeat < 60:
        return last_heartbeat
    capture("ninja monitor heartbeat", {"uptime_seconds": int(now - start_time)})
    print(f"\U0001f497 Emitted heartbeat for {agent_id}", flush=True)
    return now


# ---------------------------------------------------------------------------
# Shared polling monitor strategy
# ---------------------------------------------------------------------------


class PollingMonitorStrategy(MonitorStrategy):
    """Poll-based monitor loop shared by channels that use the same pattern.

    Channels like Slack and Teams use an identical poll -> collect -> dispatch
    loop.  This base class provides the full implementation so per-channel
    subclasses (``processes/slack/monitor_strategy.py``, etc.) only need to
    inherit -- no code duplication.

    Channels with a fundamentally different monitor (e.g. WhatsApp) implement
    ``MonitorStrategy`` directly instead of subclassing this.
    """

    def run(self) -> int:  # noqa: C901 — long but linear; splitting hurts readability
        # Wire SIGHUP -> refresh all config caches (config hot-reload without restart)
        logger.info("Starting monitor process...")
        install_sighup_handler()

        parser = argparse.ArgumentParser(
            description="Agent Monitor - Watch the messaging channel for mentions"
        )
        parser.add_argument(
            "--agent", "-a", help="Agent to run as (default: from config)"
        )
        parser.add_argument(
            "--interval",
            "-i",
            type=int,
            default=POLL_INTERVAL,
            help="Poll interval in seconds",
        )
        args = parser.parse_args()

        logger.info(f"Monitor starting with args: {args}")

        config = load_agent_config()
        agent_id = args.agent or config.get("default_agent", "").lower()

        logger.info(f"Resolved agent_id: {agent_id} (from args or config)")

        if not agent_id or agent_id not in AGENTS:
            print("❌ No valid agent configured!", file=sys.stderr)
            print(f"Available agents: {', '.join(AGENTS.keys())}", file=sys.stderr)
            print("Set 'default_agent' in ~/.agent_settings.json", file=sys.stderr)
            logger.info("No valid agent configured; exiting")
            return 1

        agent = AGENTS[agent_id]
        channel = resolve_messaging_channel()

        logger.info(
            f"Starting monitor for agent '{agent_id}' ({agent['name']}) on channel '{channel}'"
        )

        print(
            f"""
╔{'=' * 60}╗
║  {agent['emoji']} {agent['name']} Monitor - Watching for mentions
╠{'=' * 60}╣
║  Agent:    {agent['name']} ({agent['role']})
║  Channel:  {channel}
║  Polling:  Every {args.interval}s (+{POLL_JITTER}s jitter)
║  Runtime:  max {MAX_RUNTIME // 60} minutes
║  Mentions: {', '.join(agent['mentions'])}
╚{'=' * 60}╝
""",
            flush=True,
        )

        logger.info(
            f"Monitor config: interval={args.interval}s, max_runtime={MAX_RUNTIME}s, "
            f"mentions={agent['mentions']}"
        )

        iface = get_messaging_interface()
        seen_messages = load_seen_messages()
        agent_data = load_agent_messages()

        # Cold start (fresh install or a reclone that wiped local state): baseline the current channel history so we don't re-ack/re-answer the whole
        # backlog. Only messages arriving after startup are processed.
        # ``primed`` is set by the adapter that baselines, so a restart cannot
        # re-baseline messages that are queued but not yet answered.
        if not seen_messages and not agent_data.get("primed"):
            primed_states = EventCacheMessageStateBatch(*iface.reporting_ids())
            iface.prime_seen_state(seen_messages, agent_data, primed_states)
            save_seen_messages(seen_messages)
            save_agent_messages(_agent_messages_for_save(agent_data))
            # Tell the cache we consciously skipped the backlog, or every
            # baselined message stays pending and alerts (SN-3996).
            primed_states.publish()

        start_time = time.time()
        status_url = os.environ.get("AGENT_STATUS_URL", "")
        phantom_id = os.environ.get("PHANTOM_CHANNEL_ID", "")
        status_reporter = (
            AgentStatusReporter(phantom_id, status_url)
            if status_url and phantom_id
            else None
        )
        if status_reporter:
            status_reporter.start()
        last_heartbeat = 0.0
        last_gh_sync_check = 0.0
        # Teams only: seen-marks are written after a run actually answers, so an
        # unanswered batch is retried instead of being lost. The other adapters
        # still mark at collection time and keep their existing behavior.
        # TO DO : teams-specific read/seen/sent messages logic should go away after teams cache
        retry_unanswered = False
        # Failure count per dispatched batch, so a batch the worker cannot handle
        # is retried a bounded number of times instead of every poll forever.
        failed_batches: dict[frozenset, int] = {}

        # Pre-populate the heartbeat before post_welcome_if_needed so the
        # health service doesn't false-alarm on the first check after startup.
        write_monitor_heartbeat()

        # A previous process may have died mid-run (crash, redeploy, restart)
        # with a checklist still up. Clear it before the ticker starts, or it
        # sits in the channel saying "working on it" forever.
        end_live_progress(iface)
        ProgressService(iface).start()

        # No DEFAULT_MODEL fallback: on Stop/Resume the metadata may not be
        # re-provisioned yet, and a guessed value would be announced as a switch.
        last_model = _read_model_raw()
        try:
            apply_vision_skill(last_model)
        except OSError:
            logger.warning("vision-skill sync failed", exc_info=True)

        print(
            f"\U0001f4e1 Starting monitor loop (max {MAX_RUNTIME // 60} minutes)...",
            flush=True,
        )
        logger.info(f"Starting monitor loop for agent {agent_id} on channel {channel}")

        try:
            while True:
                last_heartbeat = maybe_emit_heartbeat(
                    agent_id, start_time, last_heartbeat
                )
                last_gh_sync_check = maybe_sync_github_token(last_gh_sync_check)
                last_model = notify_model_change(iface, last_model)

                if time.time() - start_time >= MAX_RUNTIME:
                    print(
                        f"\n⏰ Max runtime ({MAX_RUNTIME // 60} minutes) reached."
                        " Stopping.",
                        flush=True,
                    )
                    logger.info(
                        f"Max runtime ({MAX_RUNTIME // 60} minutes) reached for agent. Stopping."
                    )
                    break

                if rate_limiter.is_backing_off():
                    remaining = rate_limiter.get_remaining_backoff()
                    print(
                        f"⏳ Rate limit backoff: {remaining:.0f}s remaining...",
                        flush=True,
                    )
                    logger.info(f"Rate limit backoff: {remaining:.0f}s remaining.")
                    time.sleep(min(remaining, 30))
                    continue

                # --- collect messages ---
                try:
                    raw_messages = iface.get_history(limit=50)
                    rate_limiter.on_success()
                except Exception as e:
                    status = http_status_of(e)
                    if status == 429:
                        backoff_time = rate_limiter.on_rate_limit()
                        time.sleep(min(backoff_time, 30))
                    else:
                        if status == 401:
                            logger.error(
                                f"Authentication failed reading {channel} messages: {e}"
                            )
                        elif status == 403:
                            logger.error(
                                f"Not authorized to read {channel} channel: {e}"
                            )
                        else:
                            logger.error(f"Error reading {channel} messages: {e}")
                        print(
                            f"⚠️ Error reading messages: {e}",
                            file=sys.stderr,
                        )
                        # Paced, not tight: a config/auth failure that stays
                        # broken must not spin the loop at full speed, and one
                        # that clears (e.g. authorization restored) must be
                        # retried without a restart.
                        time.sleep(POLL_INTERVAL)
                    continue

                if should_post_welcome(iface):
                    iface.post_welcome_if_needed(
                        agent_data,
                        build_welcome_message(agent),
                        build_welcome_signature(agent),
                    )

                print(f"\U0001f4e8 Got {len(raw_messages)} messages", flush=True)
                logger.info(f"Got {len(raw_messages)} messages")

                pending_messages: list[PendingMessage] = []
                message_states = EventCacheMessageStateBatch(*iface.reporting_ids())
                shadow_mode = monitor_shadow_mode(iface)

                for msg in raw_messages:
                    collect_kwargs: dict = {}
                    if shadow_mode:
                        collect_kwargs["shadow_mode"] = True
                    pending_messages.extend(
                        iface.collect_pending(
                            msg,
                            agent.get("mentions", []),
                            seen_messages,
                            agent_data,
                            message_states,
                            **collect_kwargs,
                        )
                    )
                logger.info(f"Collected {len(pending_messages)} pending message(s)")

                # Before cron injection: synthetic cron ids were never in the
                # cache's pending table.
                message_states.publish()

                # --- inject due cron jobs ---
                for job in get_due_cron_messages(time.time()):
                    if claim_cron(job["id"]):
                        pending_messages.append(
                            PendingMessage(
                                id=f"cron:{job['id']}:{int(time.time())}",
                                thread_id=job.get("thread_ts") or "",
                                text=job["prompt"],
                                sender=Member(display_name="cron"),
                                type="cron",
                                cron_id=job["id"],
                            )
                        )
                        logger.info(f"Cron job '{job['id']}' is due — queued for batch")

                # --- dispatch ---
                if pending_messages:
                    capture(
                        "ninja batch processing started",
                        {"message_count": len(pending_messages)},
                    )
                    print(
                        f"\n\U0001f4cb Processing {len(pending_messages)}"
                        " pending message(s)...",
                        flush=True,
                    )
                    logger.info(
                        f"Processing {len(pending_messages)} pending message(s)"
                    )
                    blocked_msg = check_cost_limit()
                    if blocked_msg:
                        iface.say(blocked_msg)
                        print(
                            "🚫 Cost limit exceeded — dispatch blocked",
                            flush=True,
                        )
                        logger.warning("Cost limit exceeded — dispatch blocked")
                    elif shadow_mode:
                        _consume_shadow_batch(
                            iface=iface,
                            pending_messages=pending_messages,
                            seen_messages=seen_messages,
                            agent_data=agent_data,
                        )
                    elif retry_unanswered:
                        start_live_progress(iface, pending_messages)
                        if status_reporter:
                            status_reporter.set_working(True)
                        posted_before = load_sent_messages()
                        try:
                            dispatched = run_batched_response(
                                agent, pending_messages, iface.say
                            )
                        finally:
                            if status_reporter:
                                status_reporter.set_working(False)
                            end_live_progress(iface)
                        # A run counts as answered only if it actually posted.
                        # The exit status alone is not enough: a worker killed
                        # mid-task (sandbox suspended, /tmp wiped) can still look
                        # like a clean run.
                        answered = dispatched and bool(
                            load_sent_messages() - posted_before
                        )
                        # Cross-cycle safety net: a prior cycle may have already
                        # posted the reply, and this run correctly declined to
                        # duplicate it, so the same-cycle delta above is empty.
                        # Without this check the monitor keeps counting a
                        # successfully-answered batch as failing and eventually
                        # fires a false "couldn't finish it" notice. Only worth
                        # the extra thread fetch when the cheap check missed.
                        if not answered and iface.batch_already_answered(
                            pending_messages
                        ):
                            answered = True
                        batch_key = frozenset(m.id for m in pending_messages)
                        if answered:
                            failed_batches.pop(batch_key, None)
                            publish_all_handled(
                                *iface.reporting_ids(), pending_messages
                            )
                            mark_handled(seen_messages, agent_data, pending_messages)
                        else:
                            # Left unmarked, so the next poll re-collects and
                            # retries instead of dropping the question silently.
                            attempts = failed_batches[batch_key] = (
                                failed_batches.get(batch_key, 0) + 1
                            )
                            give_up = attempts >= MAX_BATCH_ATTEMPTS
                            if give_up:
                                # Say so before abandoning it: a silent drop is
                                # the failure this retry loop exists to prevent.
                                try:
                                    iface.say(
                                        "⚠️ I hit repeated errors working on your "
                                        "last message and couldn't finish it. "
                                        "Please send it again."
                                    )
                                except RuntimeError as e:
                                    # TeamsAPIError/TeamsConfigError — a lost
                                    # notice must not break the poll loop.
                                    logger.warning(f"Give-up notice failed: {e}")
                                publish_all_handled(
                                    *iface.reporting_ids(), pending_messages
                                )
                                mark_handled(
                                    seen_messages, agent_data, pending_messages
                                )
                            logger.error(
                                f"Batch of {len(pending_messages)} message(s) not"
                                f" answered (try {attempts}/{MAX_BATCH_ATTEMPTS}) —"
                                f" {'giving up' if give_up else 'retrying next poll'}"
                            )
                    else:
                        start_live_progress(iface, pending_messages)
                        if status_reporter:
                            status_reporter.set_working(True)
                        try:
                            run_batched_response(agent, pending_messages, iface.say)
                        finally:
                            if status_reporter:
                                status_reporter.set_working(False)
                            end_live_progress(iface)
                        _maybe_advance_read_cursor(iface, pending_messages)
                else:
                    blocked_msg = check_cost_limit()

                if not blocked_msg:
                    maybe_launch_orchestrator()

                save_seen_messages(seen_messages)
                save_agent_messages(_agent_messages_for_save(agent_data))

                jitter = random.uniform(0, POLL_JITTER)
                sleep_time = args.interval + jitter
                if rate_limiter.consecutive_rate_limits > 0:
                    sleep_time += BACKOFF_INITIAL / 2
                    logger.info(
                        f"Extended sleep due to recent rate limits: {sleep_time:.0f}s"
                    )
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("Monitor stopped via KeyboardInterrupt")
            save_seen_messages(seen_messages)
            save_agent_messages(_agent_messages_for_save(agent_data))

        return 0
