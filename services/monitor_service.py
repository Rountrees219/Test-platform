"""
services.monitor_service — business logic for the monitor process.

This module contains everything the monitor *does* (prompt building,
Claude dispatch, the out-of-credits notification, welcome copy) separated
from everything it *is* (poll loop, rate-limit backoff, SIGHUP wiring).
Classifying a run as out of credits lives in
``agent_providers.credit_errors`` — the providers need it too.

``processes/monitor.py`` imports from here; nothing else should need to.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent_providers.base import AgentRunConfig
from agent_providers.claude.claude_output import primary_model
from agent_providers.claude.claude_provider import ClaudeProvider, detect_api_error
from agent_providers.codex.codex_provider import CodexProvider, parse_codex_output
from agent_providers.credit_errors import result_out_of_credits
from clients.posthog_client import capture, is_feature_enabled
from clients.super_ninja_client import get_super_ninja_url, get_thread_id
from constants import (
    AI_DISCLOSURE_BLOCK,
    CLAUDE_RUN_MONITOR_TIMEOUT_SECONDS,
    CODEX_HARNESS_MODEL,
    CODEX_RUN_MONITOR_TIMEOUT_SECONDS,
    DEFAULT_TASK_TITLE,
    LIVE_PROGRESS_FEATURE_FLAG,
    LIVE_PROGRESS_TEAMS_FEATURE_FLAG,
    LIVE_PROGRESS_WHATSAPP_FEATURE_FLAG,
    MONITOR_SERVICE_NAME,
    SYSTEM_PROMPT_CODEX_PATH,
    SYSTEM_PROMPT_PATH,
    SYSTEM_PROMPT_PATH_SINGLE,
    SYSTEM_PROMPT_SINGLE_CODEX_PATH,
)
from core import live_progress
from core.config import is_orchestrator_enabled
from core.logging import get_logger
from core.metadata import get_selected_model
from messaging.message_utils import FORCE_THREAD_ENV, forced_thread_for_batch
from messaging.pending import PendingMessage
from utils.cost import (
    build_custom_headers,
    generate_task_title,
    record_task_cost,
)
from utils.system_notification import get_disk_warning

logger = get_logger(MONITOR_SERVICE_NAME)

# REPO_ROOT is the src/ninja/ package root — used to locate claude-wrapper.sh
_REPO_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Credit exhaustion
# ---------------------------------------------------------------------------


class RunOutOfCreditsException(Exception):
    """Raised when the agent's output signals the account has no remaining credits."""


# ---------------------------------------------------------------------------
# Welcome message
# ---------------------------------------------------------------------------


# Distinctive opening phrase used as an idempotency anchor by the adapter's
# post_welcome_if_needed() implementation. Must match the first user-visible
# sentence of build_welcome_message(). Do not change without updating the
# adapter and the test suite.
def build_welcome_signature(agent) -> str:
    name = agent.get("name", "Ninja")
    role = agent.get("role", "Browser Automation Agent")
    return f"Hi, I'm {name} \u2014 your {role}."


