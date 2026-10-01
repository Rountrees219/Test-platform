#!/usr/bin/env python3
"""
Health Check — Unified system diagnostics for Ninja.

Checks all critical subsystems in one command:
  - Browser server (port 9222)
  - Messaging channel credentials (via active adapter ABC)
  - GitHub CLI authentication (via 3-tier token resolver)
  - Settings file validity
  - Model configuration
  - Pipedream Connect gateway
  - Claude CLI installation

Usage:
    python tools/health_check.py              # Human-readable output
    python tools/health_check.py --json       # JSON output for scripting
    python tools/health_check.py --fix        # Attempt auto-fix for common issues

Python API:
    from tools.health_check import run_health_check
    results = run_health_check()
    print(results["browser"]["status"])  # "ok" or "error"
"""

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from constants import SANDBOX_METADATA_PATH
from tools.token_health import check_github_token, check_messaging_token
from utils.pipedream import PipedreamClient, PipedreamError

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = REPO_ROOT / "settings.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def check_browser() -> dict:
    """Check if browser server is running on port 9222."""
    try:
        resp = urllib.request.urlopen("http://localhost:9222/json/version", timeout=3)
        if resp.status == 200:
            data = json.loads(resp.read().decode())
            return {
                "status": "ok",
                "message": "Browser server running",
                "browser": data.get("Browser", "unknown"),
                "ws_url": data.get("webSocketDebuggerUrl", ""),
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Browser server not responding: {e}",
            "fix": "python ninja/browser_server.py start",
        }


def check_settings() -> dict:
    """Check settings.json and model configuration."""
    issues = []
    info = {}

    # Check project settings.json
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                settings = json.load(f)
            env = settings.get("env", {})
            info["model"] = env.get("ANTHROPIC_MODEL", "not set")
            info["base_url"] = env.get("ANTHROPIC_BASE_URL", "not set")[:50]
            if not env.get("ANTHROPIC_AUTH_TOKEN"):
                issues.append("No auth token in settings.json")
        except (json.JSONDecodeError, IOError) as e:
            issues.append(f"Cannot read settings.json: {e}")
    else:
        issues.append("settings.json not found (will be auto-generated on start)")

    # Check sandbox metadata for model override
    if SANDBOX_METADATA_PATH.exists():
        try:
            with open(SANDBOX_METADATA_PATH) as f:
                meta = json.load(f)
            selected = meta.get("litellm_selected_model", "")
            if selected:
                info["sandbox_model"] = selected
        except Exception:
            pass

    # Check Claude settings
    if CLAUDE_SETTINGS.exists():
        try:
            with open(CLAUDE_SETTINGS) as f:
                cs = json.load(f)
            claude_model = cs.get("env", {}).get("ANTHROPIC_MODEL", "")
            if claude_model:
                info["claude_model"] = claude_model
        except Exception:
            pass

    if issues:
        return {"status": "warning", "message": "; ".join(issues), **info}
    return {"status": "ok", "message": "Settings valid", **info}


def check_files() -> dict:
    """Check that required project files exist."""
    required = [
        "orchestrator.py",
        "slack_interface.py",
        "browser_interface.py",
        "ninja/browser_server.py",
        "ninja/observer.py",
        "ninja/actions.py",
        "ninja/stealth.py",
        "agent-docs/NINJA_SPEC.md",
        "agent-docs/AGENT_PROTOCOL.md",
        "agent-docs/SLACK_INTERFACE.md",
        "agent-docs/PIPEDREAM_CONNECT.md",
    ]
    missing = [f for f in required if not (REPO_ROOT / f).exists()]

    if missing:
        return {
            "status": "error",
            "message": f"Missing files: {', '.join(missing)}",
            "missing": missing,
        }
    return {"status": "ok", "message": f"All {len(required)} required files present"}


def check_pipedream_health() -> dict:
    """Check Pipedream Connect gateway health endpoint."""
    try:
        result = PipedreamClient().check_health()
        return {
            "status": "ok",
            "message": "Pipedream Connect gateway reachable",
            **result,
        }
    except PipedreamError as exc:
        return {
            "status": "error",
            "message": f"Pipedream Connect gateway returned HTTP {exc.status_code}: {exc.message}",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Pipedream Connect gateway not reachable: {e}",
        }


