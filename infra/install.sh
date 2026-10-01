#!/usr/bin/env bash
# install.sh — Setup script for Ninja browser automation agent
#
# Usage:
#   ./install.sh --messaging-channel slack --channel "#my-channel" --channel-id "C0AAAAMBR1R"
#   ./install.sh --messaging-channel whatsapp
#   ./install.sh --messaging-channel teams --teams-id "TEAM_ID" --team-name "TEAM_NAME" --channel-id "CHANNEL_ID" --channel-name "CHANNEL_NAME"
#
# What this does:
#   1.   Installs Python dependencies (requirements.txt)
#   1.5. Installs pdx CLI
#   1.6. Installs the gh → Gitea shim
#   2.   Creates the logs directory + writes MESSAGING_CHANNEL to /etc/environment
#   3.   Runs channel-specific setup (install/<channel>.sh)
#   4.   Installs and enables systemd services
#   5.   Configures VNC (removes password)
#   6.   Waits for browser server to be ready

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

MESSAGING_CHANNEL=""
CHANNEL_ARGS=()   # forwarded to the channel-specific install script
USER_TIMEZONE=""  # optional IANA zone from install CLI (Teams/WhatsApp)

SUPPORTED_CHANNELS=("slack" "whatsapp" "teams" "local")

usage() {
    echo "Usage: $0 --messaging-channel <slack|whatsapp|teams|local> [channel-specific options]"
    echo ""
    echo "Options:"
    echo "  --messaging-channel CHANNEL   Messaging channel (required: slack|whatsapp|teams|local)"
    echo "  --channel CHANNEL             Channel name — passed to channel script"
    echo "  --channel-id CHANNEL_ID       Channel ID — passed to channel script"
    echo "  --chat-jid JID                WhatsApp JID (optional)"
    echo "  --teams-id TEAM_ID            Microsoft Teams team ID — passed to channel script"
    echo "  --team-name TEAM_NAME         Microsoft Teams team display name — passed to channel script (optional)"
    echo "  --channel-name CHANNEL_NAME   Microsoft Teams channel display name — passed to channel script (optional)"
    echo "  --workspace-id WORKSPACE_ID   Workspace ID — passed to channel script (optional)"
    echo "  --user-timezone TIMEZONE      IANA timezone (optional; Teams/WhatsApp install)"
    echo "  --help                        Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --messaging-channel slack --channel '#my-channel' --channel-id 'C0AAAAMBR1R'"
    echo "  $0 --messaging-channel whatsapp"
    echo "  $0 --messaging-channel teams --teams-id 'TEAM_ID' --team-name 'TEAM_NAME' --channel-id 'CHANNEL_ID' --channel-name 'CHANNEL_NAME'"
    echo "  $0 --messaging-channel local"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --messaging-channel) MESSAGING_CHANNEL="$2"; shift 2 ;;
        --channel|--channel-id|--channel-name|--workspace-id|--chat-jid|--teams-id|--team-name)
            CHANNEL_ARGS+=("$1" "$2"); shift 2 ;;
        --user-timezone) USER_TIMEZONE="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ -z "$MESSAGING_CHANNEL" ]]; then
    echo "ERROR: --messaging-channel is required"
    usage
    exit 1
fi

if [[ ! " ${SUPPORTED_CHANNELS[*]} " =~ " ${MESSAGING_CHANNEL} " ]]; then
    echo "ERROR: unsupported channel '${MESSAGING_CHANNEL}'. Choose from: ${SUPPORTED_CHANNELS[*]}"
    exit 1
fi

echo "=== Ninja Browser Automation — Setup (channel: ${MESSAGING_CHANNEL}) ==="
echo ""

# ---------------------------------------------------------------------------
# Step 1: Python dependencies
# ---------------------------------------------------------------------------
echo "▶ Installing Python dependencies..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"
echo "  ✓ Python packages installed"

# Ensure the ninja package is importable by adding its parent to PYTHONPATH
NINJA_PARENT="$(cd "$SCRIPT_DIR/.." && pwd)"
if ! grep -q "$NINJA_PARENT" /etc/environment 2>/dev/null; then
    echo "PYTHONPATH=\"${NINJA_PARENT}:\${PYTHONPATH:-}\"" >> /etc/environment
fi
export PYTHONPATH="${NINJA_PARENT}:${PYTHONPATH:-}"
echo "  ✓ PYTHONPATH configured (${NINJA_PARENT})"

# ---------------------------------------------------------------------------
# Step 1.5: Install pdx CLI (Pipedream LLM wrapper)
# ---------------------------------------------------------------------------
PDX_SRC="$SCRIPT_DIR/bin/pdx"
PDX_DST="/usr/local/bin/pdx"
if [[ -f "$PDX_SRC" ]]; then
    chmod +x "$PDX_SRC"
    ln -sf "$PDX_SRC" "$PDX_DST"
    echo "  ✓ pdx CLI installed → $PDX_DST"
