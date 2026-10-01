"""
VNC Integration — Human override via noVNC.

Provides utilities for sharing the VNC link and waiting for human interaction.
noVNC runs on port 6080 via supervisord (websockify → x11vnc, no password, no nginx).
"""

import subprocess

from core.metadata import load_sandbox_metadata

# Port 6080: direct noVNC (websockify → x11vnc, no password)
VNC_PORT = 6080


def get_vnc_url() -> str:
    """
    Get the public noVNC URL for sharing the live browser view.

    Returns the auto-connect URL — no password needed.
    """
    try:
        meta = load_sandbox_metadata()
        sandbox_id = meta["thread_id"]
        stage = meta.get("environment", "")
        prefix = f"{stage}" if stage and stage != "prod" else ""
        return f"https://{VNC_PORT}-{sandbox_id}.app.super.{prefix}myninja.ai/vnc.html?autoconnect=true"
    except (KeyError, Exception):
        return f"http://0.0.0.0:{VNC_PORT}/vnc.html?autoconnect=true"


def share_vnc_link(reason: str = "Browser view available"):
    """Post the VNC link to Slack."""
    vnc_url = get_vnc_url()
    msg = f"🖥️ {reason}\n\nWatch live: {vnc_url}"
    subprocess.run(
        ["python", "slack_interface.py", "say", msg],
        capture_output=True,
    )


def request_human_help(reason: str, page_url: str = ""):
    """
    Post a human help request to Slack with the VNC link.

    Use this when the agent hits a CAPTCHA, login wall, or needs manual input.
    """
    vnc_url = get_vnc_url()
    parts = [
        f"🚨 *Human Help Needed*",
        f"",
        f"*Reason:* {reason}",
    ]
    if page_url:
        parts.append(f"*Page:* {page_url}")
    parts.extend(
        [
            f"",
            f"🖥️ *Open browser:* {vnc_url}",
            f"",
            f"Please complete the action in the browser and reply here when done.",
        ]
    )
    subprocess.run(
        ["python", "slack_interface.py", "say", "\n".join(parts)],
        capture_output=True,
    )
