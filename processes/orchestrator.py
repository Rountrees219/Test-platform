"""
Ninja Orchestrator — Browser Automation Agent

Runs Claude Code as the Ninja browser automation agent.
Agent identity is read from ~/.agent_settings.json config file.
Agent behavior is defined by agent-docs/NINJA_SPEC.md.

Usage:
    python orchestrator.py                    # Run default work loop
    python orchestrator.py --task "Do X"      # Run single task
    python orchestrator.py --list             # List available agents
    python orchestrator.py --test             # Run capability tests
"""

import argparse
import json
import logging
import os
import shutil
import string
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent_providers.base import AgentRunConfig
from agent_providers.claude.claude_output import primary_model
from agent_providers.claude.claude_provider import ClaudeProvider
from agent_providers.codex.codex_provider import parse_codex_output

# Import centralized agent configuration
from agents_config import AGENTS
from clients.posthog_client import capture, is_feature_enabled
from clients.super_ninja_client import get_thread_id
from clients.token_resolver import resolve_github_token
from constants import (
    AGENT_SETTINGS_PATH,
    CLAUDE_RUN_ORCHESTRATOR_TIMEOUT_SECONDS,
    CODEX_HARNESS_MODEL,
    DEFAULT_MODEL,
    ORCHESTRATOR_SERVICE_NAME,
    SANDBOX_METADATA_PATH,
    STOP_HOOKS_FEATURE_FLAG,
    SYSTEM_PROMPT_PATH_ORCHESTRATOR,
)
from core.config import is_orchestrator_enabled
from core.logging import get_logger
from core.metadata import load_sandbox_metadata
from messaging.factory import resolve_messaging_channel
from services.orchestrator_service import run_codex_agent
from tools import issues
from utils.cost import (
    active_messaging_channel,
    build_custom_headers,
    record_task_cost,
)
from utils.system_notification import get_disk_warning
from utils.vision_skill import vision_skill_enabled

REPO_ROOT = Path(__file__).parent.parent
CLAUDE_PIN_INSTALLER = REPO_ROOT / "agent_providers/claude/pin-claude-code-install.sh"
LOCK_FILE = REPO_ROOT / ".orchestrator.lock"
# systemd unit the orchestrator runs as. Canonical home for this name; the
# monitor imports it from here so there is a single source of truth.
ORCHESTRATOR_SERVICE = "ninja.service"
LOG_DIR = Path("/workspace/logs")
MCP_TOKEN_FILE = Path("/dev/shm/mcp-token")
SETTINGS_FILE = REPO_ROOT / "settings.json"
# Blocked-issue review: every N orchestrator cycles, revisit issues labelled
# 'blocked' to see if any can be unblocked. Counter persists across runs.
CYCLE_COUNT_FILE = REPO_ROOT / ".cycle_count"
BLOCKED_REVIEW_EVERY = 24
CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

# Settings template - variables filled from /root/.claude/settings.json
# Stop + PostToolUse are always registered. Stop no-ops unless NINJA_CYCLE_RUN=1;
# PostToolUse (monitor length) no-ops unless NINJA_MONITOR_RUN=1 and the
# orchestrator is enabled.
SETTINGS_TEMPLATE = string.Template("""{
    "env": {
        "ANTHROPIC_AUTH_TOKEN": "$auth_token",
        "ANTHROPIC_BASE_URL": "$base_url",
        "ANTHROPIC_MODEL": "$model"
    },
    "modelPicker": $model_picker,
    "permissions": {
        "allow": [
            "Bash","Edit(**)","Skill","WebSearch","WebFetch",
            "Read(//tmp/**)","Edit(//tmp/**)"
        ]
    },
    "attribution": {
        "commit": ""
    },
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 $stop_hook",
                        "timeout": 60
                    }
                ]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 $length_hook",
                        "timeout": 30
                    }
                ]
            }
        ]
    }
}
""")


def _add_vision_read_hook(settings: dict, model: str) -> dict:
    """Point a Read of a picture at the vision skill, for a model that cannot see one.

    Read hands the file to the model and the provider answers 400, which takes
    the whole turn down rather than the one tool call.
    """
    if vision_skill_enabled(model):
        settings["hooks"]["PreToolUse"] = [
            {
                "matcher": "Read",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 {REPO_ROOT / 'vision_read_hook.py'}",
                        "timeout": 15,
                    }
                ],
            }
        ]
    return settings


# Ensure log directory exists. /workspace/logs only exists inside the production
# So skip the mkdir when running under pytest
if os.environ.get("NINJA_TEST_MODE") != "1":
    LOG_DIR.mkdir(exist_ok=True)


# Single canonical logger name. Filename is always ``ninja_YYYY-MM-DD.log``.
# We deliberately ignore caller-supplied agent names — historically several
# callers passed ``"orchestrator"`` early in startup and then ``"ninja"``
# later, which created empty ``orchestrator_*.log`` orphan files daily.
_LOGGER_NAME = "ninja"

logger = get_logger(ORCHESTRATOR_SERVICE_NAME)


