import time
from pathlib import Path

from agent_providers.base import AgentRunConfig, AgentRunResult
from agent_providers.codex.codex_provider import CodexProvider
from constants import (
    CODEX_RUN_MONITOR_TIMEOUT_SECONDS,
    ORCHESTRATOR_SERVICE_NAME,
    SYSTEM_PROMPT_ORCHESTRATOR_CODEX_PATH,
)
from core.logging import get_logger

REPO_ROOT = Path(__file__).parent.parent

logger = get_logger(ORCHESTRATOR_SERVICE_NAME)


def run_codex_agent(
    prompt: str,
    title: str,
    env: dict,
    logger,
    task_id: str,
) -> tuple[AgentRunResult, float]:
    logger.info(f"Running orchestrator using Codex agent with prompt: {prompt}")

    spec = AgentRunConfig(
        prompt=prompt,
        system_prompt_path=SYSTEM_PROMPT_ORCHESTRATOR_CODEX_PATH,
        tools=["Bash", "Edit", "Read", "Write"],
        timeout_seconds=CODEX_RUN_MONITOR_TIMEOUT_SECONDS,
        session_name="orchestrator",
        cwd=REPO_ROOT,
        env=env,
        process_label="orchestrator",
        title=title,
        task_id=task_id,
    )

    provider = CodexProvider()
    provider.setup(logger)

    run_started = time.monotonic()
    result = provider.run(spec, logger)
    return result, round(time.monotonic() - run_started, 2)
