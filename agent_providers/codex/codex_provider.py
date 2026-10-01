import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import replace
from functools import partial
from pathlib import Path

from agent_providers.base import (
    AgentProvider,
    AgentRunConfig,
    AgentRunResult,
    git_askpass_env,
)
from agent_providers.codex.codex_utils import get_latest_traces_session_id
from clients.litellm_client import get_config
from clients.super_ninja_client import get_thread_id
from constants import (
    AGENTS_MD_CODEX_PATH,
    ENV_VAR_CONVERSATION_ID,
    ENV_VAR_FEATURE,
    ENV_VAR_TASK_ID,
    HEADER_NINJA_CONVERSATION_ID,
    HEADER_NINJA_FEATURE,
    HEADER_NINJA_TASK_ID,
)
from core.config import load_codex_settings, save_codex_settings
from core.metadata import get_selected_model
from pydantic import BaseModel
from utils.cost import build_feature, compute_cost

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODEX_CONFIG_DIR = Path.home() / ".codex"
CODEX_CONFIG_FILE = CODEX_CONFIG_DIR / "config.toml"
LENGTH_PROMPT_FILE = REPO_ROOT / "stopped_length_prompt.txt"
# Written by codex_length_hook.py, which hardcodes the same path because it runs
# standalone (outside the package). Keep the two in sync.
LENGTH_STATE_FILE = Path("/workspace/ninja/stop_hook_length_state.json")


# ----- Length hardstop resume (mirrors claude-wrapper.sh) -------------------


def _stopped_by_length() -> bool:
    """True if the length hook hard-stopped the current monitor run."""
    try:
        return (
            json.loads(LENGTH_STATE_FILE.read_text()).get("stopped_by_length") is True
        )
    except (OSError, ValueError):
        return False


def _reset_length_state() -> None:
    try:
        LENGTH_STATE_FILE.write_text(json.dumps({"stopped_by_length": False}))
    except OSError:
        pass


def _length_handoff_prompt() -> str:
    try:
        return LENGTH_PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return (
            "You were stopped because the number of turns reached its "
            "maximum. Summarize the current progress and report back to the "
            "user, and create a GitHub issue so the background agent can "
            "continue the task."
        )


class CodexUsage(BaseModel):
    """Token usage from Codex. Supports ``+=`` for aggregating across turns."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def __iadd__(self, other: "CodexUsage") -> "CodexUsage":
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_output_tokens += other.reasoning_output_tokens
        return self

    def compute_cost(self, model: str) -> float:
        uncached_input = max(self.input_tokens - self.cached_input_tokens, 0)
        return compute_cost(
            model, uncached_input, self.output_tokens, 0, 0, self.cached_input_tokens
        )


def parse_codex_output(stdout: str) -> CodexUsage:
    """Parse Codex ``--json`` JSONL stream and return aggregated usage."""
    total = CodexUsage()
    if not stdout:
        return total
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        total += CodexUsage.model_validate(usage)
    return total


class CodexProvider(AgentProvider):
    name = "codex"

    def setup(self, logger: logging.Logger) -> bool:
        """
        Setup codex cli and setup skills.
        """
        if not shutil.which("codex"):
            logger.error("Codex CLI not found on PATH!")
            logger.error("Install with: npm install -g @openai/codex")
            raise RuntimeError("Codex CLI not found on PATH")
        self._write_config(logger)
        self._install_skills(logger)
        load_codex_settings()

    def upgrade(self, logger: logging.Logger, timeout: int = 120) -> None:
        if not shutil.which("codex"):
            return
        try:
            subprocess.run(
                ["npm", "install", "-g", "@openai/codex@latest"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("codex upgrade skipped", exc_info=True)

    # TODO: move inline config to a config template
    def _write_config(self, logger: logging.Logger) -> None:
        """Codex uses TOML. Point it at LiteLLM via an OpenAI-compatible
        provider so it reuses the same gateway/token Claude uses."""
        CODEX_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        base_url = get_config().get("base_url", "")
        model = get_selected_model()
        stop_hook = REPO_ROOT / "agent_providers" / "codex_hooks" / "codex_stop_hook.py"
        length_hook = (
            REPO_ROOT / "agent_providers" / "codex_hooks" / "codex_length_hook.py"
        )
        config = f"""\
model = "{model}"
model_provider = "litellm"
approval_policy = "never"
sandbox_mode = "danger-full-access"
model_auto_compact_token_limit = 200000
web_search = "disabled"

[tools]
view_image = false

[skills]
bundled.enabled = false

[model_providers.litellm]
name = "litellm"
base_url = "{base_url}"
env_key = "LITELLM_API_KEY"
wire_api = "responses"
env_http_headers = {{ "{HEADER_NINJA_TASK_ID}" = "{ENV_VAR_TASK_ID}", "{HEADER_NINJA_FEATURE}" = "{ENV_VAR_FEATURE}", "{HEADER_NINJA_CONVERSATION_ID}" = "{ENV_VAR_CONVERSATION_ID}" }}

[projects."/workspace/ninja"]
trust_level = "trusted"

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = 'python3 {stop_hook}'
timeout = 60

[[hooks.PostToolUse]]