else
    echo "  ⚠ bin/pdx not found — skipping pdx install"
fi

# ---------------------------------------------------------------------------
# Step 1.6: Install the gh → Gitea shim
# ---------------------------------------------------------------------------
# Re-point /usr/local/bin/gh at the live tree. ninja-install.sh installs the
# shim from its staging dir, which ninja-upgrade never refreshes; pointing at
# /workspace/ninja instead is what lets a dist patch the shim.
#
# Safe on a GitHub sandbox: the shim execs the real gh with no Gitea configured.
GH_SHIM_SRC="$(cd "$SCRIPT_DIR/.." && pwd)/tools/gh_shim.py"
GH_SHIM_DST="/usr/local/bin/gh"
REAL_GH="/usr/bin/gh"
if [[ ! -f "$GH_SHIM_SRC" ]]; then
    echo "  ⚠ tools/gh_shim.py not found — leaving $GH_SHIM_DST alone"
elif [[ ! -x "$REAL_GH" && ! -f /gitea-cred/gitea.env && -z "${GITEA_URL:-}" ]]; then
    # Shadowing gh needs either a real binary to fall back to or a Gitea that
    # makes the fail-safe unreachable. Neither holds here.
    echo "  ⚠ no gh at $REAL_GH and no Gitea credentials — skipping gh shim"
else
    chmod +x "$GH_SHIM_SRC"
    ln -sf "$GH_SHIM_SRC" "$GH_SHIM_DST"
    echo "  ✓ gh shim installed → $GH_SHIM_DST (real gh: $REAL_GH)"
fi

# ---------------------------------------------------------------------------
# Step 1.7: Bootstrap Claude skills catalog
# ---------------------------------------------------------------------------
echo "▶ Bootstrapping Claude skills catalog..."
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
SKILLS_ZIP="$SCRIPT_DIR/../skills.zip"        # src/ninja/skills.zip (SCRIPT_DIR = infra/)
if [[ -f "$SKILLS_ZIP" ]]; then
    command -v unzip >/dev/null 2>&1 || { apt-get update && apt-get install -y unzip; }
    mkdir -p "$CLAUDE_HOME"
    # -o so re-runs (upgrades) refresh the catalog; strip macOS cruft.
    unzip -o -q "$SKILLS_ZIP" -d "$CLAUDE_HOME" -x "__MACOSX/*" "*/.DS_Store"
    # memory/ is Codex-only; Claude uses system-prompt memory paths instead.
    rm -rf "$CLAUDE_HOME/skills/memory"
    echo "  ✓ Skills unpacked → $CLAUDE_HOME/skills"
else
    echo "  ⚠ skills.zip not found at $SKILLS_ZIP — skipping (Skill catalog will be empty)"
fi

# ---------------------------------------------------------------------------
# Step 1.7: Bootstrap Codex skills catalog
# ---------------------------------------------------------------------------
echo "▶ Bootstrapping Codex skills catalog..."
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
if [[ -f "$SKILLS_ZIP" ]]; then
    mkdir -p "$CODEX_HOME"
    unzip -o -q "$SKILLS_ZIP" -d "$CODEX_HOME" -x "__MACOSX/*" "*/.DS_Store"
    echo "  ✓ Skills unpacked → $CODEX_HOME/skills"
else
    echo "  ⚠ skills.zip not found at $SKILLS_ZIP — skipping Codex skills"
fi

# ---------------------------------------------------------------------------
# Step 2: Log directory + MESSAGING_CHANNEL → /etc/environment
# ---------------------------------------------------------------------------
mkdir -p /workspace/logs
echo "  ✓ Log directory ready (/workspace/logs)"

# Log rotation: the systemd units append to /workspace/logs/*.log with no
# rotation, so a repeated line can grow a log to GBs and fill the disk. Drop the
# logrotate config and let the stock daily logrotate run it (rotate 5,
# copytruncate to keep systemd's open fd valid). logrotate itself is baked into
# the sandbox image, but the overlay does not enable its timer, so enable it.
if command -v logrotate >/dev/null 2>&1; then
    cp "$SCRIPT_DIR/logrotate/ninja-logs" /etc/logrotate.d/ninja-logs
    echo "  ✓ logrotate config installed (/etc/logrotate.d/ninja-logs, daily x5)"
    if systemctl list-unit-files | grep -q '^logrotate\.timer'; then
        systemctl enable --now logrotate.timer >/dev/null 2>&1 || true
        echo "  ✓ stock logrotate.timer enabled (daily)"
    else
        echo "  ⚠ logrotate.timer not present — relying on cron.daily"
    fi
