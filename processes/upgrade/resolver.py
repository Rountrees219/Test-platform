"""ConflictResolver — hands merge conflicts to whichever agent harness this box
actually runs.

The harness is chosen the same way the rest of the product chooses it (the
selected model), so a Codex box doesn't get a claude invocation that may have no
CLI, no credentials, or no usable model configured. Going through
``AgentProvider`` also means we don't hardcode a wrapper path or reimplement
prompt-file/timeout handling.

Best-effort by contract: every failure is reported and returns control to the
caller's customer-wins fallback, which never loses the customer's edits.
"""

from __future__ import annotations

from constants import UPGRADE_SERVICE_NAME
from core.logging import get_logger
from processes.upgrade.core import Command, Config
from processes.upgrade.reporting import Reporter

logger = get_logger(UPGRADE_SERVICE_NAME)

_TOOLS = ["Bash", "Edit", "Read", "Write"]


class ConflictResolver:
    def __init__(self, config: Config, reporter: Reporter):
        self._c = config
        self._r = reporter
        self._cmd = Command(reporter, cwd=config.ninja_home)

    def resolve(self, prompt: str, files: list[str]) -> None:
        """Attempt an in-place resolution of ``files``.

        Nothing is returned: the caller decides whether it worked by scanning
        for leftover conflict markers, which is the only answer we trust.
        """
        if self._c.llm_resolve_cmd:
            self._cmd.run(
                self._c.llm_resolve_cmd,
                str(self._c.ninja_home),
                "\n".join(files),
                capture=False,
                quiet=False,
            )
            return

        harness = self._harness()
        if harness is None:
            return
        provider, name = harness
        result = provider.run(self._spec(prompt), logger)
        if result.timed_out:
            self._r.log_warn(
                f"{name} conflict resolution timed out after {self._c.llm_timeout}s"
            )
        elif result.exit_code != 0:
            detail = (result.stderr or "").strip().splitlines()
            self._r.log_warn(
                f"{name} conflict resolution exited {result.exit_code}"
                + (f": {detail[-1][:200]}" if detail else "")
            )
        else:
            self._r.log(f"{name} finished resolving conflicts")

    def _spec(self, prompt: str):
        from agent_providers.base import AgentRunConfig

        return AgentRunConfig(
            prompt=prompt,
            task_id="upgrade-conflicts",
            tools=_TOOLS,
            timeout_seconds=self._c.llm_timeout,
            # Its own session lane, so a resolution can't inherit or disturb
            # the monitor's conversation.
            session_name="upgrade",
            cwd=self._c.ninja_home,
            process_label="upgrade",
            title="Upgrade conflict resolution",
        )

    def _harness(self):
        """The provider for this box's selected model, or None if unavailable.

        Imported lazily and reported rather than raised: a missing agent CLI
        must degrade the upgrade to customer-wins, not fail it.
        """
        try:
            from agent_providers.claude.claude_provider import ClaudeProvider
            from agent_providers.codex.codex_provider import CodexProvider
            from constants import CODEX_HARNESS_MODEL
            from core.metadata import get_selected_model

            model = get_selected_model()
            if model in CODEX_HARNESS_MODEL:
                provider = CodexProvider()
                provider.setup(logger)  # writes config, installs skills
                return provider, f"codex ({model})"
            return ClaudeProvider(), f"claude ({model})"
        except (ImportError, RuntimeError, OSError) as e:
            self._r.log_error(
                f"no agent harness available to resolve conflicts ({e}) "
                "— falling back to customer-wins"
            )
            return None
