"""agent_providers.base — provider abstraction for agent CLIs.

An ``AgentProvider`` owns everything that differs between agent backends
(Claude Code vs Codex): how the CLI is launched, how the system prompt is
delivered, and how the "keep working until the issue queue empties" loop is
realised (Claude uses a Stop hook; Codex has none, so its provider loops in
Python).

The shared ``run_agent`` (see ``runner.py``) builds an ``AgentRunConfig`` and
hands it to a provider's ``run``. This keeps every caller — orchestrator.py,
monitor_service.py — free of CLI-specific details.
"""

from __future__ import annotations

import logging
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Written by ninja-install.sh's setup_git_askpass(); same path processes/upgrade/core.py::Git.ASKPASS
# uses. ninja.service doesn't inherit it otherwise, so the agent's own git commands would fail
# with "could not read Username".
GIT_ASKPASS_PATH = Path("/usr/local/bin/git-askpass.sh")


def git_askpass_env() -> dict[str, str]:
    if GIT_ASKPASS_PATH.is_file():
        return {"GIT_ASKPASS": str(GIT_ASKPASS_PATH)}
    return {}


@dataclass
class AgentRunConfig:
    """Configure agent provider to run an invocation."""

    prompt: str
    task_id: str
    system_prompt_path: Optional[Path] = None
    system_prompt_mode: str = "replace"  # "replace" or "append"
    tools: list[str] = field(default_factory=lambda: ["Bash", "Edit", "Read", "Write"])
    timeout_seconds: int = 900
    session_name: str = "monitor"
    cwd: Optional[Path] = None
    env: dict[str, str] = field(default_factory=dict)

    process_label: str = "monitor"
    title: Optional[str] = "Agent Run"


@dataclass
class AgentRunResult:
    """Normalised result so callers don't care which CLI ran."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    parsed: object = None  # ClaudeResult when set by ClaudeProvider


class AgentProvider(ABC):
    """Base class for all agent CLI backends."""

    name: str = "base"

    def __init__(self):
        super().__init__()

    @abstractmethod
    def setup(self, logger: logging.Logger) -> bool:
        """Verify/install the CLI, write config, unpack skills.

        Return False to abort startup.
        """

    def upgrade(self, logger: logging.Logger, timeout: int = 120) -> None:
        """Idempotent CLI self-update. Default: no-op (override as needed)."""

    @abstractmethod
    def run(self, spec: AgentRunConfig, logger: logging.Logger) -> AgentRunResult:
        """Run one monitor invocation and return a normalised result."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _write_prompt_file(prompt: str) -> str:
        """Write ``prompt`` to a temp file (fed to the CLI via stdin to avoid
        E2BIG on large prompts). Caller is responsible for unlinking it."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            return f.name