def build_welcome_message(agent: dict) -> str:
    """Build the first-run welcome announcement text (channel-agnostic Markdown).

    The first user-visible sentence MUST contain ``WELCOME_SIGNATURE`` — it
    doubles as an invisible idempotency anchor when reading back channel history.
    """
    welcome_signature = build_welcome_signature(agent)
    emoji = agent.get("emoji", "\U0001f47b")
    return (
        f"{emoji} **{welcome_signature}**\n"
        "Think of me as a virtual employee on your team. Brief me in "
        "any language \u2014 by message, voice note, or file \u2014 and I'll "
        "get the work done. No clicking, no copy-pasting, no API keys "
        "for you to manage.\n"
        "\n"
        "**\U0001f4bc What you can ask me to do**\n"
        '- **Research & reports** \u2014 "Pull the top 10 competitors for '
        'X, summarise their pricing, give me a one-pager."\n'
        '- **Lead gen & outreach** \u2014 "Find 50 founders of seed-stage '
        'fintechs in NYC and draft a personalised intro email to each."\n'
        '- **Recruiting** \u2014 "Source 20 senior backend engineers from '
        'LinkedIn matching this JD and message them on my behalf."\n'
        '- **Operations & data entry** \u2014 "Update these 30 Salesforce '
        'records from this spreadsheet," or "file my expense reports '
        'from these receipts."\n'
        '- **Travel & bookings** \u2014 "Book the cheapest direct flight '
        'from SFO to JFK next Friday and add it to my calendar."\n'
        '- **Creative work** \u2014 "Generate a flat-style logo for a '
        'coffee app called Brewly," or edit a photo, design a banner, '
        "or mock up a landing page hero image.\n"
        "- **Reports posted right here** \u2014 I deliver results back to "
        "this channel as a message, file, image, or thread you can act on.\n"
        "\n"
        "**\U0001f9f0 How I get things done \u2014 two complementary tools**\n"
        "- \U0001f30d **Browser** \u2014 I drive a real Chromium browser the "
        "way a human would: navigate any website, fill forms, click "
        "through flows, scrape data, log in to dashboards, download "
        "files. *Best for:* anything without an API \u2014 internal admin "
        "tools, niche SaaS, web search, news, social profiles, "
        "research across many sites.\n"
        "- \U0001f50c **Integrations (3,000+ apps)** \u2014 direct, "
        "authenticated access to Slack, Gmail, Google Calendar, "
        "GitHub, Jira, Linear, Notion, Salesforce, HubSpot, Stripe, "
        "Airtable, Asana, LinkedIn, AWS, and ~3,000 more. "
        "*Best for:* anything with a stable API \u2014 fast, reliable, "
        "rate-limit-friendly, and works in the background even while "
        "you're offline.\n"
        "I pick the right tool for each step automatically; you "
        "don't have to choose.\n"
        "\n"
        "**\U0001f4ac How to brief me**\n"
        "- Just type a message in this channel \u2014 I reply to every "
        "human message.\n"
        "- Reply in a thread or post in the channel to ping me.\n"
        "- Send a *voice note* in any language and I'll transcribe and "
        "act on it.\n"
        "- Drop a *screenshot, PDF, spreadsheet, or any file* with "
        "your request and I'll use it as context.\n"
        "\n"
        "**\U0001f441 Watch me work**\n"
        "- [**Live Browser**](0.0.0.0:6080/vnc.html?autoconnect=true) "
        "\u2014 watch my Chromium session in real time. Take over with "
        "mouse/keyboard if I get stuck.\n"
        "- [**Activity Dashboard**](0.0.0.0:9000) \u2014 live identity, "
        "logs, reasoning trace, and per-task cost.\n"
        "- [**Connect Apps**](0.0.0.0:9020) \u2014 connect new apps "
        "(one-click OAuth) so I can use them. Anything you connect "
        "here becomes a tool I can call.\n"
        "\n"
        f"{AI_DISCLOSURE_BLOCK}\n"
        "\n"
        f"\U0001f9e0 **Model:** {get_selected_model()}"
    )


def _resolve_batch_prompt(agent: dict, pending_messages: list) -> str:
    from processes.factory import get_prompt_strategy

    strategy = get_prompt_strategy()
    builder = getattr(strategy, "build_batch_prompt", None)
    if callable(builder):
        return builder(agent=agent, pending_messages=pending_messages)
    return build_batch_prompt(agent, pending_messages)


def _resolve_system_prompt() -> tuple[Path, str]:
    from processes.factory import get_prompt_strategy

    strategy = get_prompt_strategy()
    resolver = getattr(strategy, "resolve_system_prompt", None)
    if callable(resolver):
        path = resolver()
        if path:
            return path, "append"

    system_prompt_path = (
        SYSTEM_PROMPT_PATH if is_orchestrator_enabled() else SYSTEM_PROMPT_PATH_SINGLE
    )
    return system_prompt_path, "replace"