else
    echo "  ⚠ logrotate not found — /workspace/logs/*.log will not be rotated"
fi

# Write MESSAGING_CHANNEL so all systemd services inherit the correct adapter.
if grep -q "^MESSAGING_CHANNEL=" /etc/environment 2>/dev/null; then
    sed -i "s/^MESSAGING_CHANNEL=.*/MESSAGING_CHANNEL=${MESSAGING_CHANNEL}/" /etc/environment
else
    echo "MESSAGING_CHANNEL=${MESSAGING_CHANNEL}" >> /etc/environment
fi
export MESSAGING_CHANNEL
echo "  ✓ Messaging channel: ${MESSAGING_CHANNEL}"

# ---------------------------------------------------------------------------
# Step 3: Channel-specific setup
# ---------------------------------------------------------------------------
CHANNEL_INSTALL="$SCRIPT_DIR/install/${MESSAGING_CHANNEL}.sh"
if [[ ! -f "$CHANNEL_INSTALL" ]]; then
    echo "❌ No install script found for channel: ${MESSAGING_CHANNEL}"
    echo "   Expected: $CHANNEL_INSTALL"
    exit 1
fi

if [[ -n "$USER_TIMEZONE" ]]; then
    export USER_TIMEZONE
fi
bash "$CHANNEL_INSTALL" "${CHANNEL_ARGS[@]}"

# ---------------------------------------------------------------------------
# Step 4: Systemd services (channel-specific)
# ---------------------------------------------------------------------------
echo ""
echo "▶ Installing systemd services..."
cp "$SCRIPT_DIR/systemd/ninja-sync.service"         /etc/systemd/system/ninja-sync.service
cp "$SCRIPT_DIR/systemd/ninja.service"              /etc/systemd/system/ninja.service
cp "$SCRIPT_DIR/systemd/ninja-dashboard.service"    /etc/systemd/system/ninja-dashboard.service
cp "$SCRIPT_DIR/systemd/ninja-integrations.service" /etc/systemd/system/ninja-integrations.service

if [[ "$MESSAGING_CHANNEL" == "whatsapp" ]]; then
    # Copy from systemd/whatsapp/ — installed filenames unchanged so
    # journalctl / wipe-state.sh / docs keep working.
    cp "$SCRIPT_DIR/systemd/whatsapp/ninja-whatsapp-gateway.service" /etc/systemd/system/ninja-whatsapp-gateway.service
    cp "$SCRIPT_DIR/systemd/whatsapp/ninja-whatsapp-monitor.service" /etc/systemd/system/ninja-whatsapp-monitor.service
    if [[ -f "$SCRIPT_DIR/systemd/whatsapp/ninja-queued-ack.service" ]]; then
        cp "$SCRIPT_DIR/systemd/whatsapp/ninja-queued-ack.service" /etc/systemd/system/ninja-queued-ack.service
    fi

    # systemd ignores /etc/environment — inject channel for dashboard badge/mode
    # and for the integrations service (else its factory defaults to "slack").
    for svc in ninja-dashboard ninja-integrations; do
        mkdir -p "/etc/systemd/system/${svc}.service.d"
        printf '[Service]\nEnvironment=MESSAGING_CHANNEL=%s\n' "$MESSAGING_CHANNEL" \
            > "/etc/systemd/system/${svc}.service.d/channel.conf"
    done

    systemctl disable --now ninja-monitor.service 2>/dev/null || true
    systemctl disable --now ninja-health.service 2>/dev/null || true

    systemctl daemon-reload
    systemctl enable ninja-sync.service ninja.service \
        ninja-whatsapp-gateway.service ninja-whatsapp-monitor.service \
        ninja-dashboard.service ninja-integrations.service
    systemctl restart ninja-sync.service ninja.service \
        ninja-whatsapp-gateway.service ninja-whatsapp-monitor.service \
        ninja-dashboard.service ninja-integrations.service

    if command -v supervisorctl >/dev/null 2>&1; then
        supervisorctl reread >/dev/null 2>&1 || true
        supervisorctl update >/dev/null 2>&1 || true
        if supervisorctl status whatsapp_gateway >/dev/null 2>&1; then
            supervisorctl restart whatsapp_gateway >/dev/null 2>&1 || true
        fi
        if supervisorctl status whatsapp_gateway 2>/dev/null | grep -q "RUNNING"; then
            systemctl disable --now ninja-whatsapp-gateway.service 2>/dev/null || true
            echo "  ⚠ supervisord runs the gateway — disabled systemd gateway unit"
        fi
    fi

    echo "  ✓ ninja-whatsapp-gateway.service installed, enabled and started"
    echo "  ✓ ninja-whatsapp-monitor.service installed, enabled and started"
    echo "  ✓ ninja-dashboard.service installed, enabled and started (port 9000 — /whatsapp panel)"
    echo "  ✓ ninja-integrations.service installed, enabled and started (port 9020)"
