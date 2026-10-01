"""Upgrade orchestrator — in-sandbox upgrade job (port of infra/ninja-upgrade.sh).

Polls the distribution for a newer version and, if found, applies dev's changes
to the customer's repo as a 3-way merge that preserves the customer's edits;
conflicts go to the LLM with a customer-wins fallback, a smoke check gates the
result, and any non-success rewinds the baseline so a re-click retries.

``Upgrader`` just wires the collaborators (Reporter, Git, PackageResolver,
SmokeChecker, Merger) and runs the sequence, threading run state explicitly.
Behaviour matches the shell — same log prose and ``NINJA_UPGRADE_RESULT`` marker.
"""

from __future__ import annotations

import argparse
import fcntl
import shutil
import sys
from contextlib import chdir
from pathlib import Path
from typing import Optional

from processes.upgrade.core import Config, Done, Git, Outcome, version_le
from processes.upgrade.merge import Merger
from processes.upgrade.package import PackageResolver
from processes.upgrade.reporting import Reporter
from processes.upgrade.smoke import SmokeChecker

__all__ = ["Outcome", "Upgrader", "main"]


class Upgrader:
    def __init__(
        self,
        *,
        version: Optional[str] = None,
        channel: Optional[str] = None,
        push: Optional[bool] = None,
        force: bool = False,
    ):
        self.config = Config.from_env(
            version=version, channel=channel, push=push, force=force
        )
        self.reporter = Reporter(self.config)
        self.git = Git(self.config.ninja_home, self.reporter)
        self.package = PackageResolver(self.config, self.reporter)
        self.smoke = SmokeChecker(self.config, self.git, self.reporter)
        self.merger = Merger(self.config, self.git, self.reporter)

        # Resources owned by the orchestrator (cleaned up in run()'s finally).
        self._staging_dir: Optional[Path] = None
        self._lock_fh = None

    # ------------------------------------------------------------------
    # Feature-flag gate
    # ------------------------------------------------------------------
    def ff_enabled(self, flag: str) -> bool:
        if self.config.ff_override == "1":
            return True
        if self.config.ff_override == "0":
            return False
        try:
            from clients.posthog_client import is_feature_enabled

            return bool(is_feature_enabled(flag))
        except Exception:
            return False  # fail safe (off)

    def upgrades_enabled(self) -> bool:
        return self.ff_enabled(self.config.upgrade_flag)

    # ------------------------------------------------------------------
    # Sequence
    # ------------------------------------------------------------------
    def _acquire_lock(self) -> None:
        """Non-blocking lock shared with ninja-sync — a second tick just exits."""
        try:
            self._lock_fh = open(self.config.lockfile, "w")
        except OSError as e:
            self.reporter.log_error(f"cannot open lockfile {self.config.lockfile}: {e}")
            raise Done(1, Outcome.ERROR)
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.reporter.log_warn("another git op holds the lock — exiting")
            raise Done(0)

    def _reconcile_unpushed(self, main_branch: str) -> None:
        """Complete a deferred push before deciding the box is 'up to date'.

        A prior run may have applied + committed the merge locally but failed to
        push (lease refused / network). The local baseline/main are then ahead of
        origin; push them here. This makes the push retry deterministic — it does
        not rely on a re-merge producing a fresh commit — and self-heals on the
        next tick/click. A no-op on a healthy run (nothing ahead of origin)."""
        c, git, r = self.config, self.git, self.reporter
        if not c.do_push:
            return
        pushed = []
        for branch in (c.baseline, main_branch):
            git.run("fetch", "origin", branch, quiet=True)  # tracking ref (best-effort)
            remote = f"origin/{branch}"
            if not git.ok("rev-parse", "--verify", "--quiet", remote):
                continue  # no remote counterpart yet — nothing to reconcile
            ahead = git.out("rev-list", "--count", f"{remote}..{branch}")
            if ahead and ahead != "0":
                if not git.ok(
                    "push", "origin", branch, "--force-with-lease", quiet=False
                ):
                    r.notify(
                        "error",
                        f"a previously-applied upgrade is still unpushed ({branch}) "
                        "— push failed again, will retry next run/click",
                    )
                    raise Done(1, Outcome.ERROR)
                pushed.append(branch)
        if pushed:
            v = self.merger.baseline_version()
            r.notify(
                "success",
                f"pushed a previously-applied upgrade to v{v} ({', '.join(pushed)})",
            )
            r.log(f"✓ Completed deferred push for v{v}: {', '.join(pushed)}")
            raise Done(0, Outcome.UPGRADED)

    def _finalize(
        self,
        new_version: str,
        main_branch: str,
        baseline_prev: str,
        pre_health: Optional[Path],
    ) -> None:
        git, r = self.git, self.reporter
        if not self.smoke.check(pre_health):
            r.log_error("✗ Smoke check failed — rolling back")
            git.run("reset", "--hard", f"pre-upgrade-v{new_version}")
            self.merger.rewind_baseline(baseline_prev)
            self.smoke.restart_services()
            r.notify(
                "rollback",
                f"v{new_version} failed smoke check — rolled back to pre-upgrade state",
            )
            raise Done(1, Outcome.ROLLED_BACK)

        if self.config.do_push:
            # Push baseline first (append-only), then customer-main. A push can
            # fail (--force-with-lease refused if the remote moved). Leave the
            # healthy local merge in place — do NOT rewind: the next run's
            # _reconcile_unpushed() sees the local branch ahead of origin and
            # pushes it, so the retry is deterministic (rewinding here instead
            # would strand the box on "up to date" with the merge unpushed).
            for branch in (self.config.baseline, main_branch):
                if not git.ok(
                    "push", "origin", branch, "--force-with-lease", quiet=False
                ):
                    r.notify(
                        "error",
                        f"v{new_version} applied locally but pushing {branch} "
                        "failed — will retry on next run/click",
                    )
                    raise Done(1, Outcome.ERROR)
        r.notify("success", f"upgraded to v{new_version}")
        r.log(f"✓ Upgraded to v{new_version}")
        r.emit_result(Outcome.UPGRADED)

    def _main(self) -> None:
        c, git, r = self.config, self.git, self.reporter
        r.init_history()

        if not (c.ninja_home / ".git").is_dir():
            r.log_error(f"no repo at {c.ninja_home}")
            raise Done(0)

        self._acquire_lock()
        git.setup()

        # Feature-flag gate.
        if not self.upgrades_enabled():
            r.log(
                f"upgrades not enabled for this user (feature flag '{c.upgrade_flag}' off) "
                "— exiting"
            )
            raise Done(0, Outcome.DISABLED)

        main_branch = git.current_branch()
        if not main_branch:
            r.notify(
                "error",
                f"detached HEAD in {c.ninja_home} (no current branch) — cannot upgrade "
                "safely, aborting",
            )
            raise Done(1, Outcome.ERROR)

        # Poll.
        staging = self.package.download()
        self._staging_dir = staging.dir
        r.version = staging.version
        self.merger.ensure_baseline()
        self._reconcile_unpushed(main_branch)  # finish any deferred push first
        cur_version = self.merger.baseline_version()

        if staging.version == cur_version:
            r.log(f"Up to date (v{cur_version}) — no-op")
            raise Done(0, Outcome.UP_TO_DATE)
        if not c.force and cur_version and version_le(staging.version, cur_version):
            r.log(
                f"Published v{staging.version} is not newer than baseline v{cur_version} "
                "— skipping"
            )
            raise Done(0)  # no marker (matches shell)
        r.log(f"Upgrade available: v{cur_version or 'none'} → v{staging.version}")

        # Recover from an interrupted merge left by a previous run.
        if git.is_merging():
            r.log_warn("aborting an in-progress merge left by a previous run")
            git.cancel_merge()

        # Clean tree — absorb pending sandbox edits as a customer commit.
        if git.has_changes():
            git.run("add", "-A", identity=True)
            git.run(
                "commit",
                "-q",
                "-m",
                "ninja-upgrade: autocommit sandbox changes before upgrade",
                identity=True,
            )

        # Pre-upgrade health snapshot (differential gate).
        pre_health: Optional[Path] = None
        if c.health_check and not c.smoke_cmd:
            pre = c.upgrade_dir / "health-pre.json"
            if self.smoke.health_snapshot(pre):
                pre_health = pre
            else:
                r.log_warn(
                    "pre-upgrade health snapshot unavailable — health gate disabled this run"
                )

        # Update baseline, then merge into customer-main.
        baseline_prev = git.out("rev-parse", c.baseline)
        self.merger.update_baseline(staging.dir, staging.version)
        if git.ok("merge-base", "--is-ancestor", c.baseline, "HEAD"):
            self.merger.rewind_baseline(baseline_prev)
            r.log("Baseline already merged — nothing to apply")
            raise Done(0, Outcome.UP_TO_DATE)

        self.merger.merge_upstream(staging.version, main_branch, baseline_prev)
        self._finalize(staging.version, main_branch, baseline_prev, pre_health)

    def _cleanup(self) -> None:
        if self._staging_dir and self._staging_dir.is_dir():
            shutil.rmtree(self._staging_dir, ignore_errors=True)
        if self._lock_fh is not None:
            try:
                self._lock_fh.close()
            except OSError:
                pass

    def run(self) -> int:
        try:
            with chdir(self.config.ninja_home):
                self._main()
            return 0
        except Done as d:
            if d.outcome is not None:
                self.reporter.emit_result(d.outcome)
            return d.code
        finally:
            self._cleanup()


# ---------------------------------------------------------------------------
# CLI
#   python -m processes.upgrade run
#   python -m processes.upgrade run --version 0.1.163 --channel beta --no-push
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    return Upgrader(
        version=args.version,
        channel=args.channel,
        # Only override when --no-push is given; otherwise pass None so
        # Config.from_env honors NINJA_UPGRADE_PUSH (default on).
        push=False if args.no_push else None,
        force=args.force,
    ).run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ninja-upgrade")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="Poll for and apply an upgrade")
    run.add_argument(
        "--version", help="Target a specific published version (default: latest)"
    )
    run.add_argument(
        "--channel", help="Distribution channel (default: $MESSAGING_CHANNEL or slack)"
    )
    run.add_argument(
        "--no-push", action="store_true", help="Apply locally but don't push to origin"
    )
    run.add_argument(
        "--force", action="store_true", help="Apply even if not newer than the baseline"
    )
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare invocation (or leading flags) defaults to `run` for convenience.
    if not argv or argv[0].startswith("-"):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 1
    return int(args.func(args) or 0)