def check_claude_cli() -> dict:
    """Check if Claude CLI is installed."""
    if shutil.which("claude"):
        return {"status": "ok", "message": "Claude CLI installed"}

    # Check common install locations
    home_path = Path.home() / ".local" / "bin" / "claude"
    if home_path.exists():
        return {"status": "ok", "message": f"Claude CLI found at {home_path}"}

    return {
        "status": "error",
        "message": "Claude CLI not found",
        "fix": "Install Claude Code CLI: https://docs.anthropic.com/en/docs/claude-code",
    }


def run_health_check(auto_fix: bool = False) -> dict:
    """
    Run all health checks and return structured results.

    Args:
        auto_fix: If True, attempt to fix common issues automatically.

    Returns:
        Dict mapping check name to result dict with "status", "message", etc.
    """
    results = {
        "browser": check_browser(),
        "messaging": check_messaging_token(),
        "github": check_github_token(),
        "settings": check_settings(),
        "files": check_files(),
        "pipedream": check_pipedream_health(),
        "claude_cli": check_claude_cli(),
    }

    # Auto-fix if requested
    if auto_fix:
        # Fix 1: Start browser if not running
        if results["browser"]["status"] == "error":
            try:
                subprocess.run(
                    ["python", "ninja/browser_server.py", "start"],
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    timeout=30,
                )
                results["browser"] = check_browser()
                if results["browser"]["status"] == "ok":
                    results["browser"]["fixed"] = True
            except Exception:
                pass

        # Fix 2: Regenerate settings.json
        if results["settings"]["status"] != "ok":
            try:
                sys.path.insert(0, str(REPO_ROOT))
                from processes.orchestrator import ensure_settings_file, setup_logging

                logger = setup_logging("health_check")
                if ensure_settings_file(logger):
                    results["settings"] = check_settings()
                    if results["settings"]["status"] == "ok":
                        results["settings"]["fixed"] = True
            except Exception:
                pass

    # Overall status
    statuses = [r["status"] for r in results.values()]
    if all(s == "ok" for s in statuses):
        results["overall"] = "healthy"
    elif any(s == "error" for s in statuses):
        results["overall"] = "unhealthy"
    else:
        results["overall"] = "degraded"

    return results


def print_results(results: dict):
    """Pretty-print health check results."""
    icons = {"ok": "✅", "warning": "⚠️", "error": "❌"}
    overall_icons = {"healthy": "🟢", "degraded": "🟡", "unhealthy": "🔴"}

    print("\n" + "=" * 60)
    print("🏥 NINJA HEALTH CHECK")
    print("=" * 60)

    for name, result in results.items():
        if name == "overall":
            continue
        status = result.get("status", "unknown")
        icon = icons.get(status, "❓")
        msg = result.get("message", "")
        print(f"\n  {icon} {name:12s} — {msg}")

        # Show fix hint on error
        if status in ("error", "warning") and "fix" in result:
            print(f"     💡 Fix: {result['fix']}")

        # Show extra details
        for key in ("model", "agent", "channel", "browser"):
            if key in result:
                print(f"     {key}: {result[key]}")

        if result.get("fixed"):
            print("     🔧 Auto-fixed!")

    overall = results.get("overall", "unknown")
    icon = overall_icons.get(overall, "❓")
    print(f"\n{'=' * 60}")
    print(f"  {icon} Overall: {overall.upper()}")
    print(f"{'=' * 60}\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Ninja Health Check — unified system diagnostics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/health_check.py            Human-readable output
  python tools/health_check.py --json     JSON output for scripting
  python tools/health_check.py --fix      Auto-fix common issues
        """,
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--fix", action="store_true", help="Attempt auto-fix for common issues"
    )

    args = parser.parse_args()
    results = run_health_check(auto_fix=args.fix)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_results(results)

    # Exit code: 0 if healthy, 1 if not
    sys.exit(0 if results.get("overall") == "healthy" else 1)


if __name__ == "__main__":
    main()