def setup_logging(agent_name: str = "orchestrator") -> logging.Logger:
    """Return the canonical ninja logger.

    Idempotent: repeated calls return the same logger without re-creating
    handlers. The file is opened lazily (``delay=True``) so a logger that
    is set up but never written to does not create an empty file.

    The ``agent_name`` argument is accepted for backward compatibility
    but ignored — see ``_LOGGER_NAME``.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # don't bubble to root → no duplicate stdout

    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_filename = LOG_DIR / f"ninja_{datetime.now().strftime('%Y-%m-%d')}.log"
    file_handler = logging.FileHandler(
        log_filename,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # No StreamHandler: systemd captures stdout/stderr into the journal
    # (StandardOutput=journal in ninja.service). Adding a stdout handler
    # here would duplicate every logger.info() into journalctl.

    return logger


def log_and_print(
    msg: str, level: str = "info", logger: logging.Logger = None, file=None
):
    """Print a message and also log it. Works before or after logger is set up."""
    # Always print to console (or specified file)
    print(msg, file=file)
    # Also log if logger is available
    if logger:
        clean_msg = msg.strip()
        if clean_msg:
            getattr(logger, level, logger.info)(clean_msg)


def get_selected_model(logger: logging.Logger = None) -> str:
    """
    Read litellm_selected_model from /dev/shm/sandbox_metadata.json if present.
    Falls back to DEFAULT_MODEL ('claude-opus-5') if the file
    doesn't exist, is unreadable, or doesn't contain litellm_selected_model.

    Returns:
        Model name string
    """
    _logger = logger or setup_logging("orchestrator")
    meta = load_sandbox_metadata()
    if not meta:
        _logger.debug(
            f"sandbox_metadata not found at {SANDBOX_METADATA_PATH}, using default model: {DEFAULT_MODEL}"
        )
        return DEFAULT_MODEL

    model = meta.get("litellm_selected_model", "").strip()
    if model:
        _logger.info(f"🎯 Model from sandbox_metadata: {model}")
        return model
    _logger.debug(
        f"litellm_selected_model not set in sandbox_metadata, using default: {DEFAULT_MODEL}"
    )
    return DEFAULT_MODEL


def upgrade_claude_cli(logger: logging.Logger = None, timeout: int = 60) -> None:
    """
    Upgrade the Claude Code CLI binary to the latest release by shelling
    out to ``claude update``.

    Behaviour
    ---------
    * If ``claude`` isn't on ``PATH`` we log and return cleanly — this
      function must never block sandbox startup on an absent binary.
    * ``claude update`` is idempotent: 3–5 s the first time (real
      download), <1 s afterwards ("Claude Code is up to date"). Exit
      code is 0 in both cases, so we don't have to parse output.
    * Any failure (non-zero exit, timeout, FileNotFoundError) is logged
      at WARNING level and swallowed. Ninja continues with whatever
      version is currently installed.

    Args:
        logger:  Optional pre-configured logger. Falls back to the
                 orchestrator logger.
        timeout: Seconds to wait for ``claude update`` before giving
                 up. 60s is generous — in practice the upgrade finishes
                 in <5s over a normal connection.
    """
    _logger = logger or setup_logging("orchestrator")

    if not shutil.which("claude"):
        _logger.debug("claude CLI not found on PATH — skipping upgrade check")
        return

    # Capture the before-version so we can log a clean "X → Y" line when
    # an actual upgrade happened. Any failure here is harmless; we just
    # proceed without the before number.
    before = ""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # Output is like "2.1.131 (Claude Code)"; take the first token.
            before = result.stdout.strip().split()[0] if result.stdout.strip() else ""
    except (subprocess.SubprocessError, OSError):
        pass

    try:
        # stdin=DEVNULL so the CLI never prompts interactively; the
        # --help output confirms the command takes no args.
        result = subprocess.run(
            [str(CLAUDE_PIN_INSTALLER)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _logger.warning(
            f"⚠️ claude update timed out after {timeout}s — "
            f"continuing with installed version"
        )
        return
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.warning(
            f"⚠️ claude update failed to start ({exc}) — "
            f"continuing with installed version"
        )
        return

    if result.returncode != 0:
        _logger.warning(
            f"⚠️ claude update exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
        return

    # Re-read the version so we can report whether this run actually
    # pulled a new binary.
    after = ""
    try:
        v = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if v.returncode == 0 and v.stdout.strip():
            after = v.stdout.strip().split()[0]
    except (subprocess.SubprocessError, OSError):
        pass

    if before and after and before != after:
        _logger.info(f"⬆️  Upgraded Claude CLI {before} → {after}")
    elif after:
        _logger.info(f"✓ Claude CLI is up to date ({after})")
    else:
        _logger.info("✓ Claude CLI update check completed")


def ensure_settings_file(logger: logging.Logger = None) -> bool:
    """
    Ensure settings.json exists in the project directory and that
    /root/.claude/settings.json uses the correct model. Also upgrades
    the Claude Code CLI binary to the latest release (once per start).

    Model selection priority:
      1. litellm_selected_model from /dev/shm/sandbox_metadata.json (if present)
      2. Default: claude-opus-5

    Always regenerates settings.json to pick up model changes.
    Also updates /root/.claude/settings.json with the selected model.

    Returns:
        True if settings.json exists or was created, False otherwise
    """
    _logger = logger or setup_logging("orchestrator")

    # Upgrade the Claude CLI binary before we touch settings. This runs
    # exactly once per sandbox start because ensure_settings_file() is
    # itself called once from main(). `claude update` is idempotent, so
    # subsequent starts on an already-current binary are a <1 s no-op.
    upgrade_claude_cli(_logger)

    # Determine model
    model = get_selected_model(_logger)

    # CLAUDE_SETTINGS_FILE lives in tmpfs (/dev/shm), wiped on every stop/start/reboot, so its symlink
    # dangles after a restart. SETTINGS_FILE is durable (EBS) and carries the same env block, so fall
    # back to it as the source and the box self-heals without suna re-onboarding it.
    source = CLAUDE_SETTINGS_FILE if CLAUDE_SETTINGS_FILE.exists() else SETTINGS_FILE
    if not source.exists():
        _logger.error(
            f"❌ Source settings not found: {CLAUDE_SETTINGS_FILE} or {SETTINGS_FILE}"
        )
        _logger.error("Cannot auto-generate settings.json. Please create it manually.")
        return False

    try:
        with open(source, "r") as f:
            claude_settings = json.load(f)

        env = claude_settings.get("env", {})
        auth_token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        base_url = env.get("ANTHROPIC_BASE_URL", "")

        if not auth_token or not base_url:
            _logger.error(
                "❌ Missing required fields in source settings (auth_token or base_url)"
            )
            return False

        # --- Repair/refresh /root/.claude/settings.json ---
        # On the durable-fallback path the tmpfs source is gone, so rewrite it unconditionally to
        # repair the dangling symlink target; otherwise only touch it when the model changed.
        current_model = env.get("ANTHROPIC_MODEL", "")
        model_picker = (
            {
                "options": [
                    {
                        "model": "deepseek-4.1-flash",
                        "label": "DeepSeek 4.1 Flash",
                        "description": "Fast model for coding and general-purpose tasks",
                    }
                ]
            }
            if model == "deepseek-4.1-flash"
            else {"options": []}
        )
        settings_changed = (
            source is not CLAUDE_SETTINGS_FILE
            or current_model != model
            or claude_settings.get("modelPicker") != model_picker
            or "modelOverrides" in claude_settings
        )
        if settings_changed:
            claude_settings["env"]["ANTHROPIC_MODEL"] = model
            claude_settings["modelPicker"] = model_picker
            claude_settings.pop("modelOverrides", None)
            CLAUDE_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CLAUDE_SETTINGS_FILE, "w") as f:
                json.dump(claude_settings, f, indent=4)
            _logger.info(
                f"🔄 Updated {CLAUDE_SETTINGS_FILE} model: {current_model} → {model}"
            )

        # --- Generate project settings.json (always regenerate) ---
        settings = json.loads(
            SETTINGS_TEMPLATE.substitute(
                auth_token=auth_token,
                base_url=base_url,
                model=model,
                model_picker=json.dumps(model_picker),
                stop_hook=str(REPO_ROOT / "orchestrator_stop_hook.py"),
                length_hook=str(REPO_ROOT / "monitor_length_hook.py"),
            )
        )
        settings_content = json.dumps(_add_vision_read_hook(settings, model), indent=4)

        with open(SETTINGS_FILE, "w") as f:
            f.write(settings_content)

        _logger.info(f"✅ Generated {SETTINGS_FILE}")
        _logger.info(f"   Model: {model}")
        _logger.info(f"   Base URL: {base_url}")
        return True

    except (json.JSONDecodeError, IOError, KeyError) as e:
        _logger.error(f"❌ Failed to generate settings.json: {e}")
        return False


def login_github_cli(logger: logging.Logger) -> bool:
    """Resolve a GitHub token (3-tier) and login the gh CLI."""
    logger.info("🔐 Resolving GitHub token (3-tier)...")
    result = resolve_github_token()
    return result["status"] == "ok"


def check_single_instance():
    """
    Ensure only one instance of the orchestrator is running.
    Uses a lock file with PID to detect and prevent duplicate instances.

    Raises:
        SystemExit if another instance is already running
    """
    current_pid = os.getpid()

    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r") as f:
                lock_data = json.load(f)

            old_pid = lock_data.get("pid")
            old_agent = lock_data.get("agent", "unknown")
            old_started = lock_data.get("started", "unknown")
            old_heartbeat = lock_data.get("heartbeat", old_started)

            # Check if the old process is still running
            if old_pid:
                process_exists = False
                is_orchestrator = False

                try:
                    # Send signal 0 to check if process exists
                    os.kill(old_pid, 0)
                    process_exists = True

                    # Verify it's actually an orchestrator process (not PID reuse)
                    try:
                        with open(f"/proc/{old_pid}/cmdline", "r") as f:
                            cmdline = f.read()
                            is_orchestrator = "orchestrator.py" in cmdline
                    except (IOError, FileNotFoundError):
                        # Can't read cmdline (maybe not Linux), assume it's orchestrator
                        is_orchestrator = True

                except OSError:
                    # Process doesn't exist
                    process_exists = False

                # Also check heartbeat staleness (if no heartbeat for 10+ minutes, consider stale)
                heartbeat_stale = False
                try:
                    heartbeat_time = datetime.fromisoformat(old_heartbeat)
                    if (
                        datetime.now() - heartbeat_time
                    ).total_seconds() > 600:  # 10 minutes
                        heartbeat_stale = True
                except (ValueError, TypeError):
                    pass  # Can't parse heartbeat, ignore

                if process_exists and is_orchestrator and not heartbeat_stale:
                    # Process exists and is orchestrator - another instance is running
                    _early_logger = setup_logging("orchestrator")
                    _early_logger.error("=" * 70)
                    _early_logger.error(
                        "ERROR: Another orchestrator instance is already running!"
                    )
                    _early_logger.error("=" * 70)
                    _early_logger.error(f"   Existing instance:")
                    _early_logger.error(f"   - PID: {old_pid}")
                    _early_logger.error(f"   - Agent: {old_agent}")
                    _early_logger.error(f"   - Started: {old_started}")
                    _early_logger.error(f"   - Last heartbeat: {old_heartbeat}")
                    _early_logger.error(f"   To stop the existing instance:")
                    _early_logger.error(f"   - kill {old_pid}")
                    _early_logger.error("   - Or: pkill -f 'orchestrator.py'")
                    _early_logger.error(
                        f"   To force remove the lock (if process is stuck):"
                    )
                    _early_logger.error(f"   - rm {LOCK_FILE}")
                    _early_logger.error("=" * 70)
                    sys.exit(1)
                else:
                    # Stale lock - process doesn't exist, wrong process, or heartbeat stale
                    reason = []
                    if not process_exists:
                        reason.append(f"PID {old_pid} no longer running")
                    elif not is_orchestrator:
                        reason.append(f"PID {old_pid} is not orchestrator (PID reuse)")
                    elif heartbeat_stale:
                        reason.append(f"heartbeat stale since {old_heartbeat}")
                    _early_logger = setup_logging("orchestrator")
                    _early_logger.info(
                        f"Removing stale lock file ({', '.join(reason)})"
                    )
        except (json.JSONDecodeError, IOError, KeyError):
            # Corrupted lock file, remove it
            _early_logger = setup_logging("orchestrator")
            _early_logger.warning("Removing corrupted lock file")

    # Create/update lock file with current process info
    lock_data = {
        "pid": current_pid,
        "agent": None,  # Will be updated after agent is determined
        "started": datetime.now().isoformat(),
        "heartbeat": datetime.now().isoformat(),
    }

    try:
        with open(LOCK_FILE, "w") as f:
            json.dump(lock_data, f)
    except IOError as e:
        _early_logger = setup_logging("orchestrator")
        _early_logger.warning(f"Could not create lock file: {e}")


def is_orchestrator_running() -> bool:
    """Return True if the orchestrator systemd unit is up (active or starting).

    Uses systemd as the OS-native source of truth — ``systemctl is-active`` —
    instead of inspecting a lock file. This is the check the monitor uses to
    decide whether to launch the orchestrator. ``activating`` (the unit's
    ExecStartPre sleep / startup window) counts as "up" so the monitor doesn't
    redundantly start a unit that is already coming up.

    Ninja runs the orchestrator exclusively as ``ninja.service`` under
    systemd, and sandboxes restart every ~30 min, so the OS unit state is both
    authoritative and self-cleaning (no stale lock can survive a restart).
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", ORCHESTRATOR_SERVICE],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # is-active prints one of: active / activating / inactive / failed / ...
        state = result.stdout.strip()
        return state in ("active", "activating", "reloading")
    except (OSError, subprocess.SubprocessError):
        # If systemctl can't be queried, assume not running so work can proceed.
        return False


