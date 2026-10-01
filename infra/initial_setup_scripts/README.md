# initial_setup_scripts

One-shot bootstrap scripts that configure a fresh Ninja sandbox so it
matches the operator's environment. Each script is **stdlib-only** and
**idempotent** — safe to re-run and safe to invoke before the Ninja
Python package is installed.

This folder lives inside `src/ninja/` so it ships through the CDK
`PublishStack` zip (`packages = [{ include = "ninja", from = "src" }]`).
Anything outside `src/ninja/` is *not* part of the deployed agent.

## Scripts

### `set_timezone.py`

Detects the Slack user's IANA timezone (e.g. `Australia/Canberra`) from
the Slack token in `/dev/shm/mcp-token` and applies it as the Linux
system timezone. This keeps all subsequent timestamps — logs,
scheduled jobs, Slack messages, GitHub commits — aligned with the
operator's local time.

#### Usage

```bash
# Detect from Slack and apply
python src/ninja/initial_setup_scripts/set_timezone.py

# Preview without modifying the system
python src/ninja/initial_setup_scripts/set_timezone.py --dry-run

# Skip Slack detection and set an explicit zone
python src/ninja/initial_setup_scripts/set_timezone.py --timezone Europe/Berlin

# Machine-readable output (only the final zone on stdout)
python src/ninja/initial_setup_scripts/set_timezone.py --quiet
```

On a deployed agent the same script is reachable as
`/workspace/ninja/initial_setup_scripts/set_timezone.py` and
`install.sh` invokes it automatically during sandbox provisioning.

#### Behaviour

1. Reads the `Slack=` record from `/dev/shm/mcp-token`.
2. Calls Slack `auth.test` + `users.info` to read the caller's `tz`.
3. Validates the zone against `/usr/share/zoneinfo`.
4. Applies it using either:
    - `timedatectl set-timezone <zone>` (prefers `sudo -n` when not
      running as root), or
    - a direct `/etc/localtime` symlink + `/etc/timezone` write
      (container / sandbox fallback that works without systemd).
5. Confirms by printing the new `date` output.

#### Exit codes

| Code | Meaning                                     |
|-----:|---------------------------------------------|
|   0  | Timezone detected and applied (or dry-run)  |
|   1  | No Slack token available                    |
|   2  | Slack API call failed                       |
|   3  | Timezone not valid on this host (no tzdata) |
|   4  | Could not apply (permissions / systemd)     |

## Adding new setup scripts

Bootstrap scripts should:

- Live in this folder (`src/ninja/initial_setup_scripts/`) so they
  ride along in the CDK package zip.
- Be runnable with **only the Python stdlib** — they may execute
  before `pip install -r requirements.txt` has happened.
- Be **idempotent** — a second run on an already-configured sandbox
  should be a no-op and exit `0`.
- Support `--dry-run` whenever they mutate system state.
- Log their actions in a consistent `▶` / `✓` / `✗` prefix style.
- Be invoked from `install.sh` via `$SCRIPT_DIR/initial_setup_scripts/<name>.py`
  so the path is correct on both git checkouts and deployed agents.
