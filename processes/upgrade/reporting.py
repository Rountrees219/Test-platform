"""Reporter — all user/telemetry output for a run: stderr logging, the durable
cross-run history file, PostHog events, and the result marker. ``version`` is set
once by the orchestrator after download so telemetry can include it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

from constants import UPGRADE_SERVICE_NAME
from core.logging import get_logger
from processes.upgrade.core import Config, Outcome

logger = get_logger(UPGRADE_SERVICE_NAME)

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_LOG_METHOD = {"DEBUG": "debug", "INFO": "info", "WARN": "warning", "ERROR": "error"}


class Reporter:
    def __init__(self, config: Config):
        self._c = config
        self.version = "unknown"  # set by the orchestrator once known
        self._history_broken = False  # so the warning is raised once per run
        self.command_failures = 0
        self.command_failures_expected = 0

    # --- durable history file ---------------------------------------------
    def history_append(self, text: str) -> None:
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(self._c.history_log, "a") as f:
                f.write(f"{ts} {text}\n")
        except OSError as e:
            # Never fails the run, but must not be silent either
            if not self._history_broken:
                self._history_broken = True
                logger.warning(
                    "upgrade history log unwritable (%s): %s", self._c.history_log, e
                )
                print(
                    f"ninja-upgrade WARN  history log unwritable "
                    f"({self._c.history_log}): {e}",
                    file=sys.stderr,
                    flush=True,
                )

    def init_history(self) -> None:
        # Size is bounded by logrotate (/workspace/logs/*.log), so just
        # ensure the dir exists and mark the run start.
        try:
            self._c.history_log.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.history_append(f"===== upgrade run start (pid {os.getpid()}) =====")

    # --- logging ----------------------------------------------------------
    def _log(self, level: str, msg: str) -> None:
        if _LEVELS.get(level, 20) < _LEVELS.get(self._c.log_level, 20):
            return

        getattr(logger, _LOG_METHOD.get(level, "info"))(msg)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"ninja-upgrade {level:<5} {msg}"

        print(f"{ts} {line}", file=sys.stderr, flush=True)
        self.history_append(line)  # durable cross-run history

    def log_debug(self, msg: str) -> None:
        self._log("DEBUG", msg)

    def log(self, msg: str) -> None:
        self._log("INFO", msg)

    def log_warn(self, msg: str) -> None:
        self._log("WARN", msg)

    def log_error(self, msg: str) -> None:
        self._log("ERROR", msg)

    def note_command_failure(self, summary: str, *, expected: bool = False) -> None:
        """Record a command that failed. ``expected`` = a probe answering "no"."""
        if expected:
            self.command_failures_expected += 1
            self.log_debug(summary)
        else:
            self.command_failures += 1
            self.log_warn(summary)

    # --- results + telemetry ----------------------------------------------
    def emit_result(self, outcome: Outcome) -> None:
        print(f"NINJA_UPGRADE_RESULT={outcome.value}", flush=True)
        self.history_append(f"RESULT={outcome.value}")

    def posthog_capture(self, status: str, message: str) -> None:
        """Best-effort PostHog event, sent synchronously so a following restart
        can't drop it. Never affects the run."""
        try:
            from clients.posthog_client import capture

            capture(
                "ninja upgrade",
                {
                    "error": 0 if status == "success" else 1,
                    "status": status,
                    "version": self.version or "unknown",
                    "message": message,
                    # Failed commands: real failures, and probes answering
                    "command_failures": self.command_failures,
                    "command_failures_expected": self.command_failures_expected,
                },
                sync=True,
            )
        except Exception:
            pass

    def notify(self, level: str, message: str) -> None:
        if self._c.notify_cmd:
            try:
                subprocess.run([self._c.notify_cmd, level, message], check=False)
            except OSError:
                pass
        self._log("INFO", f"notify[{level}] {message}")
        if level in ("success", "error", "rollback"):
            self.posthog_capture(level, message)