def build_batch_prompt(
    agent: dict,
    pending_messages: list[PendingMessage],
) -> str:
    """Assemble the single batched prompt for a set of pending messages.

    Shared by the Claude and Codex monitor runners so the copy stays in one
    place. (Extracted verbatim from the original inline body of
    run_batched_response.)
    """
    agent_name = agent["name"]
    agent_role = agent["role"]
    agent_emoji = agent["emoji"]

    messages_text = ""
    for i, msg in enumerate(pending_messages, 1):
        msg_type = msg.type or "mention"

        if msg_type == "cron":
            reply_hint = say_cmd(msg.thread_id or None)
            messages_text += (
                f"\n--- Message {i} (cron — scheduled job) ---\n"
                f"Cron ID: {msg.cron_id or 'unknown'}\n"
                f"Prompt: {msg.text}\n"
            )
            continue

        thread_info = (
            f"\n   Thread: {msg.thread_id}" if msg.thread_id else "\n   Channel: main"
        )

        attachment_info = ""
        if msg.audio_files:
            attachment_info += (
                "\n   🎤 AUDIO/VOICE MESSAGE — Must transcribe before responding!"
            )
            for af in msg.audio_files:
                attachment_info += (
                    f"\n   Audio file: {af.name or 'audio'} "
                    f"({af.mimetype or 'audio/*'})"
                    f"\n   Event ID: {af.event_id or msg.event_id or 'N/A'}"
                    f"\n   File ID: {af.id or 'N/A'}"
                )
        if msg.image_files:
            attachment_info += "\n   🖼️  IMAGE ATTACHMENT(S):"
            for img in msg.image_files:
                size_kb = img.size // 1024
                attachment_info += (
                    f"\n   Image: {img.name or 'image'} "
                    f"({img.mimetype or 'image/*'}, {size_kb} KB)"
                    f"\n   Event ID: {img.event_id or msg.event_id or 'N/A'}"
                    f"\n   File ID: {img.id or 'N/A'}"
                )
        if msg.pdf_files:
            attachment_info += "\n   📄 PDF ATTACHMENT(S):"
            for pdf in msg.pdf_files:
                size_kb = pdf.size // 1024
                attachment_info += (
                    f"\n   PDF: {pdf.name or 'file.pdf'} ({size_kb} KB)"
                    f"\n   Event ID: {pdf.event_id or msg.event_id or 'N/A'}"
                    f"\n   File ID: {pdf.id or 'N/A'}"
                )
        if msg.other_files:
            attachment_info += "\n   📎 OTHER FILE ATTACHMENT(S):"
            for of in msg.other_files:
                size_kb = of.size // 1024
                attachment_info += (
                    f"\n   File: {of.name or 'file'} "
                    f"({of.mimetype or 'application/octet-stream'}, {size_kb} KB)"
                    f"\n   Event ID: {of.event_id or msg.event_id or 'N/A'}"
                    f"\n   File ID: {of.id or 'N/A'}"
                )

        messages_text += (
            f"\n--- Message {i} ({msg_type}) ---\n"
            f"From: {msg.sender.display_name}\n"
            f"Time: {msg.id or 'Unknown'}\n"
            f"Text: {msg.text}{attachment_info}{thread_info}\n"
        )

    return messages_text


# ---------------------------------------------------------------------------
# Live progress message
# ---------------------------------------------------------------------------


