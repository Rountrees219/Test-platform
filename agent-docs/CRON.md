# Cron — Scheduled Agent Prompts

Ninja can run recurring agent prompts on a schedule. Cron jobs flow
through the **same monitor batch path** as WhatsApp mentions, so the agent
responds to scheduled jobs the same way it responds to user messages —
just with `type: cron` instead of `type: mention`.

> **Companion docs:** [`MONITOR.md`](MONITOR.md) (the loop that ticks
> cron), [`WHATSAPP_INTERFACE.md`](WHATSAPP_INTERFACE.md) (how to post the
> result back to WhatsApp).

## When to use cron

- "Send a daily standup at 9am customer-local time."
- "Check the deploy queue every 30 minutes."
- "Post a weekly report on Mondays."

Schedules evaluate in **system local time**, which Ninja sets from the
customer's WhatsApp account timezone. So `0 9 * * *` means 9am for the
customer, not 9am UTC.

## CLI — `tools/cron.py`

```bash
# Create a job
python tools/cron.py add \
    --id daily-summary \
    --schedule "0 9 * * *" \
    --prompt "Post a short daily summary of yesterday's PRs."

# List
python tools/cron.py list
python tools/cron.py list --json

# Inspect
python tools/cron.py show daily-summary

# Pause / resume
python tools/cron.py disable daily-summary
python tools/cron.py enable daily-summary

# Force-fire on the next monitor tick (useful for testing)
python tools/cron.py trigger daily-summary

# Delete
python tools/cron.py remove daily-summary
```

`--json` is supported on `list` and `add` for machine-readable output.

The agent always posts to its single configured WhatsApp channel — there
is no per-job channel. If a job needs to reply in a thread, pass
`--thread-ts <ts>` at creation.

## Schedule syntax

Standard 5-field POSIX cron:

```
┌───── minute (0-59)
│ ┌─── hour (0-23)
│ │ ┌─ day of month (1-31)
│ │ │ ┌─── month (1-12)
│ │ │ │ ┌─── day of week (0-6, Sunday=0)
│ │ │ │ │
* * * * *
```

Examples:

| Schedule        | Meaning                              |
|-----------------|--------------------------------------|
| `*/5 * * * *`   | Every 5 minutes                      |
| `0 9 * * *`     | Every day at 9:00 local              |
| `0 9 * * 1-5`   | Weekdays at 9:00 local               |
| `30 17 * * 5`   | Fridays at 5:30pm local              |
| `0 */2 * * *`   | Every two hours on the hour          |

## Job storage

Jobs live in `.ninja_crons.json` next to `processes/monitor.py`. The file is
**gitignored** — it is per-installation runtime state. Schema:

```json
{
  "id": "daily-summary",
  "enabled": true,
  "schedule": "0 9 * * *",
  "prompt": "Post a short daily summary of yesterday's PRs.",
  "thread_ts": null,
  "next_run_at": 1779800400.0,
  "last_run_at": null,
  "run_count": 0,
  "created_at": 1779700000.0
}
```

## How it executes

1. `ninja-monitor.service` ticks every ~60s (60s base + up to 5s random
   jitter) and calls `cron_scheduler.get_due_cron_messages()`.
2. For each due job, the monitor calls `claim_cron(id)` which advances
   `next_run_at` **before** the agent runs (restart-safe).
3. The job is appended to the same `pending_messages` batch as WhatsApp
   mentions, with `type: "cron"`.
4. The agent answers it like any other message and posts the result
   using `messaging/whatsapp/interface.py say "..."`. The agent has a single
   configured WhatsApp channel, so no `-c` flag is needed. If the job has
   `thread_ts` set, the agent replies in-thread with `-t <message_id>`.

## Responding to a cron item (agent instructions)

When you receive a batch entry with `type: cron`, treat it as a
self-directed task, not a user question:

- Execute the `prompt` exactly.
- Post the result with `python messaging/whatsapp/interface.py say "..."`. If the
  job has `thread_ts` set, reply in-thread with `-t <message_id>`.
  Ninja has only one configured WhatsApp channel, so no `-c` flag is
  needed.
- Keep output concise — same tone rules as `MONITOR.md`.
- Do **not** ask for confirmation. The schedule is the consent.

## Limits and non-goals (v1)

- No per-job timezone overrides — uses system local time.
- No conditional / dependency-gated runs (deferred).
- No script-only crons (deferred — use the agent prompt instead).
- No web dashboard. Manage with the CLI.
- Single Ninja monitor process assumed; multi-worker locking would
  need a real DB.
