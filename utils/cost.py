"""
Cost calculation for Claude token usage.

Margin is per-model (stored in pricing.py). Anthropic models apply a 2×
markup on top of base price; non-Anthropic models store the customer-facing
price directly with margin 0.0.
"""

import fcntl
import json
import os
import sys
import urllib.parse
import uuid
from datetime import datetime, timezone
from functools import cache
from pathlib import Path

import httpx
from clients.litellm_client import api_url, get_headers
from constants import (
    COST_LIMIT_PATH,
    HEADER_NINJA_CONVERSATION_ID,
    HEADER_NINJA_FEATURE,
    HEADER_NINJA_TASK_ID,
    LABEL_GENERATE_TASK_TITLE,
    MCP_TOKEN_PATH,
)
from core.config import load_agent_config
from core.logging import get_logger
from utils.pricing import get_pricing

logger = get_logger("cost")

TASK_LOG_FILE = Path("/workspace/ninja/.task_log.jsonl")

_TITLE_SYSTEM_PROMPT = "You are a helpful assistant that generates extremely concise titles (2-4 words maximum) for tasks based on the user's message. Respond with only the title, no other text or punctuation."
_TITLE_USER_PROMPT = "Generate an extremely brief title (2-4 words only) for a task that starts with this message:\n{prompt}"
# The start of the prompt is enough to name a task. Sending all of it (the
# orchestrator's prompt is huge) just makes this call time out.
_TITLE_PROMPT_LIMIT = 400
_MAX_FEATURE_LEN = 120


@cache
def _resolve_teams_channel_name(
    token: str, team_id: str, channel_id: str
) -> str | None:
    """Return the Teams channel displayName from Microsoft Graph. Returns None
    on any failure so the caller can fall back to the raw channel_id."""
    try:
        base = os.environ.get(
            "MICROSOFT_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0"
        ).rstrip("/")
        url = (
            f"{base}/teams/{urllib.parse.quote(team_id)}"
            f"/channels/{urllib.parse.quote(channel_id)}"
        )
        resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
        name = resp.json().get("displayName") if resp.status_code == 200 else None
        logger.info(
            f"teams-channel-name: graph status={resp.status_code} name={name!r}"
        )
        return str(name) if name else None
    except Exception as e:
        logger.warning(f"teams-channel-name: lookup failed: {e}")
        return None


def _teams_access_token() -> str | None:
    """The Graph token the Teams adapter actually uses, refreshed at boot."""
    try:
        with open(MCP_TOKEN_PATH, encoding="utf-8") as f:
            for line in f:
                if line.startswith("MSTeams="):
                    payload = json.loads(line[len("MSTeams=") :])
                    return payload.get("access_token") or None
    except (OSError, ValueError, AttributeError) as e:
        logger.info(f"teams-access-token: unreadable ({e})")
    return None


def _teams_channel_name_from_graph(tm: dict) -> str | None:
    """Look up the channel displayName if the teams config is complete."""
    token = _teams_access_token() or tm.get("access_token")
    team_id = tm.get("team_id")
    channel_id = tm.get("channel_id")
    if not (token and team_id and channel_id):
        logger.info(
            "teams-channel-name: skipped (missing "
            f"token={bool(token)} team_id={bool(team_id)} "
            f"channel_id={bool(channel_id)})"
        )
        return None
    name = _resolve_teams_channel_name(str(token), str(team_id), str(channel_id))
    if name is None:
        _resolve_teams_channel_name.cache_clear()
    return name


def active_messaging_channel(cfg: dict | None = None) -> str:
    """Active channel from MESSAGING_CHANNEL, else inferred from the adapter
    block in ``~/.agent_settings.json``. systemd units without a channel
    drop-in (ninja.service) see the env var unset, so the settings file is
    the fallback — mirroring ninja-upgrade.sh's resolve_channel().
    """
    active = (os.environ.get("MESSAGING_CHANNEL") or "").strip().lower()
    if active:
        return active
    cfg = cfg if cfg is not None else load_agent_config()
    if isinstance(cfg.get("teams"), dict):
        return "teams"
    if isinstance(cfg.get("whatsapp"), dict):
        return "whatsapp"
    return ""


