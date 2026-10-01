#!/usr/bin/env python3
"""
Persistent Browser Server for Ninja.

Launches a Chromium instance with --remote-debugging-port=9222 that persists
across Claude Code sessions. Tabs, cookies, and state survive between tasks.

Usage:
    python ninja/browser_server.py start   # Launch browser (foreground)
    python ninja/browser_server.py status  # Check if running
    python ninja/browser_server.py stop    # Kill the browser
    python ninja/browser_server.py restart # Stop + start

The browser is visible on VNC (DISPLAY=:99) and accessible via CDP at
http://localhost:9222.
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CDP_PORT = 9222
CDP_ENDPOINT = f"http://localhost:{CDP_PORT}"
PID_FILE = Path(__file__).parent / ".browser_server.pid"
BROWSER_DATA_DIR = Path(__file__).parent / "browser_data"
# Psiphon tunnel core runs a local HTTP proxy on this host/port
PSIPHON_HOST = "127.0.0.1"
PSIPHON_PORT = 18080
PSIPHON_PROXY = f"http://{PSIPHON_HOST}:{PSIPHON_PORT}"
DISPLAY = os.environ.get("DISPLAY", ":99")

# Find Chromium binary
# Playwright uses different subdirectory names per architecture:
#   chrome-linux64/  — x86_64 (typical cloud / Linux servers)
#   chrome-linux/    — ARM64  (Apple Silicon Macs running Docker)
CHROMIUM_PATHS = [
    # x86_64 — hardcoded latest known revision + glob fallback
    Path("/root/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"),
    # ARM64 — hardcoded latest known revision + glob fallback
    Path("/root/.cache/ms-playwright/chromium-1208/chrome-linux/chrome"),
    # Glob fallback: any installed revision, both architectures
    *sorted(
        Path("/root/.cache/ms-playwright").glob("chromium-*/chrome-linux64/chrome")
    ),
    *sorted(Path("/root/.cache/ms-playwright").glob("chromium-*/chrome-linux/chrome")),
]


def _find_chromium() -> str:
    """Find the Chromium binary."""
    for p in CHROMIUM_PATHS:
        if p.exists():
            return str(p)
    raise FileNotFoundError("Chromium not found. Run: playwright install chromium")


def _is_running() -> bool:
    """Check if the browser server is running and responsive."""
    try:
        resp = urllib.request.urlopen(f"{CDP_ENDPOINT}/json/version", timeout=3)
        return resp.status == 200
    except Exception:
        return False


def _get_pid() -> int | None:
    """Read PID from file, verify process exists."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Check if alive
        return pid
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return None


def status():
    """Print browser server status."""
    pid = _get_pid()
    running = _is_running()

    if running:
        print(f"✅ Browser server is running (PID: {pid})")
        try:
            resp = urllib.request.urlopen(f"{CDP_ENDPOINT}/json/version", timeout=3)
            info = json.loads(resp.read())
            print(f"   Browser: {info.get('Browser', 'unknown')}")
            print(f"   CDP endpoint: {CDP_ENDPOINT}")
        except Exception:
            pass

        # List open pages
        try:
            resp = urllib.request.urlopen(f"{CDP_ENDPOINT}/json/list", timeout=3)
            pages = json.loads(resp.read())
            print(f"   Open tabs: {len(pages)}")
            for i, page in enumerate(pages):
                title = page.get("title", "")[:60]
                url = page.get("url", "")[:80]
                print(f"     [{i}] {title} — {url}")
        except Exception:
            pass
    elif pid:
        print(f"⚠️  Browser process exists (PID: {pid}) but CDP not responding")
    else:
        print("❌ Browser server is not running")
        print(f"   Start with: python {__file__} start")

    return running


def stop():
    """Stop the browser server."""
    pid = _get_pid()
    if pid:
        print(f"Stopping browser server (PID: {pid})...")
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait up to 5 seconds for graceful shutdown
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except OSError:
                    break
            else:
                # Force kill if still alive
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        PID_FILE.unlink(missing_ok=True)
        print("✅ Browser server stopped")
    else:
        print("Browser server is not running")
        # Clean up any orphaned chromium with our CDP port
        subprocess.run(
            ["pkill", "-f", f"--remote-debugging-port={CDP_PORT}"],
            capture_output=True,
        )


