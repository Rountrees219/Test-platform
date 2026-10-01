#!/usr/bin/env python3
"""
Agent Monitor — thin entry point.

Dispatches to the active channel's monitor strategy via ``processes/factory.py``.
Shared utilities (constants, rate limiter, heartbeat, orchestrator helpers) and
the ``PollingMonitorStrategy`` base class live in ``processes/common.py``.

Usage:
    python monitor.py              # Run with configured agent
    python monitor.py --agent ninja # Run as specific agent
"""

from __future__ import annotations

from processes.factory import get_monitor_strategy


def main():
    strategy = get_monitor_strategy()
    raise SystemExit(strategy.run())


if __name__ == "__main__":
    main()