def _feature_channel() -> str:
    """Channel label for the first part of the wallet feature ("<channel> - <title>").

    Slack writes the top-level ``default_channel`` key at install time and
    keeps using it unchanged. Teams stores its ids in the ``teams`` block of
    ``~/.agent_settings.json`` instead, so when MESSAGING_CHANNEL=teams the
    label is resolved from there. The trace log below records the active
    channel, label source and config keys so any other channel (e.g.
    whatsapp) can be diagnosed and added the same way.
    """
    cfg = load_agent_config()
    active = active_messaging_channel(cfg)
    if active == "teams":
        tm = cfg.get("teams") if isinstance(cfg.get("teams"), dict) else {}
        if tm.get("channel_name"):
            tm_label, source = tm["channel_name"], "teams.channel_name"
        elif name := _teams_channel_name_from_graph(tm):
            tm_label, source = name, "graph.displayName"
        else:
            tm_label, source = None, "(none)"
        label = f"teams-{tm_label}" if tm_label else ""
    elif active == "whatsapp":
        wa = cfg.get("whatsapp") if isinstance(cfg.get("whatsapp"), dict) else {}
        if wa.get("channel_label"):
            wa_label, source = wa["channel_label"], "whatsapp.channel_label"
        else:
            wa_label, source = None, "(none)"
        label = f"whatsapp-{wa_label}" if wa_label else ""
    else:
        label = cfg.get("default_channel", "")
        source = "default_channel" if label else "(none)"
    label = str(label).encode("ascii", errors="ignore").decode("ascii")
    label = " ".join(label.split())
    logger.info(
        f"feature-channel: active={active or '(unset)'} source={source} "
        f"label={label!r} env_channel={os.environ.get('MESSAGING_CHANNEL')!r} "
        f"cfg_keys={sorted(cfg.keys())}"
    )
    return label


def get_spend_stats() -> dict:
    """Read TASK_LOG_FILE once and return total, monthly, daily spend and task count."""
    now = datetime.now(timezone.utc)
    today = now.date()
    total = monthly = daily = 0.0
    count = 0
    if TASK_LOG_FILE.exists():
        with open(TASK_LOG_FILE, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cost = entry.get("cost", 0.0)
                total += cost
                count += 1
                created_at = entry.get("created_at")
                if not created_at:
                    continue
                try:
                    dt = datetime.fromisoformat(created_at).astimezone(timezone.utc)
                except ValueError:
                    continue
                if dt.year == now.year and dt.month == now.month:
                    monthly += cost
                if dt.date() == today:
                    daily += cost
    return {
        "total_spend": total,
        "monthly_spend": monthly,
        "daily_spend": daily,
        "task_count": count,
    }


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
    cache_read_tokens: int,
) -> float:
    """Return the customer-facing total cost in USD after applying the gateway margin."""
    return sum(
        compute_cost_breakdown(
            model,
            input_tokens,
            output_tokens,
            cache_write_5m_tokens,
            cache_write_1h_tokens,
            cache_read_tokens,
        ).values()
    )


def compute_cost_breakdown(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
    cache_read_tokens: int,
) -> dict:
    """Return per-category customer-facing costs in USD after applying the gateway margin."""
    pricing = get_pricing(model)
    m = 1 + pricing.get("margin", 1.0)
    return {
        "input": (input_tokens / 1_000_000) * pricing["input"] * m,
        "output": (output_tokens / 1_000_000) * pricing["output"] * m,
        "cache_write_5m": (cache_write_5m_tokens / 1_000_000)
        * pricing["cache_write_5m"]
        * m,
        "cache_write_1h": (cache_write_1h_tokens / 1_000_000)
        * pricing["cache_write_1h"]
        * m,
        "cache_read": (cache_read_tokens / 1_000_000) * pricing["cache_read"] * m,
    }


