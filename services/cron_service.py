"""
cron_scheduler.py — JSON-backed cron scheduler for Ninja.

Internal library used by ``monitor.py`` to find due cron jobs and inject
them into the existing ``pending_messages`` batch. End users / agents
should drive cron via ``tools/cron.py`` (CLI) — see
``agent-docs/CRON.md``.

Design notes
------------
* Single state file at ``REPO_ROOT/.ninja_crons.json`` — gitignored.
* Schedules are evaluated in **system local time**. Ninja now sets
  ``/etc/localtime`` from the customer's Slack timezone (PR #19), so a
  cron like ``"0 9 * * *"`` means 9am customer-local.
* "Claim before run": when a job is due, we advance ``next_run_at``
  *before* the agent runs. Restart-safe; trades duplicate runs for
  occasional missed runs, which is the right default for chat agents.
* No DB, no daemon, no external service. Ninja's existing
  ``ninja-monitor.service`` is the tick loop.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from croniter import croniter
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "croniter is required for the Ninja cron scheduler. "
        "Install with: pip install croniter"
    ) from e


REPO_ROOT = Path(__file__).parent
CRONS_FILE = REPO_ROOT / ".ninja_crons.json"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def load_crons() -> list[dict[str, Any]]:
    """Return the list of cron jobs. Empty list if the file does not exist."""
    if not CRONS_FILE.exists():
        return []
    try:
        with CRONS_FILE.open("r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return data.get("jobs", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_crons(jobs: list[dict[str, Any]]) -> None:
    """Atomically persist cron jobs to disk."""
    CRONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".ninja_crons.", suffix=".json", dir=str(CRONS_FILE.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(jobs, f, indent=2, sort_keys=True)
        os.replace(tmp_path, CRONS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Schedule math
# ---------------------------------------------------------------------------


def calculate_next_run(schedule: str, from_ts: float | None = None) -> float:
    """Return the next epoch-seconds at which ``schedule`` fires after ``from_ts``.

    Evaluates in **system local time**. Ninja sets ``/etc/localtime``
    from the customer's Slack timezone (PR #19), so a schedule like
    ``"0 9 * * *"`` means 9am customer-local, not 9am UTC.

    Implementation note: croniter interprets cron fields in UTC when
    given a naive datetime, which would silently make every job fire at
    UTC times. Passing a timezone-aware datetime in system local zone
    makes croniter respect that zone. We attach the local zone via
    ``.astimezone()``.
    """
    ts = from_ts if from_ts is not None else time.time()
    base_local_aware = datetime.fromtimestamp(ts).astimezone()
    itr = croniter(schedule, base_local_aware)
    nxt = itr.get_next(datetime)  # timezone-aware datetime in same zone
    return nxt.timestamp()


def is_valid_schedule(schedule: str) -> bool:
    """Cheap validity check used by the CLI."""
    try:
        croniter(schedule, datetime.now().astimezone())
        return True
    except (ValueError, KeyError):
        return False


# ---------------------------------------------------------------------------
# Public API used by monitor.py
# ---------------------------------------------------------------------------


def get_due_cron_messages(now_ts: float | None = None) -> list[dict[str, Any]]:
    """Return enabled jobs whose ``next_run_at`` is <= ``now_ts``.

    Does *not* mutate state. The monitor calls ``claim_cron(job_id)`` to
    advance ``next_run_at`` before invoking the agent.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    due: list[dict[str, Any]] = []
    for job in load_crons():
        if not job.get("enabled", True):
            continue
        next_run_at = job.get("next_run_at")
        if next_run_at is None:
            continue
        if float(next_run_at) <= now_ts:
            due.append(job)
    return due


def claim_cron(job_id: str, now_ts: float | None = None) -> bool:
    """Advance ``next_run_at`` for ``job_id`` to its next scheduled time.

    Returns True if the job was successfully claimed (i.e. it was due
    and we advanced it). Returns False if the job is missing, disabled,
    or already advanced past now (race with another tick).
    """
    now_ts = now_ts if now_ts is not None else time.time()
    jobs = load_crons()
    changed = False
    claimed = False
    for job in jobs:
        if job.get("id") != job_id:
            continue
        if not job.get("enabled", True):
            return False
        next_run_at = job.get("next_run_at")
        if next_run_at is None or float(next_run_at) > now_ts:
            return False
        job["last_run_at"] = now_ts
        job["next_run_at"] = calculate_next_run(job["schedule"], now_ts)
        job["run_count"] = int(job.get("run_count", 0)) + 1
        changed = True
        claimed = True
        break
    if changed:
        save_crons(jobs)
    return claimed


# ---------------------------------------------------------------------------
# CRUD helpers used by tools/cron.py
# ---------------------------------------------------------------------------


def add_cron(
    job_id: str,
    schedule: str,
    prompt: str,
    thread_ts: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    if not is_valid_schedule(schedule):
        raise ValueError(f"Invalid cron expression: {schedule!r}")
    jobs = load_crons()
    if any(j.get("id") == job_id for j in jobs):
        raise ValueError(f"Cron job {job_id!r} already exists")
    job = {
        "id": job_id,
        "enabled": enabled,
        "schedule": schedule,
        "prompt": prompt,
        "thread_ts": thread_ts,
        "next_run_at": calculate_next_run(schedule),
        "last_run_at": None,
        "run_count": 0,
        "created_at": time.time(),
    }
    jobs.append(job)
    save_crons(jobs)
    return job


def remove_cron(job_id: str) -> bool:
    jobs = load_crons()
    new_jobs = [j for j in jobs if j.get("id") != job_id]
    if len(new_jobs) == len(jobs):
        return False
    save_crons(new_jobs)
    return True


def set_enabled(job_id: str, enabled: bool) -> bool:
    jobs = load_crons()
    for job in jobs:
        if job.get("id") == job_id:
            job["enabled"] = enabled
            if enabled and job.get("next_run_at") is None:
                job["next_run_at"] = calculate_next_run(job["schedule"])
            save_crons(jobs)
            return True
    return False


def trigger_cron(job_id: str) -> bool:
    """Force ``next_run_at`` to now so the next monitor tick picks it up."""
    jobs = load_crons()
    for job in jobs:
        if job.get("id") == job_id:
            job["next_run_at"] = time.time()
            job["enabled"] = True
            save_crons(jobs)
            return True
    return False


def get_cron(job_id: str) -> dict[str, Any] | None:
    for job in load_crons():
        if job.get("id") == job_id:
            return job
    return None