def update_lock_file(agent_name: str = None):
    """Update the lock file with the agent name and refresh heartbeat."""
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r") as f:
                lock_data = json.load(f)
            if agent_name:
                lock_data["agent"] = agent_name
            lock_data["heartbeat"] = datetime.now().isoformat()
            with open(LOCK_FILE, "w") as f:
                json.dump(lock_data, f)
        except (json.JSONDecodeError, IOError):
            pass


def update_heartbeat():
    """Update just the heartbeat timestamp in the lock file."""
    update_lock_file(agent_name=None)


def remove_lock_file():
    """Remove the lock file when orchestrator exits."""
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except IOError:
        pass


def load_config() -> dict:
    """Load agent configuration from ~/.agent_settings.json"""
    if not AGENT_SETTINGS_PATH.exists():
        return {}

    try:
        with open(AGENT_SETTINGS_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        _early_logger = setup_logging("orchestrator")
        _early_logger.warning(f"Could not read config: {e}")
        return {}


def get_agent_from_config() -> dict:
    """
    Get the agent configuration from the config file.

    Returns:
        Agent dict with name, role, emoji, spec

    Raises:
        SystemExit if no agent is configured
    """
    config = load_config()
    agent_id = config.get("default_agent", "").lower()

    _early_logger = setup_logging("orchestrator")

    if not agent_id:
        _early_logger.error("❌ ERROR: No agent configured!")
        _early_logger.error("")
        _early_logger.error(
            "The orchestrator requires an agent identity to be set in the config file."
        )
        _early_logger.error(f"Config file: {AGENT_SETTINGS_PATH}")
        _early_logger.error("")
        _early_logger.error("💡 To configure your agent, run:")
        _early_logger.error("   python slack_interface.py config --set-agent nova")
        _early_logger.error("")
        _early_logger.error(f"🤖 Available agents: {', '.join(AGENTS.keys())}")
        sys.exit(1)

    if agent_id not in AGENTS:
        _early_logger.error(f"❌ ERROR: Invalid agent '{agent_id}' in config!")
        _early_logger.error("")
        _early_logger.error(f"💡 Valid agents: {', '.join(AGENTS.keys())}")
        _early_logger.error("")
        _early_logger.error("💡 To fix, run:")
        _early_logger.error("   python slack_interface.py config --set-agent nova")
        sys.exit(1)

    return AGENTS[agent_id]


def build_prompt(agent: dict, task: str = "", lean: bool = False) -> str:
    """Build the prompt for the Ninja browser automation agent.

    Channel-specific prompt variables (channel label, default task, interface
    doc line) are provided by the active channel's ``PromptStrategy`` via
    ``processes/factory.py``.

    Args:
        agent: Agent configuration dict
        task: Optional specific task
        lean: When True, omit the heavy context (the detailed doc-read list)
            and replace it with a one-line pointer. Used for follow-up calls
            in a cycle that already carries the docs in its -c conversation
            (e.g. the periodic blocked-issue review) — re-sending the list
            would be wasteful.
    """
    from processes.factory import get_prompt_strategy

    # Get default channel from config
    config = load_config()

    # Channel-specific prompt variables from the active strategy.
    prompt_vars = get_prompt_strategy().get_prompt_vars(config)
    default_task = prompt_vars.default_task
    interface_doc = prompt_vars.interface_doc

    docs_block = f"""1. **Your Specification:** `cat agent-docs/NINJA_SPEC.md`
2. **Agent Protocol:** `cat agent-docs/AGENT_PROTOCOL.md`
{interface_doc}
4. **Workflow Docs:** `cat agent-docs/ORCHESTRATOR.md`
5. **Integrations:** `cat agent-docs/PIPEDREAM_CONNECT.md` — third-party app integrations via Pipedream Connect gateway and the `pdx` CLI"""

    if lean:
        # The docs are already in the continued conversation. Keep only a
        # thin pointer so this call doesn't re-send them.
        return f"""# You are {agent['name']} {agent['emoji']} (continuing this cycle)

You already read the docs in the work phase above. Now do the following.

---

## Current Task

{task if task else default_task}
"""

    return f"""# You are {agent['name']} {agent['emoji']}

## Your Identity
- **Name:** {agent['name']}
- **Role:** {agent['role']}
- **Emoji:** {agent['emoji']}

---

## Documentation Files (READ THESE FIRST)

You are currently running as the orchestrator agent. Before starting work, read these files for full context:

{docs_block}

---

## Current Task

{task if task else default_task}
"""


def count_open_issues() -> int:
    """Return the number of actionable open issues (open and not 'blocked').

    Calls the issue tool directly as a Python function (no subprocess).
    Returns 0 on any failure so a transient GitHub/gh problem is treated as
    an empty queue (cycle skipped) instead of crashing the orchestrator.
    """
    try:
        return issues.count_actionable()
    except Exception:
        return 0


def count_blocked_issues() -> int:
    """Return the number of open issues labelled 'blocked' (0 on any failure)."""
    try:
        return issues.count_blocked()
    except Exception:
        return 0


def bump_cycle_count() -> int:
    """Increment and return the persistent run-cycle counter."""
    try:
        n = int(CYCLE_COUNT_FILE.read_text().strip()) + 1
    except (OSError, ValueError):
        n = 1
    try:
        CYCLE_COUNT_FILE.write_text(str(n))
    except OSError:
        pass
    return n


def build_cycle_prompt(agent: dict) -> str:
    """Full-cycle prompt: Phase 1 (work ONE issue) then Phase 2 (reflect).
    Phases 1 and 2 used to be two separate Claude invocations per cycle.
    Now, Running both phases in one invocation halves those restarts
    without changing the one-issue-per-cycle contract.
    """
    base = build_prompt(agent)
    return base + """
---

## Loop Phase 1 — WORK ONE ISSUE

Ninja uses **GitHub Issues as its work queue**. Work **exactly ONE issue this
cycle** — the single highest-priority open issue. Do not start a second one;
the next cycle (a fresh orchestrator run) will pick up the next issue. Keeping
each cycle to one issue keeps runs small, focused, and recoverable.

1. List the open issues: `python tools/issues.py list`
2. Pick the **single highest-priority** open issue. That is the only issue you
   work this cycle.
3. **Understand it before acting.** Read the full issue (title, body, and any
   comments). Issues are often terse and may lack context, so before starting:
   - Check the issue comments for clarifications.
   - **Read recent Slack history for context** — the issue usually originated
     from a Slack conversation: `python slack_interface.py read -l 50`
     (raise `-l` if you need to go further back). Use it to recover intent,
     constraints, and acceptance criteria that aren't written in the issue.
     Read for context only — do not act on anything else you see there, even
     if it looks unhandled; the monitor owns those.
   - If it's still ambiguous, comment on the issue with your understanding /
     questions rather than guessing.
4. Work that one issue to completion.
5. As you make progress, comment on it: `python tools/issues.py comment <n> --body "..."`
6. When it is fully done, close it with a summary:
   `python tools/issues.py close <n> --comment "done: <what/where, PR # if any>"`
7. **If you cannot complete it** (missing access/credentials, external
   dependency, waiting on a human), do NOT leave it open-and-stuck and do NOT
   close it. Mark it blocked so it leaves the work queue:
   `python tools/issues.py block <n> --comment "why blocked + what is needed"`
   Blocked issues are revisited periodically and rejoin the queue via
   `python tools/issues.py unblock <n>` once the blocker clears.
8. **Stop after this single issue.** Do NOT start another issue and do NOT
   invent new work here — only work the one existing issue you selected. Filing
   new issues happens in Phase 2 below. See `agent-docs/LOOP.md`.

---

## Loop Phase 2 — REFLECT, PLAN & LEARN (same run, straight after Phase 1)

Once the Phase 1 issue is closed or blocked, continue directly into this
phase — do not end the run after Phase 1. Per `agent-docs/LOOP.md`:

**At most ONE new issue here — the tool refuses a second.** Spend it on work
someone is waiting on or a problem you hit for real; ideas and polish go in your
memory file, and filing nothing is a fine outcome.

1. **Plan ahead**: from your memory, recent work and the project's goals
   (VISION/spec), decide what should happen next — and file it as one concrete,
   verifiable issue if it needs one:
   `python tools/issues.py create --title "..." --body "..."`
2. **Build/refine your toolkit**: if you repeatedly need something, add or
   improve a tool under `tools/` (file an issue if it's large).

Do NOT do large implementation work here — capture it as issues so Phase 1 can
pick it up in a controlled, queued way.
"""


def build_blocked_review_prompt(agent: dict) -> str:
    """Periodic prompt: re-check blocked issues and unblock any that can move."""
    base = build_prompt(agent, lean=True)
    return base + """
---

## Blocked-Issue Review

Some open issues are labelled `blocked` (Ninja could not progress them). For
EACH blocked issue, decide:

1. List them: `python tools/issues.py list --label blocked`
2. Read the issue + its BLOCKED comment to see what it was waiting on.
3. If the blocker is now resolved (access granted, dependency shipped, human
   replied), return it to the queue:
   `python tools/issues.py unblock <n> --comment "unblocked: <why>"`
4. If it is permanently impossible or obsolete, close it:
   `python tools/issues.py close <n> --comment "won't do: <why>"`
5. Otherwise leave it blocked — optionally comment what is still missing.

Do NOT do implementation work here; only triage the blocked list.
"""


def build_issues_prompt(num_issues: int, num_blocked_issues: int) -> str:
    if num_blocked_issues == -1:
        return f"You have {num_issues} open issues. Work on an issue, do self-reflection phase right after. Do not check blocked issues this cycle."
    elif num_blocked_issues:
        return f"You have {num_issues} open issues. Work on an issue, do self-reflection phase right after. And also {num_blocked_issues} blocked issues to check."
    return f"You have {num_issues} open issues. Work on an issue, do self-reflection phase right after. No blocked issues."


def build_orchestrator_title(task: str = "") -> str:
    """Usage-dashboard label, e.g. ``my-repo/12``. build_feature() adds the channel.

    Not model-generated: the cycle prompt is a fixed template, so summarising it
    only restates it.
    """
    task = (task or "").strip()
    if task:
        return task

    issue = first_actionable_issue()
    repo = github_repo_name()
    if issue and repo:
        return f"{repo}/{issue['number']}"
    if issue:
        return f"#{issue['number']}"
    return "orchestrator cycle"


def build_orchestrator_feature_title(task: str, title: str) -> str:
    """Title for the ``x-ninja-feature`` header only, so the Supabase
    transactions table reads like any other task. On Teams the issue title
    replaces ``repo/number``; every other channel and every other use of the
    title (task log, agent config) keeps ``title`` unchanged.
    """
    if active_messaging_channel() != "teams" or (task or "").strip():
        return title
    issue = first_actionable_issue()
    feature_title = str(issue["title"]) if issue and issue.get("title") else title
    logger.info(
        f"build_orchestrator_feature_title: title={title!r} "
        f"feature_title={feature_title!r}"
    )
    return feature_title


def github_repo_name() -> str:
    """Repo the issues live in, e.g. ``slack-kimi-k3-aug-5``. "" if unresolved.

    Same order gh uses: GH_REPO (local dev), else the git remote of REPO_ROOT
    (beta/prod, the path tools/issues.py relies on).
    """
    repo = os.environ.get("GH_REPO", "").strip()
    if not repo:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner",
                    "-q",
                    ".nameWithOwner",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(REPO_ROOT),
            )
            repo = result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    return repo.rsplit("/", 1)[-1]


