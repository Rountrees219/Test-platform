#!/usr/bin/env python3
"""Queued-ack watcher.

Runs as an always-on daemon so it can reply even while an agent turn is blocked.
When BUSY_MARKER is present and new inbound messages arrive, sends ONE rate-limited
"message queued" ack so the operator isn't left hanging during long turns. The agent
answers for real on its next turn.

Run:  python queued_ack.py            (daemon)
      python queued_ack.py --once --dry-run   (single test iteration, no send)
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

NINJA_SRC = Path(__file__).resolve().parent

BUSY_MARKER = Path("/workspace/.ninja-turn-busy")
GROUP_JID = os.environ.get("NINJA_GROUP_JID", os.environ.get("PHANTOM_GROUP_JID", ""))
POLL_SEC = float(os.environ.get("QUEUED_ACK_POLL_SEC", "4"))
COOLDOWN_SEC = float(os.environ.get("QUEUED_ACK_COOLDOWN_SEC", "45"))
MIN_BUSY_SEC = float(os.environ.get("QUEUED_ACK_MIN_BUSY_SEC", "20"))
ACK_TEXT = (
    "message queued — I'm mid-task right now; I'll reply properly as soon as "
    "I'm free."
)

CLI = ["python3", "messaging/whatsapp/interface.py"]
ENV = {**os.environ, "PYTHONPATH": str(NINJA_SRC)}


def _run(cmd, timeout=25):
    return subprocess.run(
        cmd,
        cwd=str(NINJA_SRC),
        env=ENV,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def peek_latest_seq():
    """Return (latest_seq, inbox_epoch) without consuming the agent's cursor."""
    try:
        out = _run(CLI + ["read", "--since", "0", "--no-save", "--json"]).stdout
        idx = out.find("{")
        if idx < 0:
            return None, None
        d = json.loads(out[idx:])
        return d.get("latest_seq"), d.get("inbox_epoch")
    except Exception:
        return None, None


def send_ack(dry_run=False):
    if dry_run:
        print(f"[dry-run] would send: {ACK_TEXT}")
        return
    # Empty --group-jid strips to "" and whatsapp_interface falls back to DM
    # routing (WHATSAPP_TO / allowlist), which can send the ack to an unrelated
    # contact. Skip silently when the group JID isn't configured.
    jid = (GROUP_JID or "").strip()
    if not jid:
        print("queued_ack: NINJA_GROUP_JID/PHANTOM_GROUP_JID not set; skipping ack")
        return
    try:
        _run(CLI + ["say", ACK_TEXT, "--ninja-prefix", "--group-jid", jid])
    except Exception as e:  # never let a send error kill the daemon
        print(f"queued_ack: send failed: {e}")


def busy_since():
    """Seconds the current turn has been busy, or None if not busy."""
    try:
        if not BUSY_MARKER.exists():
            return None
        return max(0.0, time.time() - BUSY_MARKER.stat().st_mtime)
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single iteration")
    ap.add_argument("--dry-run", action="store_true", help="log instead of sending")
    args = ap.parse_args()

    baseline_seq = None
    baseline_epoch = None
    last_ack = 0.0
    prev_busy = False

    while True:
        secs = busy_since()
        busy = secs is not None
        latest, epoch = peek_latest_seq()

        if busy and not prev_busy:
            baseline_seq, baseline_epoch = latest, epoch
        if not busy:
            baseline_seq, baseline_epoch = None, None
        prev_busy = busy

        should = (
            busy
            and secs is not None
            and secs >= MIN_BUSY_SEC
            and latest is not None
            and baseline_seq is not None
            and epoch == baseline_epoch
            and latest > baseline_seq
            and (time.time() - last_ack) >= COOLDOWN_SEC
        )
        if should:
            send_ack(dry_run=args.dry_run)
            last_ack = time.time()
            baseline_seq = latest
        elif args.once:
            print(
                f"busy={busy} secs={secs} latest={latest} baseline={baseline_seq} "
                f"epoch_match={epoch == baseline_epoch} -> ack={should}"
            )

        if args.once:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