def start_live_progress(iface, pending_messages: list[PendingMessage]) -> bool:
    """Arm live progress for the run about to start.

    Nothing is posted here — the thread posts later, only if the run turns out
    to be slow.

    The whole body is guarded on purpose. This runs before the dispatch and
    outside the caller's try, and the poll loop catches nothing, so anything
    escaping here would cost the user their answer *and* skip the orchestrator
    launch that follows.
    """
    try:
        if not is_feature_enabled(LIVE_PROGRESS_FEATURE_FLAG, default=False):
            return False
        if not getattr(iface, "supports_activity_progress", False):
            return False
        # A second gate, not a capability check: whether we want Teams and
        # WhatsApp to.
        provider = getattr(iface, "activity_provider", None)
        if provider == "teams" and not is_feature_enabled(
            LIVE_PROGRESS_TEAMS_FEATURE_FLAG, default=False
        ):
            return False
        if provider == "whatsapp" and not is_feature_enabled(
            LIVE_PROGRESS_WHATSAPP_FEATURE_FLAG, default=False
        ):
            return False
        # Claude only for now — the Codex hook records no steps, so the
        # checklist would stay empty all run.
        if get_selected_model() in CODEX_HARNESS_MODEL:
            return False
        # Nobody is waiting on a cron job, so those get nothing.
        thread_ts = next(
            (m.thread_id or m.id for m in pending_messages if m.type != "cron"),
            None,
        )
        if not thread_ts:
            return False

        end_live_progress(iface)

        # An id, not a display name — a name never matches the stored activity.
        channel_id = iface.reporting_ids()[1]

        live_progress.arm(
            channel=channel_id,
            thread_ts=thread_ts,
            provider=provider,
            workspace_id=iface.activity_workspace_id(),
        )
        return True
    except Exception as e:
        logger.warning(f"Live progress could not start: {e}")
        return False


def end_live_progress(iface) -> None:
    """Complete the activity once the run is over.

    Guarded whole for the same reason as ``start_live_progress``: the caller
    runs this in a ``finally``, so an error here would replace the outcome of a
    run that already answered.
    """
    try:
        state = live_progress.read_state()
        if not state:
            return
        try:
            if state.get("activity_started"):
                thread_id = state.get("thread_ts")
                if thread_id:
                    iface.complete_activity(thread_id)
        finally:
            # Forget it either way, or the next run inherits stale state.
            live_progress.clear_state()
    except Exception as e:
        logger.warning(f"Live progress cleanup failed: {e}")


# ---------------------------------------------------------------------------
# Prompt / CLI command helpers
# ---------------------------------------------------------------------------


def say_cmd(thread_ts: Optional[str] = None) -> str:
    """Return the channel-appropriate CLI say command for Claude's prompt.

    Reads ``MESSAGING_CHANNEL`` env-var (default: ``slack``) so the command
    embedded in Claude's prompt always matches the active adapter.
    """
    channel = os.environ.get("MESSAGING_CHANNEL", "slack")
    base = f"python messaging/{channel}/interface.py say"
    if thread_ts:
        return f'{base} "message" -t {thread_ts}'
    return f'{base} "message"'


# ---------------------------------------------------------------------------
# Claude batch dispatch
# ---------------------------------------------------------------------------


