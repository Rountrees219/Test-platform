# Ninja Loop (issue-driven)

Ninja runs as a loop. **GitHub Issues are the work queue** (durable memory):
the monitor produces work, the orchestrator does it.

- **Monitor** (`processes/monitor.py`): answers quick WhatsApp messages inline. For
  substantial work, files an issue (`tools/issues.py create`). When there are
  open issues and no orchestrator is running, it launches one via
  `systemctl start ninja.service`.
- **Orchestrator** (`processes/orchestrator.py`), two phases per run — both in
  a **single Claude invocation** — then exits:
  - **Phase 1 — WORK** (only if open issues): work **exactly one** issue — the
    single highest-priority open issue: understand it (read the issue + WhatsApp
    history for context), complete it, comment, and `close` it. The next cycle
    picks up the next issue.
  - **Phase 2 — REFLECT** (straight after Phase 1, same invocation): file new
    follow-up issues, refine `tools/`. Capture large work as issues — don't do
    it inline. No open issues → skip both phases.

## Issue tool (`tools/issues.py`)

```
python tools/issues.py list | count --json
python tools/issues.py create --title "..." --body "..."
python tools/issues.py comment <n> --body "..."
python tools/issues.py close <n> --comment "done: ..."
```

Issues are labelled `ninja` by default. Python: `from tools import issues`.

## Blocked issues

If Phase 1 cannot progress an issue (missing access, external dependency,
waiting on a human), it marks it `block <n> --comment "why"` — the `blocked`
label removes it from the work queue (and from monitor launch decisions).
Every 24 orchestrator cycles a blocked-issue review re-triages the list:
`unblock` if the blocker cleared, `close` if obsolete, else leave blocked.

## Why issues

Durable across restarts, decouples monitor/orchestrator (they coordinate via the
queue + systemd unit state — `systemctl is-active ninja.service` — not a shared
checkout), visible/auditable, and self-feeding (reflect keeps the queue full).
