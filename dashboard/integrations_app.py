#!/usr/bin/env python3
"""
Ninja Integrations Dashboard  — port 9020

Minimal redirector to the Pipedream Connect UI.

Routes
------
GET  /                 Redirect (302) to a fresh get_connection_link() URL.
                       If misconfigured, returns a plain error page.
GET  /api/status       JSON identity info (external_user_id, ninja_user_id, channel workspace).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, Response, jsonify, redirect
from flask_cors import CORS


# ─── path setup ─────────────────────────────────────────────────────────────
def _find_ninja_src() -> Optional[Path]:
    for c in [
        Path("/workspace/ninja"),
        Path(__file__).parent.parent,
    ]:
        if (c / "utils" / "pipedream.py").exists():
            return c
    return None


_src = _find_ninja_src()
if _src and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from core.config import load_agent_config
from utils.pipedream import PipedreamClient

# ─── Flask app ───────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)


def _settings() -> Dict[str, Any]:
    return load_agent_config()


def _pdx_client():
    """Return a PipedreamClient instance, or raise on misconfiguration."""
    return PipedreamClient()


def _get_connection_link() -> str:
    """Call the Pipedream Connect gateway and return a fresh Connect UI URL."""
    return _pdx_client().get_connection_link()


# ─── Routes ──────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Redirect to a fresh Pipedream Connect UI link."""
    try:
        link = _get_connection_link()
        return redirect(link, code=302)
    except ValueError as e:
        # Misconfiguration — NINJA_USER_ID not set, missing agent_settings fields
        return Response(
            f"<h2>Integrations not configured</h2><pre>{e}</pre>",
            status=400,
            mimetype="text/html",
        )
    except Exception as e:
        return Response(
            f"<h2>Failed to get connection link</h2><pre>{e}</pre>",
            status=502,
            mimetype="text/html",
        )


@app.route("/api/status")
def api_status():
    """Return agent identity info as JSON."""
    s = _settings()
    team_id = s.get("default_team_id", "")
    channel_id = s.get("default_channel_id", "")
    external_user_id = f"{team_id}.{channel_id}" if team_id and channel_id else None
    agent = {
        "team_id": team_id,
        "team_name": s.get("workspace", ""),
        "team_domain": s.get("default_team_domain", ""),
        "channel": s.get("default_channel", ""),
        "channel_id": channel_id,
        "external_user_id": external_user_id,
        "ninja_user_id": s.get("ninja_user_id", "")
        or os.environ.get("NINJA_USER_ID", ""),
    }
    return jsonify({"ok": True, "external_user_id": external_user_id, "agent": agent})


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("INTEGRATIONS_PORT", 9020))
    print(f"🔌 Ninja Integrations Dashboard → http://0.0.0.0:{port}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