def first_actionable_issue() -> dict | None:
    """Oldest open non-blocked issue, or None. The agent picks its own issue, so
    this is where the run starts, not a guarantee."""
    try:
        actionable = [
            it
            for it in issues.list_issues(state="open")
            if not any(
                lbl.get("name") == issues.BLOCKED_LABEL for lbl in it.get("labels", [])
            )
        ]
        return min(actionable, key=lambda it: it["number"]) if actionable else None
    except Exception:  # noqa: BLE001 — never fail a run over a label
        return None


# The Stop hook's phase + depth counter, and the permit that lets reflect file
# one issue. Both cleared per launch so a killed run can't leak either.
CYCLE_STATE_FILE = REPO_ROOT / "cycle_state.json"
CYCLE_ISSUE_PERMIT_FILE = REPO_ROOT / "cycle_issue_permit"
# Safety net only — ORCHESTRATOR_DEPTH ends the run well before this.
CYCLE_RUN_TIMEOUT = 4 * 3600


def run_agent(
    agent: dict,
    task: str = "",
    prompt: str = None,
    timeout: int = 900,
    cycle: bool = False,
):
    """
    Run agent for orchestrator task. Decides to run codex or claude based on selected model.
    """
    if os.environ.get("NINJA_CYCLE_RUN") == "1":
        logger.warning(
            "Called from inside a running cycle — refusing to launch a nested agent"
        )
        print("Already inside a running agent cycle — nothing to do.")
        return

    logger.info(f"Running agent {agent['name']} with task: {task}")

    if prompt is None:
        prompt = build_prompt(agent, task)

    disk_warning = get_disk_warning()
    if disk_warning:
        prompt += f"\n\n{disk_warning}"

    conversation_id = get_thread_id()
    task_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).timestamp()

    title = build_orchestrator_title(task)
    feature_title = build_orchestrator_feature_title(task, title)

    codex_harness_enabled = get_selected_model() in CODEX_HARNESS_MODEL

    if codex_harness_enabled:
        logger.info(f"Selected model is a codex harness model, using Codex agent.")
        subprocess_env = {**os.environ}
        if cycle:
            # Arms the Stop hook, which otherwise no-ops. Clearing both
            # files restarts the depth count and the issue permit.
            subprocess_env["NINJA_CYCLE_RUN"] = "1"
            CYCLE_STATE_FILE.unlink(missing_ok=True)
            CYCLE_ISSUE_PERMIT_FILE.unlink(missing_ok=True)
        try:
            result, duration_seconds = run_codex_agent(
                prompt,
                feature_title,
                subprocess_env,
                logger,
                task_id=task_id,
            )
            if result.stderr:
                logger.warning(f"Codex stderr:\n{result.stderr}")
            logger.info(f"Agent run completed")

            capture(
                "agent_provider_run_duration",
                {
                    "process": "orchestrator",
                    "provider": "codex",
                    "duration_seconds": duration_seconds,
                },
            )

            codex_result = parse_codex_output(result.stdout)
            codex_model = get_selected_model()
            codex_cost = codex_result.compute_cost(codex_model)
            t = threading.Thread(
                target=record_task_cost,
                args=([prompt], title, codex_cost),
                kwargs={
                    "model": codex_model,
                    "task_id": task_id,
                    "conversation_id": conversation_id,
                },
            )
            t.start()
            t.join(timeout=30)
        except subprocess.TimeoutExpired:
            logger.warning(f"Codex CLI timed out after {timeout // 60} minutes")
        except Exception as e:
            logger.error(f"Error running Codex agent: {e}")
    else:
        run_claude_agent(
            agent,
            prompt,
            task_id,
            title,
            conversation_id,
            timeout,
            cycle,
            feature_title,
        )


