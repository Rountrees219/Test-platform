"""
Process-layer ABCs — channel-agnostic contracts for monitor and prompt strategies.

Each channel adapter (slack/, whatsapp/, teams/) must implement these interfaces.
Internal code should dispatch via ``processes/factory.py``, never import a
specific channel's strategy directly.

Mirrors the ``messaging/base.py`` pattern: ABCs here, implementations in
per-channel subdirectories, factory picks the right one at runtime.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class MonitorStrategy(ABC):
    """Channel-specific monitor process strategy.

    Slack polls ``iface.get_history()``; WhatsApp delegates to a separate
    monitor service.
    """

    @abstractmethod
    def run(self) -> int:
        """Run the monitor loop.

        This is the main entry point for the channel's monitor process.
        Should block until the monitor is done (shutdown signal, max runtime,
        etc.).

        Returns:
            Exit code (0 for clean shutdown).
        """


class PromptVars(BaseModel):
    """Channel-specific prompt variables returned by ``PromptStrategy``.

    Attributes:
        channel:       Human-readable channel label
                       (e.g. ``"#general"``, ``"(unbound)"``).
        default_task:  Fallback task instruction when no explicit task is
                       given to the orchestrator.
        interface_doc: The line-3 documentation reference for the channel's
                       interface docs (full markdown line).
    """

    channel: str
    default_task: str
    interface_doc: str


class PromptStrategy(ABC):
    """Channel-specific prompt building strategy.

    Each channel provides the variables that differ in the orchestrator's
    ``build_prompt()`` output: which channel label to show, what the default
    task instruction says, and which interface doc to reference.
    """

    @abstractmethod
    def get_prompt_vars(self, config: dict) -> PromptVars:
        """Return channel-specific prompt variables.

        Args:
            config: The agent settings dict (from ``~/.agent_settings.json``).

        Returns:
            A ``PromptVars`` instance with channel, default_task, and
            interface_doc populated.
        """
