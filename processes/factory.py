"""
Process factory — picks channel-specific monitor and prompt strategies.

Lazy-imports the selected channel so unused strategy modules are never loaded.
Mirrors the ``messaging/factory.py`` pattern.

Usage:
    from processes.factory import get_monitor_strategy, get_prompt_strategy

    strategy = get_monitor_strategy()
    exit_code = strategy.run()

    prompt_vars = get_prompt_strategy().get_prompt_vars(config)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from messaging.factory import resolve_messaging_channel

if TYPE_CHECKING:
    from processes.base import MonitorStrategy, PromptStrategy


def get_monitor_strategy() -> "MonitorStrategy":
    """Return the MonitorStrategy for the active messaging channel.

    Raises:
        ValueError: If the resolved channel has no monitor strategy.
    """
    channel = resolve_messaging_channel()

    if channel == "slack":
        from processes.slack.monitor_strategy import SlackMonitorStrategy

        from infra.initial_setup_scripts.set_timezone import main as sync_timezone

        sync_timezone(["--quiet"])
        return SlackMonitorStrategy()

    if channel == "whatsapp":
        from processes.whatsapp.monitor_strategy import WhatsAppMonitorStrategy

        return WhatsAppMonitorStrategy()

    if channel == "teams":
        from processes.teams.monitor_strategy import TeamsMonitorStrategy

        return TeamsMonitorStrategy()

    if channel == "local":
        from processes.local.monitor_strategy import LocalMonitorStrategy

        return LocalMonitorStrategy()

    raise ValueError(f"No monitor strategy for channel: {channel!r}")


def get_prompt_strategy() -> "PromptStrategy":
    """Return the PromptStrategy for the active messaging channel.

    Raises:
        ValueError: If the resolved channel has no prompt strategy.
    """
    channel = resolve_messaging_channel()

    if channel == "slack":
        from processes.slack.prompt_strategy import SlackPromptStrategy

        return SlackPromptStrategy()

    if channel == "whatsapp":
        from processes.whatsapp.prompt_strategy import WhatsAppPromptStrategy

        return WhatsAppPromptStrategy()

    if channel == "teams":
        from processes.teams.prompt_strategy import TeamsPromptStrategy

        return TeamsPromptStrategy()

    if channel == "local":
        from processes.local.prompt_strategy import LocalPromptStrategy

        return LocalPromptStrategy()

    raise ValueError(f"No prompt strategy for channel: {channel!r}")