def run_claude_agent(
    agent: dict,
    prompt: str,
    task_id: str,
    title: str,
    conversation_id: str,
    timeout: int = 900,
    cycle: bool = False,
    feature_title: str = "",
) -> None:
    """Run Claude Code for a single agent in headless autonomous mode.

    ``prompt``, ``task_id``, ``title``, and ``conversation_id`` are provided
    by the caller (``run_agent``).  ``timeout`` bounds the Claude subprocess
    in seconds.  ``cycle=True`` arms the Stop hook (NINJA_CYCLE_RUN=1).
    """
    agent_logger = setup_logging(agent["name"].lower())

    agent_logger.info(f"\n{'='*60}")
    agent_logger.info(f"{agent['emoji']} Starting {agent['name']} ({agent['role']})")
    agent_logger.info(f"{'='*60}\n")

    custom_headers = build_custom_headers(
        task_id, feature_title or title, conversation_id
    )

    env = {"ANTHROPIC_CUSTOM_HEADERS": custom_headers}
    if cycle:
        env["NINJA_CYCLE_RUN"] = "1"
        CYCLE_STATE_FILE.unlink(missing_ok=True)
        CYCLE_ISSUE_PERMIT_FILE.unlink(missing_ok=True)

    spec = AgentRunConfig(
        prompt=prompt,
        system_prompt_path=SYSTEM_PROMPT_PATH_ORCHESTRATOR,
        tools=["Bash", "Edit", "Read", "Skill", "Write"],
        timeout_seconds=CLAUDE_RUN_ORCHESTRATOR_TIMEOUT_SECONDS,
        session_name="orchestrator",
        cwd=REPO_ROOT,
        env=env,
        process_label="orchestrator",
        title=title,
        task_id=task_id,
    )

    provider = ClaudeProvider()
    claude_started = time.monotonic()
    result = provider.run(spec, agent_logger)
    capture(
        "agent_provider_run_duration",
        {
            "process": "orchestrator",
            "provider": "claude",
            "duration_seconds": round(time.monotonic() - claude_started, 2),
        },
    )

    parsed = result.parsed
    text_output = parsed.text if parsed else result.stdout
    if text_output:
        agent_logger.info(f"Claude output:\n{text_output}")
    if result.stderr:
        agent_logger.warning(f"Claude stderr:\n{result.stderr}")
    if parsed and parsed.is_error:
        agent_logger.error(f"Claude reported error: subtype={parsed.subtype}")
    if parsed and not parsed.is_error and parsed.total_cost_usd > 0:
        t = threading.Thread(
            target=record_task_cost,
            args=([prompt], title, parsed.total_cost_usd),
            kwargs={
                "model": primary_model(parsed),
                "task_id": task_id,
                "conversation_id": conversation_id,
            },
        )
        t.start()
        t.join(timeout=30)

    agent_logger.info(f"\n✅ {agent['name']} completed\n")


