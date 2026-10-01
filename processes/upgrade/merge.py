"""Merger — baseline management, conflict resolution, and the 3-way merge.

Conflicts go to an agent harness (via ConflictResolver) with a customer-wins
fallback. Run-specific values (versions, branch, baseline_prev, staging_dir) are
passed as method params.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from processes.upgrade.core import Config, Done, Git, Outcome
from processes.upgrade.reporting import Reporter
from processes.upgrade.resolver import ConflictResolver


class Merger:
    def __init__(self, config: Config, git: Git, reporter: Reporter, resolver=None):
        self._c = config
        self._git = git
        self._r = reporter
        self._resolver = resolver or ConflictResolver(config, reporter)

    # --- baseline management ----------------------------------------------
    def baseline_version(self) -> str:
        return self._git.out("show", f"{self._c.baseline}:VERSION")

    def ensure_baseline(self) -> None:
        git, baseline, r = self._git, self._c.baseline, self._r
        git.run("fetch", "origin", baseline, quiet=True)  # best-effort
        if git.ok("show-ref", "--verify", "--quiet", f"refs/heads/{baseline}"):
            return
        if git.ok("show-ref", "--verify", "--quiet", f"refs/remotes/origin/{baseline}"):
            git.run("branch", baseline, f"origin/{baseline}")
            return
        r.log(f"No {baseline} branch — bootstrapping from the install commit")
        root = ""
        out = git.out("log", "--grep=^Initialize ninja", "--format=%H")
        if out:
            root = out.splitlines()[-1]
        if not root:
            out = git.out("rev-list", "--max-parents=0", "HEAD")
            root = out.splitlines()[-1] if out else ""
        git.run("branch", baseline, root)
        r.log_warn(
            f"Bootstrapped {baseline} at {git.out('rev-parse', '--short', root)} "
            "— first merge may be heavier"
        )

    def update_baseline(self, staging_dir: Path, new_version: str) -> None:
        git, baseline = self._git, self._c.baseline
        worktree = Path(tempfile.mkdtemp(prefix="ninja-baseline."))
        try:
            git.run("worktree", "add", "-f", str(worktree), baseline)
            # --checksum: same-length edits (e.g. 0.1.0 → 0.1.1) are otherwise
            # skipped by rsync's default size+mtime quick-check.
            subprocess.run(
                [
                    "rsync",
                    "-a",
                    "--checksum",
                    "--delete",
                    "--exclude=.git",
                    f"{staging_dir}/ninja/",
                    f"{worktree}/",
                ],
                check=True,
            )
            git.run("add", "-A", cwd=worktree)
            if git.ok("diff", "--cached", "--quiet", cwd=worktree):
                self._r.log(
                    f"Baseline already at v{new_version} content — nothing to commit"
                )
            else:
                git.run(
                    "commit",
                    "-q",
                    "-m",
                    f"ninja v{new_version}",
                    identity=True,
                    cwd=worktree,
                )
        except subprocess.CalledProcessError as e:
            # Would otherwise escape run(), which only catches Done — no result
            # marker, no telemetry, just a traceback.
            self._r.log_error(f"staging rsync into the baseline worktree failed: {e}")
            raise Done(1, Outcome.ERROR)
        finally:
            git.run("worktree", "remove", "--force", str(worktree))

        # One post-condition instead of checking each step: a failed worktree
        # add, rsync, add or commit all end the same way — the baseline does not
        # carry the new version. Without this the caller sees an unmoved
        # baseline, concludes "already merged", and reports UP_TO_DATE for an upgrade that never happened.
        landed = self.baseline_version()
        if landed != new_version:
            self._r.log_error(
                f"baseline is at v{landed or '(unknown)'} after update, expected "
                f"v{new_version} — not merging"
            )
            raise Done(1, Outcome.ERROR)

    def rewind_baseline(self, baseline_prev: str) -> None:
        """Rewind the local baseline to its pre-update tip so the next run
        re-detects the upgrade — i.e. a re-click retries instead of the box
        reporting "up to date" with the upgrade left unfinished."""
        if baseline_prev:
            self._git.run("branch", "-f", self._c.baseline, baseline_prev)

    # --- conflict resolution (agent harness) ------------------------------
    def resolve_conflicts(self, new_version: str) -> bool:
        git, r = self._git, self._r
        conflicted = git.conflicted_files()
        if not conflicted:
            return True
        r.notify("conflict", f"LLM resolving: {git.conflicted_pretty()}")
        joined = "\n".join(conflicted)
        branch = git.current_branch()
        prompt = (
            "You are resolving git merge conflicts during an automated upgrade.\n"
            f"Repo: {self._c.ninja_home} (branch {branch}).\n"
            "For each file below, the conflict markers delimit two sides:\n"
            "  <<<<<<< HEAD          = the CUSTOMER's own edit (preserve their intent)\n"
            "  =======\n"
            f"  >>>>>>> {self._c.baseline}     = DEV's published change for "
            f"v{new_version} (apply the fix)\n"
            "Merge both intents where possible. Where the two sides genuinely conflict "
            "and cannot both be kept, KEEP THE CUSTOMER'S value (customer-wins) — never "
            "let dev's change overwrite a customer edit. Remove ALL conflict markers. "
            "Edit files in place; do not touch anything else. Conflicted files:\n"
            f"{joined}"
        )
        self._resolver.resolve(prompt, conflicted)
        git.run("add", "-A")
        if git.needs_resolution():
            return False
        # Backstop scan for leftover conflict markers. git grep exits: 0 = found
        # (not resolved), 1 = none found (resolved), 2 = error. Only exit 1 counts
        # as resolved — treat 0 AND errors as unresolved so a grep failure fails
        # safe (→ customer-wins fallback) rather than committing unverified content.
        rc = git.run(
            "grep", "-lE", r"^(<<<<<<<|=======|>>>>>>>)", "--", ".", quiet=True
        ).returncode
        return rc == 1

    # --- merge ------------------------------------------------------------
    def _take_upstream_owned(self, baseline: str) -> None:
        git = self._git
        changed = []
        for f in self._c.upstream_owned:
            if not git.ok("cat-file", "-e", f"{baseline}:{f}"):
                continue
            if not git.ok("diff", "--quiet", f"HEAD:{f}", f"{baseline}:{f}"):
                if not git.ok(
                    "checkout", baseline, "--", f, identity=True, quiet=False
                ):
                    continue
                git.run("add", "--", f, identity=True)
                changed.append(f)
        if changed:
            git.run("commit", "--amend", "--no-edit", identity=True)
            self._r.log(
                f"upstream-owned: forced upstream version for: {' '.join(changed)} "
            )

    def merge_upstream(
        self, new_version: str, main_branch: str, baseline_prev: str
    ) -> None:
        git, baseline, r = self._git, self._c.baseline, self._r
        git.run("tag", "-f", f"pre-upgrade-v{new_version}")
        r.log(f"Merging {baseline} (v{new_version}) into {main_branch}")
        msg = f"Merge ninja v{new_version} into {main_branch}"
        if git.ok("merge", "-m", msg, baseline, identity=True):
            return  # clean merge

        conflicted = git.conflicted_pretty()

        # LLM (claude code) first — it merges both sides rather than blindly
        # keeping one; a bad merge is caught by the post-merge smoke/health gate.
        if self.resolve_conflicts(new_version):
            git.run("commit", "-m", f"{msg} (conflicts resolved by LLM)", identity=True)
            r.log("Conflicts resolved by LLM")
            return

        # LLM couldn't fully resolve — fall back to customer-wins so the
        # customer's edits are never lost; a human is the last resort.
        r.log_warn(
            f"LLM could not fully resolve {conflicted} — falling back to customer-wins"
        )
        git.cancel_merge()
        cw_msg = f"{msg} (customer edits kept on conflicts)"
        if git.ok("merge", "-X", "ours", "-m", cw_msg, baseline, identity=True):
            self._take_upstream_owned(baseline)
            r.log_warn(
                f"customer-wins: kept customer edits over dev changes on conflicting "
                f"lines in: {conflicted}"
            )
            r.notify(
                "info",
                f"kept your edits on conflicting lines in: {conflicted}— dev's changes "
                "there were held back for review",
            )
            return

        # tree-level (modify/delete) conflicts -X ours can't handle
        unresolved = git.conflicted_files()
        if unresolved:
            held_back = []
            for f in unresolved:
                if not f:
                    continue
                if git.ok("show", f":2:{f}"):  # customer kept the file
                    git.run("checkout", "--ours", "--", f)
                    git.run("add", "--", f, identity=True)
                else:  # customer deleted it
                    git.run("rm", "--", f, identity=True)
                held_back.append(f)
            if not git.needs_resolution():
                if not git.ok(
                    "commit", "--no-edit", "-m", cw_msg, identity=True, quiet=False
                ):
                    git.run("commit", "-m", cw_msg, identity=True)
                r.log_warn(
                    "customer-wins: resolved tree-level (modify/delete) conflicts by "
                    f"keeping customer state in: {' '.join(held_back)} "
                )
                r.notify(
                    "conflict",
                    f"kept your files on modify/delete conflicts in: {' '.join(held_back)}"
                    "— dev's changes there were held back for review",
                )
                return

        unresolved_pretty = git.conflicted_pretty()
        git.cancel_merge()
        # Rewind so the box reports the upgrade as still pending (not "up to
        # date") — a re-click after the human resolves it will re-attempt.
        self.rewind_baseline(baseline_prev)
        r.notify(
            "error",
            f"upgrade to v{new_version} needs a human — customer-wins couldn't "
            f"auto-resolve: {unresolved_pretty}. {main_branch} untouched.",
        )
        raise Done(1, Outcome.CONFLICT)
