"""SmokeChecker — verdict on whether a freshly-applied upgrade is healthy.

``check()`` takes the pre-upgrade health snapshot to diff against. Every
subprocess goes through ``Command``, so a failed service or import is logged.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

from processes.upgrade.core import Command, Config, Git
from processes.upgrade.reporting import Reporter


class SmokeChecker:
    def __init__(self, config: Config, git: Git, reporter: Reporter):
        self._c = config
        self._git = git
        self._r = reporter
        # Inherit stdout/stderr by default (capture=False at the call sites):
        # a failing check's own output belongs in the journal, as before.
        self._cmd = Command(reporter, cwd=config.ninja_home)

    def restart_services(self) -> None:
        self._cmd.ok(
            "systemctl", "restart", *self._c.services, capture=False, quiet=False
        )

    def assert_services_active(self, quiet: bool = False) -> bool:
        for svc in self._c.services:
            if not self._cmd.ok(
                "systemctl", "is-active", "--quiet", svc, capture=False
            ):
                if not quiet:
                    self._r.log_error(f"service not active: {svc}")
                return False
        if self._c.channel == "whatsapp":
            if not self._cmd.ok(
                "systemctl",
                "is-active",
                "--quiet",
                "ninja-whatsapp-gateway.service",
                capture=False,
            ):
                if not quiet:
                    self._r.log_error("service not active: ninja-whatsapp-gateway")
                return False
        return True

    def wait_services_active(self, timeout: int) -> bool:
        # Per-poll checks are silenced (services still coming up); only the final
        # check after the timeout logs which service is down.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.assert_services_active(quiet=True):
                return True
            time.sleep(self._c.startup_poll)
        return self.assert_services_active()

    def health_snapshot(self, out: Path) -> bool:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            # Snapshots outlive the run, so a leftover from a previous one would
            # be mistaken for this run's if the health service fails to write.
            out.unlink(missing_ok=True)
        except OSError as e:
            self._r.log_warn(f"cannot prepare health snapshot {out}: {e}")
            return False
        env = {**os.environ, "PYTHONPATH": f"/workspace:{self._c.ninja_home}"}
        self._cmd.run(
            "/usr/local/bin/python",
            "processes/health_service.py",
            "--once",
            "--status-file",
            str(out),
            env=env,
            # --once exits with the number of failing checks, so a non-zero exit
            # is the reading we asked for — the written file decides success.
            quiet=True,
        )
        return out.is_file() and out.stat().st_size > 0

    def health_regressed(self, pre: Path, post: Path) -> Optional[str]:
        """Comma-joined checks that went ok(0)→fail(1), else None."""
        try:
            pre_d = json.loads(pre.read_text())
            post_d = json.loads(post.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        reg = sorted(k for k, v in post_d.items() if v and pre_d.get(k, 1) == 0)
        return ",".join(reg) if reg else None

    def check(self, pre_health: Optional[Path]) -> bool:
        c = self._c
        if c.smoke_cmd:
            # Run the override directly as a program (no shell) — matches the old
            # script's `"$NINJA_SMOKE_CMD"` and avoids shell-injection. Fails
            # closed (→ rollback) on a nonzero exit OR an exec error (OSError).
            return self._cmd.ok(c.smoke_cmd, capture=False, quiet=False)
        env = {**os.environ, "PYTHONPATH": f"/workspace:{c.ninja_home}"}
        # 1. Compiles + orchestrator imports.
        if not self._cmd.ok(
            "/usr/local/bin/python",
            "-c",
            "import processes.orchestrator",
            env=env,
            capture=False,
            quiet=False,
        ):
            return False
        # 2. Services restart and come active.
        self.restart_services()
        if not self.wait_services_active(c.startup_timeout):
            return False
        # 2b. Soak (catch crash-loops).
        time.sleep(c.soak_secs)
        if not self.assert_services_active():
            self._r.log_error(f"a service crashed during the {c.soak_secs}s soak")
            return False
        # 3. Web endpoints answer.
        for port in (9000, 9020):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10):
                    pass
            except Exception:
                return False
        # 4. Differential health gate.
        if (
            c.health_check
            and pre_health
            and pre_health.is_file()
            and pre_health.stat().st_size > 0
        ):
            post = c.upgrade_dir / "health-post.json"
            if self.health_snapshot(post):
                reg = self.health_regressed(pre_health, post)
                if reg:
                    self._r.log_error(f"health regressed after upgrade: {reg}")
                    return False
                self._r.log("health checks: no regression vs pre-upgrade")
            else:
                self._r.log_warn(
                    "post-upgrade health snapshot unavailable — skipping health gate"
                )
        return True