def run_capability_tests() -> bool:
    """
    Run all capability tests and report results.

    Returns:
        True if all tests pass, False otherwise
    """
    test_logger = setup_logging("orchestrator")

    test_logger.info("\n" + "=" * 60)
    test_logger.info("🧪 CAPABILITY TESTS")
    test_logger.info("=" * 60)

    results = {}
    all_passed = True

    # Test 1: Config file
    test_logger.info("\n📋 Test 1: Configuration File")
    config = load_config()
    if config.get("default_agent"):
        test_logger.info(f"   ✅ Agent configured: {config.get('default_agent')}")
        results["config"] = True
    else:
        test_logger.error("   ❌ No agent configured")
        results["config"] = False
        all_passed = False

    if config.get("default_channel"):
        test_logger.info(f"   ✅ Channel configured: {config.get('default_channel')}")
    else:
        test_logger.warning("   ⚠️  No default channel configured")

    # Test 2: Browser Server
    test_logger.info("\n📋 Test 2: Browser Server")
    try:
        import urllib.request

        resp = urllib.request.urlopen("http://localhost:9222/json/version", timeout=3)
        if resp.status == 200:
            test_logger.info("   ✅ Browser server running on port 9222")
            results["browser"] = True
        else:
            test_logger.error("   ❌ Browser server not responding")
            results["browser"] = False
            all_passed = False
    except Exception:
        test_logger.warning(
            "   ⚠️  Browser server not running (start with: python ninja/browser_server.py start)"
        )
        results["browser"] = False

    # Test 3: Claude CLI (MANDATORY)
    test_logger.info("\n📋 Test 3: Claude CLI (REQUIRED)")
    if shutil.which("claude"):
        test_logger.info("   ✅ Claude CLI installed")
        results["claude"] = True
    else:
        test_logger.error("   ❌ Claude CLI not installed")
        test_logger.warning("   ⚠️  Claude CLI is REQUIRED to run agents")
        results["claude"] = False
        all_passed = False

    # Test 4: Project Files
    test_logger.info("\n📋 Test 4: Project Files")
    required_files = [
        "slack_interface.py",
        "browser_interface.py",
        "ninja/browser_server.py",
        "ninja/observer.py",
        "ninja/actions.py",
        "agent-docs/NINJA_SPEC.md",
        "agent-docs/AGENT_PROTOCOL.md",
        "agent-docs/SLACK_INTERFACE.md",
        "agent-docs/PIPEDREAM_CONNECT.md",
        "memory",
    ]
    files_ok = True
    for f in required_files:
        path = REPO_ROOT / f
        if path.exists():
            test_logger.info(f"   ✅ {f}")
        else:
            test_logger.error(f"   ❌ {f} missing")
            files_ok = False
            all_passed = False
    results["files"] = files_ok

    # Summary
    test_logger.info("\n" + "=" * 60)
    test_logger.info("📊 TEST SUMMARY")
    test_logger.info("=" * 60)

    for test, passed in results.items():
        if passed is True:
            status = "✅ PASS"
        elif passed is False:
            status = "❌ FAIL"
        else:
            status = "⚠️  SKIP"
        test_logger.info(f"   {test:12} {status}")

    test_logger.info("")
    if all_passed:
        test_logger.info("🎉 All tests passed! Agent is ready to work.")
    else:
        test_logger.warning(
            "⚠️  Some tests failed. Please fix issues before running agent."
        )
    test_logger.info("=" * 60 + "\n")

    return all_passed


