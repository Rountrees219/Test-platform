#!/usr/bin/env bash
# Pre-publish hook: scrub publisher Baileys auth + cross-tenant leftovers before Firecracker snapshot.

set -euo pipefail

echo "▶ wipe-whatsapp-state: scrubbing Baileys auth + bind state + runtime leftovers"

systemctl stop ninja-whatsapp-gateway.service \
               ninja-whatsapp-monitor.service 2>/dev/null || true

WA_AUTH_DIR="/workspace/ninja/messaging/whatsapp/gateway/auth/default"
if [[ -d "$WA_AUTH_DIR" ]]; then
    find "$WA_AUTH_DIR" -mindepth 1 -delete
    echo "  ✓ $WA_AUTH_DIR emptied"
fi

rm -f "$WA_AUTH_DIR/bound.json" 2>/dev/null || true
rm -f /workspace/ninja/messaging/whatsapp/gateway/auth/bound.json 2>/dev/null || true

rm -rf /workspace/ninja/messaging/whatsapp/gateway/media 2>/dev/null || true

NINJA_WA_RUNTIME_DIR="/root/.ninja_whatsapp"
rm -rf "$NINJA_WA_RUNTIME_DIR" 2>/dev/null || true
echo "  ✓ $NINJA_WA_RUNTIME_DIR removed"

# Legacy runtime paths (pre-.ninja_whatsapp consolidation)
rm -rf /tmp/ninja-wa-media /tmp/phantom-wa-media /tmp/wa-media 2>/dev/null || true
rm -f /root/ninja-wa-recent-messages.json /root/ninja-wa-recent-messages.json.lock 2>/dev/null || true
rm -f /root/.rm_*.json 2>/dev/null || true
rm -rf /workspace/ninja/services/whatsapp/memory 2>/dev/null || true

rm -f /root/.config/gh/hosts.yml 2>/dev/null || true

rm -f /workspace/logs/whatsapp-*.log 2>/dev/null || true
rm -f /workspace/logs/monitor-*.log 2>/dev/null || true
rm -f /workspace/logs/ninja-whatsapp-*.log 2>/dev/null || true
rm -f /workspace/logs/phantom-whatsapp-*.log 2>/dev/null || true
rm -f /workspace/logs/ninja_*.log 2>/dev/null || true
rm -f /workspace/outputs/*.txt 2>/dev/null || true

python3 - <<'PY'
import json, os, sys
p = os.path.expanduser("~/.agent_settings.json")
if not os.path.exists(p):
    sys.exit(0)
try:
    with open(p) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
d["whatsapp"] = {}
with open(p, "w") as f:
    json.dump(d, f, indent=2)
print("  ✓ ~/.agent_settings.json scrubbed (kept mode=whatsapp, cleared whatsapp block)")
PY

for env_file in /etc/systemd/system/ninja-whatsapp-gateway.service.d/*.conf \
                /workspace/ninja/messaging/whatsapp/gateway/.env; do
    if [[ -f "$env_file" ]]; then
        sed -i.bak '/^WHATSAPP_ALLOWED_CHAT_JID=/d; /^WHATSAPP_ALLOWED_TO=/d' "$env_file" 2>/dev/null || true
        rm -f "${env_file}.bak" 2>/dev/null || true
    fi
done

echo "  ✓ wipe-whatsapp-state complete — installer will see a fresh QR on first boot"