[[hooks.PostToolUse.hooks]]
type = "command"
command = 'python3 {length_hook}'
timeout = 30
"""
        CODEX_CONFIG_FILE.write_text(config, encoding="utf-8")
        logger.info("Wrote %s", CODEX_CONFIG_FILE)

    def _install_skills(self, logger: logging.Logger) -> None:
        """Codex skills"""
        skills_root = CODEX_CONFIG_DIR / "skills"
        if not skills_root.exists():
            subprocess.run(
                [
                    "unzip",
                    "-o",
                    str(REPO_ROOT / "skills.zip"),
                    "-d",
                    str(CODEX_CONFIG_DIR),
                    "-x",
                    "__MACOSX/*",
                    "*/.DS_Store",
                ],
                capture_output=True,
                text=True,
            )
        lines = [
            "## Available Skills",
            "File-based skills — read the SKILL.md then run its scripts.",
            "",
        ]
        logger.info(
            f"Found {len(list(skills_root.glob('*/SKILL.md')))} skills in {skills_root}"
        )
        for skill_md in sorted(skills_root.glob("*/SKILL.md")):
            desc = ""
            for ln in skill_md.read_text(encoding="utf-8").splitlines():
                if ln.startswith("description:"):
                    desc = ln[len("description:") :].strip()
                    break
            lines.append(f"- `{skill_md.parent.name}` — {desc} (see `{skill_md}`)")
        AGENTS_MD_CODEX_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _resume_failed(r: subprocess.CompletedProcess) -> bool:
        if r.returncode == 0:
            return False
        combined = ((r.stdout or "") + (r.stderr or "")).lower()
        return "session" in combined or "not found" in combined

    def run(self, spec: AgentRunConfig, logger: logging.Logger) -> AgentRunResult:
        """Run one invocation of the Codex CLI and return a normalised result."""
        logger.info("Codex: starting run with %ds timeout", spec.timeout_seconds)

        prompt_file = self._write_prompt_file(spec.prompt)
        try:
            cfg = get_config()
            auth_token = cfg["api_key"]
            if not auth_token:
                logger.error(
                    "ANTHROPIC_AUTH_TOKEN not found in settings.json "
                    f"(looked in source: {cfg.get('source') or 'no settings file'})"
                )

            task_id = spec.task_id
            conversation_id = get_thread_id() or str(uuid.uuid4())
            env = {
                **os.environ,
                **git_askpass_env(),
                **spec.env,
                "LITELLM_API_KEY": auth_token,
                ENV_VAR_TASK_ID: task_id,
                ENV_VAR_FEATURE: build_feature(spec.title),
                ENV_VAR_CONVERSATION_ID: conversation_id,
            }
            logger.info(
                f"Codex running with task_id: {task_id}, conversation_id: {conversation_id}"
            )

            run_codex = partial(
                subprocess.run,
                cwd=str(spec.cwd or REPO_ROOT),
                timeout=spec.timeout_seconds + 60,
                capture_output=True,
                text=True,
                env=env,
            )

            session_id = (
                load_codex_settings().get("session_id", {}).get(spec.session_name)
            )
            logger.info(f"Using session_id for {spec.session_name}: {session_id}")

            argv = self.get_argv(spec, session_id)
            with open(prompt_file, encoding="utf-8") as stdin_f:
                r = run_codex(argv, stdin=stdin_f)

            if session_id and self._resume_failed(r):
                logger.warning(f"Codex resume failed for {session_id}; retrying fresh")
                save_codex_settings(spec.session_name, None)
                argv = self.get_argv(spec, None)
                with open(prompt_file, encoding="utf-8") as stdin_f:
                    r = run_codex(argv, stdin=stdin_f)
                session_id = None

            if not session_id:
                session_id = get_latest_traces_session_id()
                if session_id:
                    logger.info(
                        f"Discovered session id for {spec.session_name}: {session_id}"
                    )
                    save_codex_settings(spec.session_name, session_id)

            # Length hardstop resume: the codex_length_hook stopped this run
            # once the tool-call budget was hit. Resume the SAME session one
            # more time with the handoff prompt so the monitor summarises and
            # files a GitHub issue. Codex analogue of claude-wrapper.sh's
            # stopped_by_length retry. A timed-out run never reaches here — it raises TimeoutExpired and is handled below.
            if spec.session_name == "monitor" and session_id and _stopped_by_length():
                logger.info(
                    "Length hardstop: resuming monitor session %s with handoff prompt",
                    session_id,
                )
                _reset_length_state()
                handoff = _length_handoff_prompt()
                retry_spec = replace(spec, prompt=handoff)
                retry_argv = self.get_argv(retry_spec, session_id)
                retry_file = self._write_prompt_file(handoff)
                try:
                    with open(retry_file, encoding="utf-8") as stdin_f:
                        r = run_codex(retry_argv, stdin=stdin_f)
                finally:
                    os.unlink(retry_file)

            res = AgentRunResult(r.stdout, r.stderr, r.returncode)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Codex CLI timed out after %d minutes",
                (spec.timeout_seconds + 60) // 60,
            )
            res = AgentRunResult(timed_out=True, exit_code=124)
        except OSError as exc:
            logger.error(f"OS error running Codex: {exc}")
            res = AgentRunResult(stderr=str(exc), exit_code=1)
        finally:
            os.unlink(prompt_file)

        return res

    def get_argv(self, spec: AgentRunConfig, session_id: str | None) -> list[str]:
        """Return the argv for a Codex run"""
        argv = ["codex", "exec"]
        if session_id:
            argv += ["resume", session_id]
        argv.append("--skip-git-repo-check")
        argv.append("--dangerously-bypass-hook-trust")
        argv.append("--json")
        if not session_id:
            argv += ["--cd", str(spec.cwd or REPO_ROOT)]
        if spec.system_prompt_path:
            argv += [
                "-c",
                f'model_instructions_file="{spec.system_prompt_path}"',
            ]
        argv.append("-")
        return argv