def main():
    parser = argparse.ArgumentParser(
        description="Ninja Orchestrator — Browser Automation Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python orchestrator.py                    Run default work loop
  python orchestrator.py --task "Do X"      Run with specific task
  python orchestrator.py --test             Run capability tests

Configuration:
  Agent identity is read from ~/.agent_settings.json
  Set with: python slack_interface.py config --set-agent ninja
        """,
    )
    parser.add_argument("--task", "-t", default="", help="Specific task for the agent")
    parser.add_argument(
        "--list", "-l", action="store_true", help="List all available agents"
    )
    parser.add_argument("--test", action="store_true", help="Run capability tests")

    args = parser.parse_args()

    if not is_orchestrator_enabled():
        print("⏸️ Orchestrator disabled via config — skipping launch", flush=True)
        sys.exit(0)

    if args.test:
        success = run_capability_tests()
        sys.exit(0 if success else 1)

    if args.list:
        list_logger = setup_logging("orchestrator")
        list_logger.info("\n📋 Available Agents:\n")
        for agent_id, agent in AGENTS.items():
            list_logger.info(f"  {agent['emoji']} {agent['name']:8} - {agent['role']}")
        list_logger.info("")

        # Show current config
        config = load_config()
        current = config.get("default_agent", "")
        if current:
            list_logger.info(f"📌 Currently configured: {current}")
        else:
            list_logger.warning(
                "⚠️  No agent configured. Run: python slack_interface.py config --set-agent <name>"
            )
        list_logger.info("")
        return

    # Check for existing instance BEFORE doing anything else
    check_single_instance()

    # Get agent from config first (needed for logging setup)
    agent = get_agent_from_config()

    # Setup logging
    logger.info("=" * 60)
    logger.info(f"Orchestrator starting for {agent['name']}")
    logger.info("=" * 60)

    # Register cleanup handler to remove lock file on exit
    import atexit
    import signal

    atexit.register(remove_lock_file)

    # Start heartbeat thread to keep lock file fresh
    import threading

    heartbeat_stop = threading.Event()

    def heartbeat_loop():
        """Update lock file heartbeat every 60 seconds."""
        while not heartbeat_stop.wait(60):  # Wait 60 seconds or until stopped
            update_heartbeat()
            logger.debug("Heartbeat updated")

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    # Also handle SIGTERM and SIGINT to clean up lock file
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        heartbeat_stop.set()  # Stop heartbeat thread
        remove_lock_file()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Ensure settings.json exists (auto-generate from /root/.claude/settings.json if missing)
    if not ensure_settings_file(logger):
        logger.error("❌ Cannot start without settings.json. Exiting.")
        sys.exit(1)

    # Login to GitHub CLI (skip for local channel — no git repo / mcp-token)
    if resolve_messaging_channel() != "local":
        login_github_cli(logger)

    # Update lock file with agent name
    update_lock_file(agent["name"])

    # Show which agent we're running
    config = load_config()
    logger.info(f"Config: {AGENT_SETTINGS_PATH}")
    logger.info(f"Agent: {agent['name']} ({agent['role']})")
    if config.get("default_channel"):
        logger.info(f"Channel: {config.get('default_channel')}")
    log_file = (
        LOG_DIR / f"{agent['name'].lower()}_{datetime.now().strftime('%Y-%m-%d')}.log"
    )
    logger.info(f"Log file: {log_file}")

    # Run the agent — issue-driven two-phase loop.
    # The monitor (monitor.py) runs as a separate process and feeds the queue by
    # filing GitHub issues and launching this orchestrator when there is open
    # work. This process exits when the cycle completes; systemd Restart will
    # re-invoke it for the next cycle. See agent-docs/LOOP.md.
    if args.task:
        # Explicit operator task: run it directly, bypass the loop phases.
        # When the system operates normally this should not be happening
        logger.info(f"Running explicit task: {args.task}")
        run_agent(agent, args.task)
    else:
        open_issues = count_open_issues()
        num_blocked_issues = count_blocked_issues()
        logger.info(f"Actionable GitHub issues (work queue): {open_issues}")

        # Work + reflect run as ONE Claude invocation (see build_cycle_prompt);
        # empty queue skips the cycle entirely.
        ran_work = open_issues > 0
        # Hook-chained run: one Claude launch works ALL pending cycles
        # (work -> reflect -> next issue ...) — no relaunch between cycles.
        hooks_enabled = is_feature_enabled(STOP_HOOKS_FEATURE_FLAG, default=False)
        if ran_work and hooks_enabled:
            kickoff = build_issues_prompt(open_issues, num_blocked_issues)
            logger.info(
                f"Hook-chained run: work+reflect cycles until the queue "
                f"empties — starting with {open_issues} open issue(s)"
            )
            run_agent(
                agent,
                prompt=kickoff,
                timeout=CYCLE_RUN_TIMEOUT,
                cycle=True,
            )
        elif ran_work:
            logger.info(
                f"Phase 1 (work) and phase 2 (reflect) - a single run: completing {open_issues} open issue(s) and {num_blocked_issues} blocked issue(s)"
            )
            run_agent(
                agent,
                prompt=build_issues_prompt(open_issues, num_blocked_issues),
            )
        else:
            logger.info("No actionable issues — skipping work + reflect phases")


if __name__ == "__main__":
    main()
