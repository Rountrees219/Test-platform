"""
core.runner — base classes for long-running ninja processes.

PollingLoop
    Subclass for processes that run on a repeated interval (e.g. monitor.py).
    Override tick() with the work to do each cycle.

OneShotJob
    Subclass for processes that run once and exit (e.g. orchestrator.py).
    Override run() with the work to do.

Example — PollingLoop:

    from core.runner import PollingLoop

    class MyMonitor(PollingLoop):
        def tick(self) -> None:
            print("doing work")

    MyMonitor(interval=30, jitter=5).start()

Example — OneShotJob:

    from core.runner import OneShotJob

    class MyWorker(OneShotJob):
        def run(self) -> int:
            print("doing work")
            return 0

    sys.exit(MyWorker().execute())
"""

from __future__ import annotations

import logging
import random
import signal
import time
from abc import ABC, abstractmethod
from typing import Optional

_logger = logging.getLogger(__name__)


class PollingLoop(ABC):
    """
    Base class for processes that run tick() on a repeated interval.

    Features:
    - Configurable interval + random jitter
    - Optional max_runtime wall-clock limit
    - Graceful shutdown on SIGTERM / SIGINT (finishes current tick)
    - Subclasses can override on_start() and on_stop() for setup/teardown
    """

    def __init__(
        self,
        interval: float = 30.0,
        jitter: float = 0.0,
        max_runtime: Optional[float] = None,
        name: Optional[str] = None,
    ) -> None:
        """
        Args:
            interval:    Seconds to wait between ticks.
            jitter:      Max random seconds added to each sleep (uniform 0..jitter).
            max_runtime: Optional wall-clock limit in seconds. Loop exits when exceeded.
            name:        Human-readable name used in log messages.
        """
        self.interval = interval
        self.jitter = jitter
        self.max_runtime = max_runtime
        self.name = name or self.__class__.__name__
        self._running = False
        self._start_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Subclass interface
    # ------------------------------------------------------------------

    @abstractmethod
    def tick(self) -> None:
        """Override with the work to perform each cycle."""

    def on_start(self) -> None:
        """Called once before the first tick. Override for setup."""

    def on_stop(self) -> None:
        """Called once after the loop exits. Override for teardown."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Enter the polling loop. Blocks until stopped or max_runtime exceeded."""
        self._install_signal_handlers()
        self._running = True
        self._start_time = time.monotonic()

        _logger.info(
            "%s: starting (interval=%.1fs, jitter=%.1fs, max_runtime=%s)",
            self.name,
            self.interval,
            self.jitter,
            f"{self.max_runtime:.0f}s" if self.max_runtime else "unlimited",
        )

        try:
            self.on_start()
            while self._running:
                if self._max_runtime_exceeded():
                    _logger.info("%s: max_runtime reached, stopping", self.name)
                    break

                try:
                    self.tick()
                except Exception:
                    _logger.exception("%s: unhandled exception in tick()", self.name)

                if not self._running:
                    break

                sleep_time = self.interval + random.uniform(0, self.jitter)
                _logger.debug("%s: sleeping %.1fs", self.name, sleep_time)
                self._interruptible_sleep(sleep_time)
        finally:
            self._running = False
            try:
                self.on_stop()
            except Exception:
                _logger.exception("%s: exception in on_stop()", self.name)
            _logger.info("%s: stopped", self.name)

    def stop(self) -> None:
        """Signal the loop to stop after the current tick completes."""
        _logger.info("%s: stop requested", self.name)
        self._running = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _max_runtime_exceeded(self) -> bool:
        if self.max_runtime is None or self._start_time is None:
            return False
        return (time.monotonic() - self._start_time) >= self.max_runtime

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in short chunks so stop() takes effect promptly."""
        chunk = 1.0
        elapsed = 0.0
        while self._running and elapsed < seconds:
            time.sleep(min(chunk, seconds - elapsed))
            elapsed += chunk

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, frame) -> None:
            _logger.info("%s: signal %d received, stopping", self.name, signum)
            self.stop()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)


class OneShotJob(ABC):
    """
    Base class for processes that run once and exit.

    Features:
    - SIGTERM / SIGINT handled gracefully (sets a flag; subclass can check)
    - Subclasses implement run() and return an exit code
    """

    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name or self.__class__.__name__
        self._interrupted = False

    # ------------------------------------------------------------------
    # Subclass interface
    # ------------------------------------------------------------------

    @abstractmethod
    def run(self) -> int:
        """
        Override with the job logic.

        Returns:
            Integer exit code (0 = success).
        """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def execute(self) -> int:
        """Run the job, handle signals, and return the exit code."""
        self._install_signal_handlers()
        _logger.info("%s: starting", self.name)
        try:
            code = self.run()
        except Exception:
            _logger.exception("%s: unhandled exception in run()", self.name)
            code = 1
        _logger.info("%s: finished (exit code %d)", self.name, code)
        return code

    @property
    def interrupted(self) -> bool:
        """True if a SIGTERM or SIGINT was received during run()."""
        return self._interrupted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, frame) -> None:
            _logger.info("%s: signal %d received", self.name, signum)
            self._interrupted = True

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