def run_batched_response(
    agent: dict,
    pending_messages: list[PendingMessage],
    messaging_say_fn,
) -> bool:
    """Send all pending messages to Claude in a single prompt.

    Args:
        agent:            Agent configuration dict.
        pending_messages: List of PendingMessage (user, text, timestamp,
                          thread_ts, type, audio_files).
        messaging_say_fn: Callable used to post the out-of-credits notification
                          (typically ``_get_messaging().say``). Passed in to
                          avoid a circular import with monitor.py.

    Returns:
        True if Claude successfully processed the messages.
    """
    if not pending_messages:
        return True

    model = get_selected_model()
    codex_harness_enabled = model in CODEX_HARNESS_MODEL
    logger.info(
        f"Selected model: {model}, Codex harness enabled: {codex_harness_enabled}"
    )

    prompt = _resolve_batch_prompt(agent, pending_messages)

    disk_warning = get_disk_warning()
    if disk_warning:
        prompt += f"\n\n{disk_warning}"

    logger.info(f"Sending {len(pending_messages)} message(s) to Agent...")

    texts = [msg.text for msg in pending_messages if msg.text]
    started_at = datetime.now(timezone.utc).timestamp()
    task_id = str(uuid.uuid4())
    conversation_id = get_thread_id()

    combined = " | ".join(texts) if texts else ""
    title = (
        generate_task_title(combined, task_id=task_id, conversation_id=conversation_id)
        if combined
        else None
    )
    if not title:
        title = (
            (combined[:50] + "…")
            if len(combined) > 50
            else combined or DEFAULT_TASK_TITLE
        )

    custom_headers = build_custom_headers(task_id, title, conversation_id)

    # Single-message batches get deterministic reply routing: export the
    # thread_ts so the say/upload CLI enforces it if the model drops -t.
    subprocess_env = {**os.environ, "ANTHROPIC_CUSTOM_HEADERS": custom_headers}
    forced_thread = forced_thread_for_batch(pending_messages)
    if forced_thread:
        subprocess_env[FORCE_THREAD_ENV] = forced_thread
    else:
        subprocess_env.pop(FORCE_THREAD_ENV, None)

    try:
        model = get_selected_model()
        if codex_harness_enabled:
            logger.info(
                f"Selected model {model} is a Codex harness model, using Codex for batch response"
            )
            result, duration = get_codex_response(
                subprocess_env,
                prompt,
                title,
                task_id=task_id,
            )
        else:
            logger.info(f"Selected model is {model}, using Claude for batch response")
            sp_path, sp_mode = _resolve_system_prompt()
            result, duration = get_claude_response(
                subprocess_env,
                prompt,
                task_id=task_id,
                system_prompt_path=sp_path,
                system_prompt_mode=sp_mode,
            )
            # Detect API errors from structured JSON output + stderr
            # (replaces the old JSONL transcript scan).
            parsed = result.parsed
            if parsed and parsed.is_error:
                logger.error(
                    f"Detected API error in Claude result: subtype={parsed.subtype}"
                )
                capture(
                    "claude_api_error",
                    {
                        "process": "monitor",
                        "task_id": task_id,
                        "conversation_id": conversation_id,
                    },
                )
            api_err = detect_api_error(result)
            if api_err:
                logger.error(f"Detected API error from stderr: {api_err}")
                capture(
                    "claude_api_error",
                    {
                        "process": "monitor",
                        "task_id": task_id,
                        "conversation_id": conversation_id,
                    },
                )
        capture(
            "agent_provider_run_duration",
            {
                "process": "monitor",
                "provider": "codex" if codex_harness_enabled else "claude",
                "duration_seconds": duration,
            },
        )
        # Extract text from JSON output for pattern matching.
        # For Codex, result.stdout is plain text; for Claude it's JSON.
        if not codex_harness_enabled and parsed and not parsed.is_error:
            text_output = parsed.text or ""
        else:
            text_output = result.stdout or ""
        output = text_output + (result.stderr or "")
        success_count = (
            output.count("Message sent")
            + output.count("✅")
            + output.count("Timestamp:")
        )

        # Record cost from JSON result (no JSONL scanning).
        if (
            not codex_harness_enabled
            and parsed
            and not parsed.is_error
            and parsed.total_cost_usd > 0
        ):
            threading.Thread(
                target=record_task_cost,
                args=(texts, title, parsed.total_cost_usd),
                kwargs={
                    "model": primary_model(parsed),
                    "task_id": task_id,
                    "conversation_id": conversation_id,
                },
                daemon=True,
            ).start()
        else:
            codex_result = (
                parse_codex_output(result.stdout) if codex_harness_enabled else None
            )
            codex_model = model if codex_harness_enabled else ""
            codex_cost = codex_result.compute_cost(codex_model) if codex_result else 0.0
            threading.Thread(
                target=record_task_cost,
                args=(texts, title, codex_cost),
                kwargs={
                    "model": codex_model,
                    "task_id": task_id,
                    "conversation_id": conversation_id,
                },
                daemon=True,
            ).start()

        # Every stream, not just stderr: Claude reports a mid-run 400/402 in its JSON payload on stdout
        # Codex reports it as a stdout JSONL event, so an stderr-only check leaves the user with silence.
        if result_out_of_credits(result):
            logger.warning("Agent provider run failed due to insufficient credits")
            raise RunOutOfCreditsException

        capture(
            "Agent provider batch response completed",
            {"success": True, "response_count": success_count},
        )
        if success_count > 0:
            logger.info(
                f"Agent provider processed batch — {success_count} response indicator(s)"
            )
        else:
            logger.warning(
                f"Agent provider batch response (may have posted): {output[:300]}...",
            )
        return True

    except RunOutOfCreditsException:
        try:
            logger.warning("Handling insufficient credit notification for user")
            top_up_url = f"{get_super_ninja_url()}/dashboard#buy-credits"
            messaging_say_fn(
                f"\U0001f4b3 **You're out of credits**\n"
                f"Your current task has been paused.\n"
                f"Add credits to pick up where you left off — your work is saved.\n\n"
                f"[**Add credits \u2192**]({top_up_url})"
            )
            print("\U0001f4b3 Posted insufficient credit notification", flush=True)
            logger.info("Posted insufficient credit notification")
        except Exception as e:
            logger.warning(f"Credit notification skipped: {e}")
        capture(
            "ninja batch response failed because of the insufficient credits",
            {"success": False, "error": "insufficient credit"},
        )
        return False
    except subprocess.TimeoutExpired:
        capture(
            "ninja batch response completed", {"success": False, "error": "timeout"}
        )
        logger.exception("Claude batch response timed out")
        return False
    except OSError as e:
        logger.exception(f"OS error running Claude: {e}")
        return False
    except Exception as e:
        capture("ninja batch response completed", {"success": False, "error": str(e)})
        logger.exception(f"Error: {e}")
        return False