def start(foreground=False):
    """Start the persistent browser server.

    Args:
        foreground: If True, block until the process exits.
                    If False (default), launch in background and return.
    """
    if _is_running():
        print(f"✅ Browser server already running at {CDP_ENDPOINT}")
        status()
        if foreground:
            # In foreground mode, wait on the existing process so supervisord
            # doesn't think we crashed
            existing_pid = _get_pid()
            if existing_pid:
                import signal as _sig

                print(f"   Monitoring existing PID {existing_pid}...")

                def _on_term(s, f):
                    stop()
                    sys.exit(0)

                _sig.signal(_sig.SIGTERM, _on_term)
                _sig.signal(_sig.SIGINT, _on_term)
                # Poll until the process dies
                while True:
                    try:
                        os.kill(existing_pid, 0)
                        time.sleep(5)
                    except OSError:
                        print("⚠️  Browser process died, restarting...")
                        break
                # Process died — fall through to restart
            else:
                return
        else:
            return

    # Kill any stale process
    old_pid = _get_pid()
    if old_pid:
        stop()

    # Clear stale Chrome singleton lock files before launching
    for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lock_path = BROWSER_DATA_DIR / lock_file
        if lock_path.is_symlink() or lock_path.exists():
            lock_path.unlink()
            print(f"   Cleared stale lock: {lock_file}")

    # Also clear any /tmp chromium lock dirs
    import glob

    for tmp_dir in glob.glob("/tmp/org.chromium.Chromium.*"):
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Kill any orphaned chromium processes holding port 9222
    subprocess.run(
        ["pkill", "-9", "-f", f"remote-debugging-port={CDP_PORT}"],
        capture_output=True,
    )
    time.sleep(1)

    chromium = _find_chromium()
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Psiphon proxy (always enabled)
    proxy_server = PSIPHON_PROXY

    args = [
        chromium,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={BROWSER_DATA_DIR}",
        f"--display={DISPLAY}",
        # Standard Playwright flags for compatibility
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-breakpad",
        "--disable-component-extensions-with-background-pages",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-hang-monitor",
        "--disable-ipc-flooding-protection",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-renderer-backgrounding",
        "--disable-sync",
        "--disable-infobars",
        "--disable-search-engine-choice-screen",
        "--enable-features=CDPScreenshotNewSurface",
        "--force-color-profile=srgb",
        "--metrics-recording-only",
        "--no-first-run",
        "--password-store=basic",
        "--use-mock-keychain",
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu",
        "--disable-gpu-sandbox",
        "--enable-unsafe-swiftshader",
        "--ignore-certificate-errors",
        "--window-size=1600,900",
        # Start with a blank tab
        "about:blank",
    ]

    # Add proxy if configured
    if proxy_server:
        args.insert(-1, f"--proxy-server={proxy_server}")
        print(f"   Proxy: {proxy_server}")

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    if foreground:
        print(f"🚀 Starting browser server (foreground) on {CDP_ENDPOINT}...")
        proc = subprocess.Popen(args, env=env)
        PID_FILE.write_text(str(proc.pid))
        print(f"   PID: {proc.pid}")
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
    else:
        print(f"🚀 Starting browser server on {CDP_ENDPOINT}...")
        proc = subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        PID_FILE.write_text(str(proc.pid))
        print(f"   PID: {proc.pid}")

        # Wait for CDP to become responsive
        for i in range(30):
            if _is_running():
                print(f"✅ Browser server ready at {CDP_ENDPOINT}")
                return
            time.sleep(0.5)

        print(
            "⚠️  Browser started but CDP not responding yet. Check with: "
            f"python {__file__} status"
        )


def ensure_running():
    """Ensure the browser server is running. Start it if not.

    Returns:
        True if browser is running (or was started), False on failure.
    """
    if _is_running():
        return True
    print("Browser server not running, starting...")
    start(foreground=False)
    return _is_running()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "start":
        fg = "--foreground" in sys.argv or "-f" in sys.argv
        start(foreground=fg)
    elif cmd == "stop":
        stop()
    elif cmd == "restart":
        stop()
        time.sleep(1)
        start()
    elif cmd == "status":
        status()
    elif cmd == "ensure":
        ensure_running()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: start | stop | restart | status | ensure")
        sys.exit(1)


if __name__ == "__main__":
    main()
