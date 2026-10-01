"""
services.progress_service — keeps the server-side progress bar alive.

Started once at monitor boot; idle until a run arms the state file.
On each tick: if the run is old enough, start the activity (once); then
report whatever steps the hook recorded and send a heartbeat.

Reporting belongs here, not in the hook: the hook runs inside the agent's
tool loop, and it only fires after a tool call returns — so a two-minute
browser step would freeze the message just when the user is watching it.
"""

from __future__ import annotations

import threading
from typing import Optional

from constants import (
    LIVE_PROGRESS_DELAY_SECONDS,
    LIVE_PROGRESS_INTERVAL_SECONDS,
    MONITOR_SERVICE_NAME,
)
from core import live_progress
from core.logging import get_logger

logger = get_logger(MONITOR_SERVICE_NAME)

# Back off after an error so a bad token or network can't hot-loop.
ERROR_BACKOFF_SECONDS = 30.0

# Stop updating after this many failures in a row.  State is kept so the
# end-of-run cleanup can still complete the activity.
MAX_UPDATE_FAILURES = 3


class ProgressService:
    """Background thread that ticks the server-side progress bar on a timer."""

    def __init__(self, iface, interval_seconds: Optional[float] = None):
        self.iface = iface
        self.interval = float(interval_seconds or LIVE_PROGRESS_INTERVAL_SECONDS)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failures = 0
        self._reported = 0
        self._run_started_at: Optional[float] = None

    def start(self) -> bool:
        """Start the daemon thread."""
        self._thread = threading.Thread(
            target=self._run, name="live-progress", daemon=True
        )
        self._thread.start()
        logger.info(f"ProgressService started (interval={self.interval}s)")
        return True

    def stop(self) -> None:
        """Signal the loop to exit (mainly for tests)."""
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
                self._stop_event.wait(self.interval)
            except Exception as e:  # never let the thread die
                logger.warning(f"ProgressService tick failed: {e}")
                self._stop_event.wait(ERROR_BACKOFF_SECONDS)

    def tick(self) -> bool:
        """One refresh.  Returns True when the activity was started or ticked."""
        state = live_progress.read_state()
        if not state:
            self._failures = 0
            return False

        # A new run starts over: new steps file, and past failures are not its.
        if state.get("started_at") != self._run_started_at:
            self._run_started_at = state.get("started_at")
            self._reported = 0
            self._failures = 0

        if self._failures >= MAX_UPDATE_FAILURES:
            return False

        thread_id = state.get("thread_ts")
        if not thread_id:
            return False

        # Wait for the run to be slow enough before showing progress.
        if not state.get("activity_started"):
            if live_progress.seconds_running(state) < LIVE_PROGRESS_DELAY_SECONDS:
                return False
            return self._start_activity(state, thread_id)

        return self._tick(thread_id)

    def _start_activity(self, state: dict, thread_id: str) -> bool:
        """Post the initial progress bar once the run is slow enough."""
        result = self.iface.start_activity(thread_id)
        if not result:
            self._failures += 1
            if self._failures >= MAX_UPDATE_FAILURES:
                logger.warning("Activity start keeps failing — stopping")
            return False

        if not live_progress.mark_activity_started(result.get("id")):
            # Run finished mid-start — complete the orphan activity.
            self.iface.complete_activity(thread_id)
            return False

        self._failures = 0
        logger.info(
            f"Activity started (thread={thread_id}, activity_id={result.get('id')})"
        )
        # The run has been working for a minute already — show that work now.
        self._report_new_steps(thread_id)
        return True

    def _report_new_steps(self, thread_id: str) -> None:
        """Send the steps the hook has recorded since the last pass.

        Stops at the first failure and leaves the cursor there, so a dropped
        step is retried in order rather than lost. The failure is not counted:
        tick_activity fails alongside it and does the counting.
        """
        for step in live_progress.read_steps()[self._reported :]:
            if not self.iface.add_completed_action(thread_id, step["label"]):
                return
            self._reported += 1

    def _tick(self, thread_id: str) -> bool:
        """Report new steps, then heartbeat to keep the progress bar alive."""
        self._report_new_steps(thread_id)
        ok = self.iface.tick_activity(thread_id)
        if ok:
            self._failures = 0
        else:
            self._failures += 1
            if self._failures >= MAX_UPDATE_FAILURES:
                logger.warning("Activity tick keeps failing — stopping")
        return ok
