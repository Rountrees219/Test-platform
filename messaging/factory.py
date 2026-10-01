"""
Messaging factory — picks one channel adapter from config.

Lazy-imports the selected channel so unused adapters are never loaded.
The active channel is resolved from the MESSAGING_CHANNEL environment
variable (default: "slack").

Usage:
    from messaging.factory import get_messaging_interface

    iface = get_messaging_interface()   # returns MessagingInterface
    iface.say("Hello!")
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from core.config import load_agent_config

if TYPE_CHECKING:
    from messaging.base import MessagingInterface

_CHANNEL_ENV = "MESSAGING_CHANNEL"
_DEFAULT_CHANNEL = "slack"

_SUPPORTED_CHANNELS = ("slack", "whatsapp", "teams", "local")


def resolve_messaging_channel() -> str:
    """Resolve active channel from env var or ``~/.agent_settings.json``.

    Resolution order:
      1. ``MESSAGING_CHANNEL`` environment variable (set by systemd drop-ins
         for most services).
      2. ``default_channel`` in ``~/.agent_settings.json`` (always available,
         including for ``ninja.service`` which has no systemd drop-in).
      3. Falls back to ``"slack"`` if neither is set.
    """
    explicit = os.environ.get(_CHANNEL_ENV)
    if explicit and explicit.strip():
        resolved = explicit.strip().lower()
        if resolved not in _SUPPORTED_CHANNELS:
            raise ValueError(
                f"Unsupported messaging channel: {resolved!r}. "
                f"Choose from: {', '.join(_SUPPORTED_CHANNELS)}"
            )
        return resolved

    # Env var not set — fall back to agent_settings.json
    try:
        channel = load_agent_config().get("default_channel", "")
        if channel and channel.strip():
            resolved = channel.strip().lower()
            if resolved in _SUPPORTED_CHANNELS:
                return resolved
    except Exception:
        pass

    return _DEFAULT_CHANNEL


def get_messaging_interface(channel: str | None = None) -> "MessagingInterface":
    """
    Return an initialised MessagingInterface for the active channel.

    Args:
        channel: Override the channel name. Falls back to the
                 MESSAGING_CHANNEL env-var, then "slack".

    Returns:
        A concrete MessagingInterface instance.

    Raises:
        ValueError: If the requested channel is not supported.
    """
    resolved = channel or resolve_messaging_channel()

    if resolved not in _SUPPORTED_CHANNELS:
        raise ValueError(
            f"Unsupported messaging channel: {resolved!r}. "
            f"Choose from: {', '.join(_SUPPORTED_CHANNELS)}"
        )

    if resolved == "slack":
        from messaging.slack.interface import SlackInterface

        return SlackInterface()

    if resolved == "whatsapp":
        from messaging.whatsapp.interface import WhatsAppInterface

        return WhatsAppInterface()

    if resolved == "teams":
        from messaging.teams.interface import TeamsInterface

        return TeamsInterface()

    if resolved == "local":
        from messaging.local.interface import LocalInterface

        return LocalInterface()

    # Unreachable — kept for type-checker satisfaction
    raise ValueError(f"Unhandled channel: {resolved!r}")
