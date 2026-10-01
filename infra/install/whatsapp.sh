#!/usr/bin/env bash
# install/whatsapp.sh — WhatsApp-specific installation steps.
#
# Called by install.sh after the common steps complete.
# Handles: agent_settings seed, optional bound.json pre-seed, gateway npm build.
#
# Usage:
#   bash install/whatsapp.sh --channel "Ops Bridge" --chat-jid "120363...@g.us"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NINJA_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
GATEWAY_DIR="$NINJA_DIR/messaging/whatsapp/gateway"

WA_LABEL=""
WA_CHAT_JID=""

usage() {
    echo "Usage: $0 [--channel LABEL] [--chat-jid JID]"
    echo "  --channel LABEL   Human-readable label stored in ~/.agent_settings.json"
    echo "  --chat-jid JID    Optional; pair via Dashboard /whatsapp if omitted"
    echo "  --help            Show this help message"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --channel)   WA_LABEL="$2"; shift 2 ;;
        --chat-jid)  WA_CHAT_JID="$2"; shift 2 ;;
        --help|-h)   usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Step 1: Align system timezone with browser timezone from sandbox metadata
# ---------------------------------------------------------------------------
echo ""
echo "▶ Aligning system timezone with browser timezone..."
TZ_SCRIPT="$NINJA_DIR/infra/initial_setup_scripts/set_timezone.py"
if [[ -f "$TZ_SCRIPT" ]]; then
    if [[ -n "${USER_TIMEZONE:-}" ]]; then
        TZ_CMD=(python3 "$TZ_SCRIPT" --timezone "$USER_TIMEZONE" --quiet)
    else
        TZ_CMD=(python3 "$TZ_SCRIPT" --channel whatsapp --quiet)
    fi
    if "${TZ_CMD[@]}" >/dev/null; then
        CURRENT_TZ="$(cat /etc/timezone 2>/dev/null || readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##')"
        echo "  ✓ Timezone: ${CURRENT_TZ:-unknown}"
    else
        echo "  ⚠ set_timezone.py exited non-zero — continuing with current system timezone."
    fi
else
    echo "  ⚠ ${TZ_SCRIPT} not found — skipping timezone sync."
fi

echo ""
echo "▶ Configuring WhatsApp (Ninja mode)..."

WA_LABEL="$WA_LABEL" WA_JID="$WA_CHAT_JID" python - <<'PY'
import json, os
from pathlib import Path

p = Path.home() / ".agent_settings.json"
data = {}
if p.exists():
    try:
        data = json.loads(p.read_text())
    except Exception:
        data = {}

# Clear stale "mode" — MESSAGING_CHANNEL env is the sole channel selector;
# keeping this key lets settings.json shadow the env if the reader ever reads it.
data.pop("mode", None)
data.setdefault("default_agent", "ninja")
wa = data.get("whatsapp") if isinstance(data.get("whatsapp"), dict) else {}
label = os.environ.get("WA_LABEL", "").strip()
jid = os.environ.get("WA_JID", "").strip()
if label:
    wa["channel_label"] = label
if jid:
    wa["bound_chat_jid"] = jid
data["whatsapp"] = wa
p.write_text(json.dumps(data, indent=2))
print(f"  settings written: {p}")
PY
echo "  ✓ ~/.agent_settings.json updated (channel=whatsapp)"

if [[ -n "$WA_CHAT_JID" ]]; then
    WA_AUTH_DIR="$GATEWAY_DIR/auth/default"
    mkdir -p "$WA_AUTH_DIR"
    # bound_at is a millisecond epoch — the gateway loader parses this via
    # Number(parsed.bound_at) (wa-bind.ts). An ISO-8601 string parses to NaN
    # and loses the install-time timestamp.
    cat > "$WA_AUTH_DIR/bound.json" <<JSON
{
  "chat_jid": "$WA_CHAT_JID",
  "bound_via": "install",
  "bound_at": $(($(date +%s) * 1000))
}
JSON
    echo "  ✓ Pre-seeded bound chat: $WA_CHAT_JID"
else
    echo "  ℹ No --chat-jid provided — pair via the dashboard /whatsapp panel."
fi

if [[ -f "$GATEWAY_DIR/package.json" ]]; then
    echo ""
    echo "▶ Installing WhatsApp gateway Node deps..."
    (cd "$GATEWAY_DIR" && npm ci --silent) || \
        (cd "$GATEWAY_DIR" && npm install --silent)
    echo "  ✓ WhatsApp gateway deps installed"
    echo "▶ Building WhatsApp gateway TypeScript..."
    (cd "$GATEWAY_DIR" && npm run build --silent)
    echo "  ✓ WhatsApp gateway built (dist/server.js ready)"
else
    echo "  ⚠ $GATEWAY_DIR/package.json not found — skipping gateway npm build"
fi

WIPE_SH="$GATEWAY_DIR/wipe-state.sh"
WIPE_PY="$GATEWAY_DIR/10_wipe_whatsapp_state.py"
if [[ -f "$WIPE_SH" && -f "$WIPE_PY" ]]; then
    mkdir -p /workspace/.agent_hooks/publish
    install -D -m 755 "$WIPE_SH" \
        /workspace/.agent_hooks/publish/wipe-state.sh
    install -D -m 755 "$WIPE_PY" \
        /workspace/.agent_hooks/publish/10_wipe_whatsapp_state.py
    echo "  ✓ pre-publish wipe hook installed (/workspace/.agent_hooks/publish/10_wipe_whatsapp_state.py)"
elif [[ -f "$WIPE_SH" ]]; then
    mkdir -p /workspace/.agent_hooks/publish
    install -D -m 755 "$WIPE_SH" \
        /workspace/.agent_hooks/publish/wipe-state.sh
    echo "  ⚠ wipe .py shim missing — installed wipe-state.sh only (will not run via run_all_hooks)"
fi
