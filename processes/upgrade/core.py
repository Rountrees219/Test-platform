"""Foundations for the upgrade package: outcomes, config, and the git wrapper.

No behavior/orchestration lives here — just the primitives every collaborator
(Reporter, PackageResolver, SmokeChecker, Merger, Upgrader) is built from.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class Outcome(str, Enum):
    """Terminal markers the dashboard classifier reads (via ``emit_result``)."""

    UPGRADED = "upgraded"
    ROLLED_BACK = "rolled_back"
    CONFLICT = "conflict"
    ERROR = "error"
    UP_TO_DATE = "up_to_date"
    DISABLED = "disabled"


class Done(Exception):
    """Abort the run with ``code``, optionally emitting ``outcome`` as the final
    marker (shell's ``exit N`` + ``emit_result``). Caught once in ``Upgrader.run``."""

    def __init__(self, code: int, outcome: Optional[Outcome] = None):
        self.code = code
        self.outcome = outcome


def version_le(new: str, cur: str) -> bool:
    """True if ``new`` <= ``cur`` under version ordering (mirrors ``sort -V``)."""

    def key(v: str):
        return [int(p) if p.isdigit() else p for p in re.split(r"[.\-_]", v.strip())]

    try:
        return key(new) <= key(cur)
    except TypeError:
        return new <= cur


_SERVICES = (
    "ninja-monitor.service ninja-dashboard.service "
    "ninja-integrations.service ninja-health.service"
)

_KNOWN_CHANNELS = ("teams", "slack", "whatsapp", "discord")


def _channel_from_settings() -> Optional[str]:
    """Infer the messaging channel from ~/.agent_settings.json.

    The settings file keys the active adapter by its channel name
    (e.g. {"teams": {...}}), so the presence of a known channel key
    tells us which adapter this instance runs. Returns None if the
    file is missing/unreadable or contains no known channel key.
    """
    try:
        settings = json.loads((Path.home() / ".agent_settings.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for name in _KNOWN_CHANNELS:
        if isinstance(settings.get(name), dict):
            return name
    return None


@dataclass(frozen=True)
class Config:
    """Immutable run configuration, resolved from env + CLI flags."""

    ninja_home: Path
    baseline: str
    do_push: bool
    health_check: bool
    upgrade_flag: str
    lockfile: Path
    upgrade_dir: Path
    history_log: Path
    log_level: str
    llm_resolve_cmd: Optional[str]
    llm_timeout: int
    smoke_cmd: Optional[str]
    notify_cmd: Optional[str]
    package_url_override: Optional[str]
    cdn_base: Optional[str]
    ff_override: Optional[str]
    upstream_owned: tuple[str, ...]
    services: tuple[str, ...]
    soak_secs: int
    startup_timeout: int
    startup_poll: int
    download_timeout: int
    channel: str
    target_version: Optional[str]
    force: bool

    @classmethod
    def from_env(
        cls,
        *,
        version: Optional[str] = None,
        channel: Optional[str] = None,
        push: Optional[bool] = None,
        force: bool = False,
    ) -> "Config":
        env = os.environ.get
        ninja_home = Path(env("NINJA_HOME", "/workspace/ninja"))
        return cls(
            ninja_home=ninja_home,
            baseline=env("NINJA_BASELINE_BRANCH", "ninja-upstream"),
            do_push=(env("NINJA_UPGRADE_PUSH", "1") == "1") if push is None else push,
            health_check=env("NINJA_HEALTH_CHECK", "1") == "1",
            upgrade_flag=env("NINJA_UPGRADE_FLAG", "ninja-auto-upgrade"),
            lockfile=Path(env("NINJA_LOCKFILE", str(ninja_home / ".ninja-git.lock"))),
            upgrade_dir=Path(env("NINJA_UPGRADE_DIR", str(ninja_home / ".upgrade"))),
            history_log=Path(
                env(
                    "NINJA_UPGRADE_HISTORY_LOG",
                    "/workspace/logs/ninja-upgrade.history.log",
                )
            ),
            log_level=env("NINJA_UPGRADE_LOG_LEVEL", "INFO"),
            llm_resolve_cmd=env("NINJA_LLM_RESOLVE_CMD"),
            llm_timeout=int(env("NINJA_LLM_TIMEOUT", "300")),
            smoke_cmd=env("NINJA_SMOKE_CMD"),
            notify_cmd=env("NINJA_NOTIFY_CMD"),
            package_url_override=env("NINJA_PACKAGE_URL"),
            cdn_base=env("NINJA_CDN_BASE"),
            ff_override=env("NINJA_FF_OVERRIDE"),
            upstream_owned=tuple(
                ("VERSION " + env("NINJA_UPSTREAM_OWNED", "")).split()
            ),
            services=tuple(_SERVICES.split()),
            soak_secs=int(env("NINJA_SMOKE_SOAK_SECS", "25")),
            startup_timeout=int(env("NINJA_SMOKE_STARTUP_SECS", "60")),
            startup_poll=int(env("NINJA_SMOKE_POLL_SECS", "2")),
            download_timeout=int(env("NINJA_DOWNLOAD_TIMEOUT", "120")),
            channel=(
                channel
                or env("MESSAGING_CHANNEL")
                or _channel_from_settings()
                or "slack"
            ),
            target_version=version,
            force=force,
        )


_IDENTITY = ["-c", "user.name=ninja", "-c", "user.email=ninja@ninjatech.ai"]


class Command:
    """Runs subprocesses and accounts for how they went.

    Nothing raises (``check=False``) and most callers ignore the result, so
    every non-zero exit — and every failure to exec at all — is logged and
    counted here. Pass ``quiet=True`` where non-zero is an expected answer
    (a probe), which logs at debug instead of warn. ``TimeoutExpired`` still
    propagates: only the caller knows what a stalled command means.
    """

    def __init__(self, reporter=None, cwd: Path | None = None):
        self._r = reporter
        self._cwd = cwd

    def run(
        self,
        *argv: str,
        cwd: Path | None = None,
        timeout: int | None = None,
        quiet: bool = False,
        capture: bool = True,
        env: dict | None = None,
        label: str | None = None,
    ) -> subprocess.CompletedProcess:
        where = cwd or self._cwd
        name = label or " ".join(argv)
        try:
            result = subprocess.run(
                list(argv),
                cwd=str(where) if where else None,
                text=True,
                capture_output=capture,
                check=False,
                timeout=timeout,
                env=env,
            )
        except OSError as e:
            # Missing/non-executable binary: the same kind of bad news as a
            # non-zero exit, so report it the same way instead of crashing a
            # caller that only asked a yes/no question.
            self._note(f"{name} could not run: {e}", quiet)
            return subprocess.CompletedProcess(list(argv), 127, "", str(e))
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            self._note(
                f"{name} exited {result.returncode}"
                + (f": {detail[0][:200]}" if detail else ""),
                quiet,
            )
        return result

    def _note(self, summary: str, quiet: bool) -> None:
        if self._r is not None:
            self._r.note_command_failure(summary, expected=quiet)

    def ok(self, *argv: str, **kw) -> bool:
        kw.setdefault("quiet", True)
        return self.run(*argv, **kw).returncode == 0

    def out(self, *argv: str, **kw) -> str:
        """Trimmed stdout ('' on failure — matches the shell's ``|| echo ''``)."""
        return self.run(*argv, **kw).stdout.strip()


class Git:
    """Git vocabulary over a ``Command``: the identity/repo defaults every git
    call needs, plus aliases for the multi-step operations callers ask for."""

    # Sandbox helper that feeds git the credentials for a push.
    ASKPASS = Path("/usr/local/bin/git-askpass.sh")

    def __init__(self, repo: Path, reporter=None):
        self.repo = repo
        self._cmd = Command(reporter, cwd=repo)

    def setup(self) -> None:
        """Wire up credentials so pushes can authenticate non-interactively."""
        if self.ASKPASS.is_file():
            os.environ["GIT_ASKPASS"] = str(self.ASKPASS)

    def run(
        self, *args: str, identity: bool = False, **kw
    ) -> subprocess.CompletedProcess:
        prefix = _IDENTITY if identity else []
        # Label without the identity prefix — it is noise in every log line.
        return self._cmd.run("git", *prefix, *args, label=f"git {' '.join(args)}", **kw)

    def ok(self, *args: str, **kw) -> bool:
        kw.setdefault("quiet", True)
        return self.run(*args, **kw).returncode == 0

    def out(self, *args: str, **kw) -> str:
        return self.run(*args, **kw).stdout.strip()

    def has_changes(self) -> bool:
        """True if the working tree has anything uncommitted (tracked or not)."""
        return bool(self.out("status", "--porcelain"))

    def is_merging(self) -> bool:
        """True if a merge is still in progress (MERGE_HEAD present)."""
        return self.ok("rev-parse", "-q", "--verify", "MERGE_HEAD")

    def cancel_merge(self) -> None:
        """Back out of an in-progress merge, forcing it if git refuses."""
        if not self.ok("merge", "--abort", quiet=False):
            self.run("reset", "--hard", "HEAD")

    def current_branch(self) -> str:
        """The checked-out branch, or "" when HEAD is detached."""
        return self.out("branch", "--show-current")

    def conflicted_files(self) -> list[str]:
        out = self.out("diff", "--name-only", "--diff-filter=U")
        return [f for f in out.splitlines() if f]

    def needs_resolution(self) -> bool:
        return bool(self.conflicted_files())

    def conflicted_pretty(self) -> str:
        return " ".join(self.conflicted_files())