def get_claude_response(
    env: dict,
    prompt: str,
    task_id: str,
    *,
    system_prompt_path: Path | None = None,
    system_prompt_mode: str | None = None,
):
    # Arm the length hook (PostToolUse) for the monitor run; it no-ops
    # otherwise. Clear the shared state so a previous run can't leak its
    # "stopped_by_length" flag into this one.
    env = {**env, "NINJA_MONITOR_RUN": "1"}
    try:
        Path("/workspace/ninja/stop_hook_length_state.json").unlink()
    except OSError:
        pass

    if system_prompt_path is None:
        system_prompt_path = (
            SYSTEM_PROMPT_PATH
            if is_orchestrator_enabled()
            else SYSTEM_PROMPT_PATH_SINGLE
        )
        system_prompt_mode = system_prompt_mode or "replace"
    else:
        system_prompt_mode = system_prompt_mode or "append"

    spec = AgentRunConfig(
        prompt=prompt,
        system_prompt_path=system_prompt_path,
        system_prompt_mode=system_prompt_mode,
        tools=["Bash", "Edit", "Read", "Skill", "Write"],
        timeout_seconds=CLAUDE_RUN_MONITOR_TIMEOUT_SECONDS,
        session_name="monitor",
        cwd=_REPO_ROOT,
        env=env,
        process_label="monitor",
        task_id=task_id,
    )

    provider = ClaudeProvider()
    run_started = time.monotonic()
    result = provider.run(spec, logger)
    return result, round(time.monotonic() - run_started, 2)


def get_codex_response(
    env: dict,
    prompt: str,
    title: str,
    task_id: str,
):

    # Arm the length hook (PostToolUse) for the monitor run; it no-ops
    # otherwise. Clear the shared state so a previous run can't leak its
    # "stopped_by_length" flag into this one.
    env = {**env, "NINJA_MONITOR_RUN": "1"}
    try:
        Path("/workspace/ninja/stop_hook_length_state.json").unlink()
    except OSError:
        pass

    system_prompt_path = (
        SYSTEM_PROMPT_CODEX_PATH
        if is_orchestrator_enabled()
        else SYSTEM_PROMPT_SINGLE_CODEX_PATH
    )

    spec = AgentRunConfig(
        prompt=prompt,
        system_prompt_path=system_prompt_path,
        tools=["Bash", "Edit", "Read", "Write"],
        timeout_seconds=CODEX_RUN_MONITOR_TIMEOUT_SECONDS,
        session_name="monitor",
        cwd=_REPO_ROOT,
        env=env,
        process_label="monitor",
        title=title,
        task_id=task_id,
    )

    provider = CodexProvider()
    provider.setup(logger)

    run_started = time.monotonic()
    result = provider.run(spec, logger)
    return result, round(time.monotonic() - run_started, 2)
