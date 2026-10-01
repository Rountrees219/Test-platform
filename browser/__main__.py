"""
Ninja — Browser Automation Agent

Entry point: runs Ninja through the orchestrator (Claude Code via claude-wrapper.sh).
Ensures the persistent browser server is running before starting.

Usage:
    python -m browser                          # Default: check Slack, do work
    python -m browser "Go to google.com..."    # Run a specific task
"""

import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents_config import AGENTS
from messaging.factory import resolve_messaging_channel
from processes.orchestrator import (
    ensure_settings_file,
    login_github_cli,
    run_agent,
    setup_logging,
)


def main():
    logger = setup_logging("ninja")

    # Ensure settings are ready
    if not ensure_settings_file(logger):
        logger.error("❌ Cannot start without settings.json. Exiting.")
        sys.exit(1)

    # Skip GitHub CLI login for local channel — no git repo / mcp-token
    if resolve_messaging_channel() != "local":
        login_github_cli(logger)

    # Ensure persistent browser is running before starting the agent
    from browser.browser_server import ensure_running

    if not ensure_running():
        logger.warning(
            "⚠️  Browser server failed to start. Ninja can still start it manually."
        )

    agent = AGENTS["ninja"]

    # If a task was passed as argument, use it
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""

    run_agent(agent, task)


if __name__ == "__main__":
    main()
