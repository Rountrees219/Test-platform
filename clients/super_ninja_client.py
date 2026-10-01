"""
Sandbox environment helpers.

Exposes the running environment name and derived URLs by reading sandbox
metadata via ``core.metadata.load_sandbox_metadata()``.
"""

from core.metadata import load_sandbox_metadata


def get_thread_id() -> str | None:
    """Return the sandbox thread ID, or None if unavailable."""
    return load_sandbox_metadata().get("thread_id")


def get_project_id() -> str | None:
    """Return the SuperNinja project ID, or None if unavailable."""
    return load_sandbox_metadata().get("project_id")


def get_super_ninja_url() -> str:
    """Return the URL where client can buy more credit, e.g. https://super.myninja.ai/"""
    metadata = load_sandbox_metadata()
    env = metadata.get("environment", "")
    prefix = env if env and env != "prod" else ""
    return f"https://super.{prefix}myninja.ai"