def _write_task_log(
    prompt_uuid: str,
    cost: float,
    texts: list[str],
    title: str,
    model: str = "",
    task_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Write a task log entry to TASK_LOG_FILE."""
    try:
        entry: dict = {
            "id": prompt_uuid,
            "texts": texts,
            "cost": cost,
            "title": title,
            "model": model,
            "ninja_task_id": task_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(TASK_LOG_FILE, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(entry) + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
        print(
            f"📝 Task log written: uuid={prompt_uuid} title={title!r} cost=${cost:.6f}",
            flush=True,
        )
    except Exception as e:
        print(f"⚠️ Could not write task log: {e}", file=sys.stderr)


def record_tool_call_cost(
    response_headers: dict,
    prompt: str,
    model: str,
) -> None:
    """Write a task log entry for a direct LiteLLM tool call (image/video/audio gen).

    Reads cost from the ``x-litellm-response-cost`` response header and task
    metadata (task_id, title, conversation_id) from ``ANTHROPIC_CUSTOM_HEADERS``
    in the environment — the same env var the parent Claude session injects.
    """
    try:
        cost_str = response_headers.get("x-litellm-response-cost", "")
        if not cost_str:
            return
        cost = float(cost_str)

        raw = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
        custom: dict[str, str] = {}
        for line in raw.strip().splitlines():
            if ": " in line:
                key, _, val = line.partition(": ")
                custom[key.strip()] = val.strip()

        task_id = custom.get(HEADER_NINJA_TASK_ID)
        if not task_id:
            return

        feature = custom.get(HEADER_NINJA_FEATURE, "")
        channel = _feature_channel()
        prefix = f"{channel} - "
        title = (
            feature[len(prefix) :]
            if channel and feature.startswith(prefix)
            else feature
        )
        title = title or prompt[:50]
        logger.info(
            f"record_tool_call_cost: feature={feature!r} title={title!r} cost={cost}"
        )
        conversation_id = custom.get(HEADER_NINJA_CONVERSATION_ID)

        _write_task_log(
            str(uuid.uuid4()),
            cost,
            [prompt],
            title,
            model=model,
            task_id=task_id,
            conversation_id=conversation_id,
        )
    except Exception as e:
        print(f"⚠️ Could not record tool call cost: {e}", file=sys.stderr)


def record_task_cost(
    texts: list[str],
    title: str,
    cost: float,
    model: str = "",
    task_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Write a task-log entry with cost data from ``--output-format json``.

    Callers obtain *cost* from ``ClaudeResult.total_cost_usd`` and *model*
    from ``primary_model(parsed)`` — no JSONL transcript scanning needed.
    """
    try:
        logger.info(
            f"record_task_cost: task_id={task_id} title={title!r} "
            f"cost={cost} model={model}"
        )
        _write_task_log(
            str(uuid.uuid4()),
            cost,
            texts,
            title,
            model=model,
            task_id=task_id,
            conversation_id=conversation_id,
        )
    except Exception as e:
        print(f"⚠️ Could not record task cost: {e}", file=sys.stderr)


def _header_safe(value: str) -> str:
    """Flatten a string so it is safe as an HTTP header value.
    When title generation fails we fall back to raw prompt text, which has
    line breaks. Claude Code reads our headers one per line, so a single
    newline in here makes it reject the whole request.
    """
    value = "".join(" " if c in "\r\n\t" or ord(c) < 32 else c for c in value)
    return " ".join(value.split())[:_MAX_FEATURE_LEN]


def build_feature(title: str):
    channel = _feature_channel()
    feature = f"{channel} - {title}" if channel else title
    feature = feature.encode("ascii", errors="ignore").decode("ascii")
    feature = _header_safe(feature)
    logger.info(f"build_feature: title={title!r} -> feature={feature!r}")
    return feature


def build_custom_headers(
    task_id: str, title: str, conversation_id: str | None = None
) -> str:
    """
    Build custom headers to track costs by thread_id and task_id
    """
    feature = build_feature(title)
    headers = f"{HEADER_NINJA_TASK_ID}: {task_id}\n{HEADER_NINJA_FEATURE}: {feature}"
    if conversation_id:
        headers += f"\n{HEADER_NINJA_CONVERSATION_ID}: {conversation_id}"
    logger.info(f"build_custom_headers: task_id={task_id} feature={feature!r}")
    return headers


def generate_task_title(
    prompt: str,
    task_id: str | None = None,
    conversation_id: str | None = None,
) -> str | None:
    """
    Generate a concise task title (2-4 words) to show in the SuperNinja usage dashboard.
    """
    try:
        extra_headers = {}
        channel = _feature_channel()
        if task_id:
            extra_headers[HEADER_NINJA_TASK_ID] = task_id
        if conversation_id:
            extra_headers[HEADER_NINJA_CONVERSATION_ID] = conversation_id
        feature = (
            f"{channel} - {LABEL_GENERATE_TASK_TITLE}"
            if channel
            else LABEL_GENERATE_TASK_TITLE
        )
        extra_headers[HEADER_NINJA_FEATURE] = feature.encode(
            "ascii", errors="ignore"
        ).decode("ascii")
        logger.info(
            f"generate_task_title: feature={extra_headers[HEADER_NINJA_FEATURE]!r}"
        )

        resp = httpx.post(
            api_url("/v1/chat/completions"),
            headers=get_headers(extra_headers),
            json={
                "model": "claude-haiku-4-5-20251001",
                "messages": [
                    {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _TITLE_USER_PROMPT.format(
                            prompt=prompt[:_TITLE_PROMPT_LIMIT]
                        ),
                    },
                ],
                "max_tokens": 20,
                "temperature": 0.7,
            },
            timeout=10.0,
        )
        title = (
            resp.json()["choices"][0]["message"]["content"].strip().strip("'\" \n\t")
        )
        return title or None
    except Exception as e:
        print(f"⚠️ Could not generate task title: {e}", file=sys.stderr)
        return None


def check_cost_limit() -> str | None:
    """Return a block message if any cost limit from config is exceeded, otherwise None."""
    try:
        limits = (
            json.loads(COST_LIMIT_PATH.read_text(encoding="utf-8"))
            if COST_LIMIT_PATH.exists()
            else {}
        )
    except Exception:
        limits = {}
    if not limits:
        return None

    stats = get_spend_stats()

    if "total" in limits:
        limit = float(limits["total"])
        spent = stats["total_spend"]
        if spent >= limit:
            return (
                f"\U0001f6ab **Total cost limit reached**\n"
                f"You've spent ${spent:.2f} of your ${limit:.2f} total limit.\n"
                "Your task has been paused. You can increase your limit to resume."
            )

    if "monthly" in limits:
        monthly_limit = float(limits["monthly"])
        monthly_spent = stats["monthly_spend"]
        if monthly_spent >= monthly_limit:
            return (
                f"\U0001f6ab **Monthly cost limit reached**\n"
                f"You've spent ${monthly_spent:.2f} of your ${monthly_limit:.2f} monthly limit.\n"
                "Your task has been paused. The limit resets next month."
            )

    if "daily" in limits:
        daily_limit = float(limits["daily"])
        daily_spent = stats["daily_spend"]
        if daily_spent >= daily_limit:
            return (
                f"\U0001f6ab **Daily cost limit reached**\n"
                f"You've spent ${daily_spent:.2f} of your ${daily_limit:.2f} daily limit.\n"
                "Your task has been paused. The limit resets tomorrow."
            )

    return None