else
    cp "$SCRIPT_DIR/systemd/ninja-monitor.service" /etc/systemd/system/ninja-monitor.service
    cp "$SCRIPT_DIR/systemd/ninja-health.service"  /etc/systemd/system/ninja-health.service

    # systemd ignores /etc/environment, so inject MESSAGING_CHANNEL into the units
    # that build a messaging interface (else they fall back to the "slack" default).
    for svc in ninja-monitor ninja-health ninja-dashboard ninja-integrations; do
        mkdir -p "/etc/systemd/system/${svc}.service.d"
        printf '[Service]\nEnvironment=MESSAGING_CHANNEL=%s\n' "$MESSAGING_CHANNEL" \
            > "/etc/systemd/system/${svc}.service.d/channel.conf"
    done

    systemctl daemon-reload

    if [[ "$MESSAGING_CHANNEL" == "local" ]]; then
        # Local canary — no git repo, skip ninja-sync (it waits for mcp-token).
        systemctl disable --now ninja-sync.service 2>/dev/null || true
        systemctl enable ninja.service ninja-monitor.service \
            ninja-dashboard.service ninja-integrations.service ninja-health.service
        systemctl start  ninja.service ninja-monitor.service \
            ninja-dashboard.service ninja-integrations.service ninja-health.service
        echo "  ✓ ninja-sync.service skipped (no git repo in local mode)"
    else
        systemctl enable ninja-sync.service ninja.service ninja-monitor.service \
            ninja-dashboard.service ninja-integrations.service ninja-health.service
        systemctl start  ninja-sync.service ninja.service ninja-monitor.service \
            ninja-dashboard.service ninja-integrations.service ninja-health.service
        echo "  ✓ ninja-sync.service installed, enabled and started"
    fi
    echo "  ✓ ninja.service installed and enabled (single work cycle, restarts on failure)"
    echo "  ✓ ninja-monitor.service installed, enabled and started (continuous messaging channel watcher)"
    echo "  ✓ ninja-dashboard.service installed, enabled and started (port 9000)"
    echo "  ✓ ninja-integrations.service installed, enabled and started (port 9020)"
    echo "  ✓ ninja-health.service installed, enabled and started (periodic credential and dependency health checks)"
fi

# ---------------------------------------------------------------------------
# Step 5: VNC password-free configuration
# ---------------------------------------------------------------------------
echo ""
echo "▶ Configuring VNC (removing password requirement)..."

SUPERVISOR_CONF="/etc/supervisor/conf.d/supervisord.conf"

if [[ -f "$SUPERVISOR_CONF" ]]; then
    sed -i 's|x11vnc -display :99 -forever -shared -rfbauth /root/.vnc/passwd -rfbport 5901|x11vnc -display :99 -forever -shared -nopw -rfbport 5901|g' "$SUPERVISOR_CONF"
    supervisorctl reread
    supervisorctl update
    supervisorctl restart x11vnc
    echo "  ✓ VNC configured to run without password (-nopw)"
    echo "  ✓ x11vnc restarted with new config"
else
    echo "  ⚠ Supervisor config not found at $SUPERVISOR_CONF — skipping VNC patch"
fi

# ---------------------------------------------------------------------------
# Step 6: Wait for browser server to be ready
# ---------------------------------------------------------------------------
echo ""
echo "▶ Waiting for browser server to be ready on port 9222..."
BROWSER_TIMEOUT=60
BROWSER_READY=false
for i in $(seq 1 "$BROWSER_TIMEOUT"); do
    if curl -sf http://localhost:9222/json/version >/dev/null 2>&1; then
        BROWSER_READY=true
        echo "  ✓ Browser server ready (${i}s)"
        break
    fi
    sleep 1
done

if [[ "$BROWSER_READY" == "false" ]]; then
    echo "  ⚠ Browser not responding after ${BROWSER_TIMEOUT}s — attempting manual start..."
    python "$SCRIPT_DIR/../browser/browser_server.py" start || true
    sleep 5
    if curl -sf http://localhost:9222/json/version >/dev/null 2>&1; then
        echo "  ✓ Browser started successfully"
    else
        echo "  ⚠ Browser could not be started — health check may still fail"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "=== Setup complete ==="
echo ""
echo "Useful commands:"
echo "  systemctl status <service_name>             # Check service status"
echo "  journalctl -u <service_name> -f             # Follow service logs"
echo "  Dashboard: http://localhost:9000"
