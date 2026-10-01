#!/usr/bin/env python3
"""
gh-shim — a minimal ``gh`` (GitHub CLI) shim that speaks Gitea ``/api/v1``.

Installed as ``/usr/local/bin/gh`` by ``ninja-install.sh``, ahead of the
apt-installed ``/usr/bin/gh`` on PATH. Phantom is trained on ``gh`` — the
orchestrator, the token resolver, the health checks and ``tools/issues.py`` all
shell out to it — so this maps the subcommands it uses onto Gitea and normalises
responses back to the GitHub-ish shape those callers parse (notably
``issue list --json ...`` → objects with
``number/title/state/url/body/labels:[{name}]/createdAt``).

Fail-safe: with no ``GITEA_URL`` resolvable every call is ``exec``'d to the real
``gh``, so a GitHub sandbox that pulls this dist is unchanged. Set
``NINJA_GH_SHIM_PASSTHROUGH=1`` to force that path.

Gitea credentials are mounted on every Dedicated Sandbox regardless of
entitlement, so their presence alone does not mean this deploy is Gitea. If
sandbox metadata names a ``git_provider`` explicitly, that wins: ``github``
forces passthrough even with Gitea creds present; ``gitea`` activates this
shim. Only when metadata has no such key at all (ninja-in-a-box, or a deploy
predating the key) do we fall back to probing by credential presence, mirroring
``ninja-install.sh``'s ``resolve_git_provider()``.

Single tenant, so there is no org: the owner is always ``GITEA_USER`` and repos
are created with ``POST /user/repos``.

Stdlib only and importable, so it keeps working when PYTHONPATH is unset or
``/workspace/ninja`` is mid-rebase, and is still unit-testable.

Configuration comes from the env, else from the credential file the Gitea
container writes and the host bind-mounts read-only:
  GITEA_URL    e.g. http://172.20.0.3:3000
  GITEA_USER   the admin user that owns every repo
  GITEA_TOKEN  its access token

Anything not mapped here is delegated to ``tea`` if present, else errors.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# Read directly, not just from the env, so `gh` works from any shell.
GITEA_CRED_FILE = os.environ.get("GITEA_CRED_FILE") or "/gitea-cred/gitea.env"

# Same tmpfs-then-durable resolution as ninja-install.sh's sandbox_metadata_file().
SANDBOX_METADATA_FILE = (
    os.environ.get("SANDBOX_METADATA_FILE") or "/dev/shm/sandbox_metadata.json"
)
SANDBOX_METADATA_DURABLE_FILE = "/root/.sandbox_metadata.json"

# The real, apt-installed gh, used for the passthrough above.
REAL_GH = os.environ.get("NINJA_REAL_GH") or "/usr/bin/gh"

TEA_BIN = os.environ.get("TEA_BIN") or "/usr/local/bin/tea"

# git-askpass.sh and token_resolver.py read the token from here, under the key
# `Github` regardless of the actual git host.
TOKEN_FILE = "/dev/shm/mcp-token"

API_TIMEOUT = 30

# Gitea caps `limit` per page; 200 matches what tools/issues.py asks gh for.
DEFAULT_ISSUE_LIMIT = "200"

VERSION = "gh-shim (ninja Gitea shim) 1.0.0"

# The phantom's own checkout. Used as a last-resort repo hint when `gh` is
# invoked from a cwd that is not a git work tree.
REPO_ROOT = "/workspace/ninja"


def _eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_cred_cache: dict[str, str] | None = None
_provider_cache: str | None = None


def _cred_file_values() -> dict[str, str]:
    """Parse GITEA_* assignments out of the credential file (cached)."""
    global _cred_cache
    if _cred_cache is not None:
        return _cred_cache
    values: dict[str, str] = {}
    try:
        with open(GITEA_CRED_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.removeprefix("export ").strip()
                values[key] = val.strip().strip("'\"")
    except OSError:
        pass
    _cred_cache = values
    return values


def _setting(name: str) -> str:
    """Env wins over the credential file, so a caller can always override."""
    return (os.environ.get(name) or _cred_file_values().get(name) or "").strip()


def gitea_url() -> str:
    return _setting("GITEA_URL").rstrip("/")


def gitea_user() -> str:
    return _setting("GITEA_USER")


def _git_provider_from_metadata() -> str:
    """``git_provider`` from sandbox metadata, or "" when the key/file is absent.

    Mirrors ninja-install.sh's sandbox_metadata_file(): tmpfs first, falling
    back to the durable copy a restart did not wipe. Duplicated here (rather
    than importing core.metadata) to keep this module stdlib-only.
    """
    global _provider_cache
    if _provider_cache is not None:
        return _provider_cache
    provider = ""
    for path in (SANDBOX_METADATA_FILE, SANDBOX_METADATA_DURABLE_FILE):
        try:
            with open(path, encoding="utf-8") as fh:
                provider = (json.load(fh).get("git_provider") or "").strip()
            break
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    _provider_cache = provider
    return provider


def _read_token_file() -> str:
    """Read the token from the `Github={"access_token": ...}` line."""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            for line in fh.read().strip().splitlines():
                if line.startswith("Github="):
                    return json.loads(line[len("Github=") :]).get("access_token", "")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return ""


def _token() -> str:
    return _setting("GITEA_TOKEN") or _read_token_file()


# ---------------------------------------------------------------------------
# Gitea API
# ---------------------------------------------------------------------------


def _ok(status: int) -> bool:
    """Any 2xx.

    Worth a named helper rather than inline literals: Gitea answers
    ``PATCH /issues/{n}`` with **201**, not 200. A shim that insisted on 200
    would report every `gh issue close` as a failure while the issue was in
    fact closed — and issues.py runs that call with ``check=True``.
    """
    return 200 <= status < 300


def _api(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    """Call Gitea /api/v1; return (status, parsed-json-or-text). 0 = no answer."""
    url = f"{gitea_url()}/api/v1{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    tok = _token()
    if tok:
        req.add_header("Authorization", f"token {tok}")
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            raw = resp.read().decode() or ""
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        return 0, str(exc)


# ---------------------------------------------------------------------------
# argv helpers
# ---------------------------------------------------------------------------


def _pop_flag(args: list[str], name: str, multiple: bool = False):
    """Remove `--name value` / `--name=value` from args and return the value(s)."""
    vals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == name and i + 1 < len(args):
            vals.append(args[i + 1])
            del args[i : i + 2]
            continue
        if arg.startswith(f"{name}="):
            vals.append(arg[len(name) + 1 :])
            del args[i]
            continue
        i += 1
    if multiple:
        return vals
    return vals[0] if vals else None


def _first_positional(args: list[str]) -> str:
    return next((a for a in args if not a.startswith("-")), "")


def _jq_lite(obj: object, expr: str) -> tuple[bool, object]:
    """Evaluate the `-q` expressions gh callers actually use: `.`, `.a`, `.a.b`.

    Returns (handled, value). Phantom's orchestrator runs
    `gh repo view --json nameWithOwner -q .nameWithOwner`; the in-a-box shim
    ignored both flags and only worked because printing `full_name` happened to
    be what the caller wanted. Handle it explicitly instead.
    """
    expr = expr.strip()
    if expr in ("", "."):
        return True, obj
    if not expr.startswith(".") or any(c in expr for c in "[]|()$ "):
        return False, None
    cur = obj
    for part in expr.lstrip(".").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return True, None
        cur = cur[part]
    return True, cur


def _emit(obj: object, jq: str | None) -> int:
    """Print an API result the way `gh --json [-q]` would."""
    if jq:
        handled, val = _jq_lite(obj, jq)
        if not handled:
            _eprint(f"gh-shim: unsupported -q expression {jq!r}; printing JSON")
        else:
            if val is None:
                print()
            elif isinstance(val, (dict, list)):
                print(json.dumps(val))
            elif isinstance(val, bool):
                print("true" if val else "false")
            else:
                print(val)
            return 0
    print(json.dumps(obj))
    return 0


# ---------------------------------------------------------------------------
# Repo resolution
# ---------------------------------------------------------------------------


def _split_repo_arg(arg: str) -> tuple[str, str]:
    """Split `owner/name` or bare `name` into (owner, repo).

    A caller-supplied owner is ignored: a dedicated sandbox is single-tenant, so
    every repo lives under GITEA_USER, the only account that exists and the only
    one the token authenticates as.
    """
    owner, _, name = arg.removesuffix(".git").rpartition("/")
    return gitea_user() or owner, name


def _repo_from_remote(cwd: str | None = None) -> tuple[str, str] | None:
    """Read (owner, repo) off the `origin` remote, as gh does for a checkout."""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    # Reduce e.g. http://user:tok@172.20.0.3:3000/ninja-admin/repo.git, or the
    # scp-like git@host:ninja-admin/repo.git, down to the owner/name pair.
    if "://" in out:
        path = out.split("://", 1)[1].split("@", 1)[-1].partition("/")[2]
    else:
        stripped = out.split("@", 1)[-1]
        path = stripped.partition(":")[2] or stripped
    segs = [s for s in path.removesuffix(".git").split("/") if s]
    if len(segs) >= 2:
        return segs[-2], segs[-1]
    return None


def _resolve_repo() -> tuple[str, str]:
    """Resolve (owner, repo) the way gh does, with Gitea-shaped fallbacks.

    Order: GH_REPO (gh honours it first, and orchestrator.github_repo_name()
    sets it in local dev) → the origin remote of cwd → the origin remote of
    /workspace/ninja → GITEA_USER plus the cwd basename.
    """
    env_repo = os.environ.get("GH_REPO", "").strip()
    if env_repo:
        return _split_repo_arg(env_repo)
    for cwd in (None, REPO_ROOT):
        if cwd and not os.path.isdir(cwd):
            continue
        pair = _repo_from_remote(cwd)
        if pair:
            return pair
    owner = gitea_user()
    if owner:
        return owner, os.path.basename(os.getcwd())
    _eprint("gh-shim: cannot resolve repo (no GH_REPO, no git remote, no GITEA_USER)")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

_label_cache: dict[tuple[str, str], dict[str, int]] = {}


def _labels_map(owner: str, repo: str) -> dict[str, int]:
    key = (owner, repo)
    if key in _label_cache:
        return _label_cache[key]
    status, body = _api("GET", f"/repos/{owner}/{repo}/labels?limit=200")
    mapping: dict[str, int] = {}
    if _ok(status) and isinstance(body, list):
        mapping = {
            lbl.get("name"): lbl.get("id")
            for lbl in body
            if lbl.get("name") and lbl.get("id") is not None
        }
        _label_cache[key] = mapping
    return mapping


def _label_id(owner: str, repo: str, name: str) -> int | None:
    return _labels_map(owner, repo).get(name)


def _create_label(
    owner: str, repo: str, name: str, color: str = "ededed", desc: str = ""
) -> int | None:
    status, body = _api(
        "POST",
        f"/repos/{owner}/{repo}/labels",
        {"name": name, "color": color, "description": desc},
    )
    if _ok(status) and isinstance(body, dict):
        _label_cache.pop((owner, repo), None)
        return body.get("id")
    return None


def _label_ids(owner: str, repo: str, names: list[str]) -> list[int]:
    """Map label names to ids, creating any that do not exist yet.

    tools/issues.py stamps `ninja` on every issue it files and `blocked` on ones
    it cannot progress, and expects labelling to just work on a fresh repo.
    """
    existing = _labels_map(owner, repo)
    ids: list[int] = []
    for name in names:
        lid = existing.get(name)
        if lid is None:
            lid = _create_label(owner, repo, name)
        if lid is not None:
            ids.append(lid)
    return ids


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# gh field name → Gitea field name, for the `--json` fields Phantom asks for.
_ISSUE_FIELDS = {
    "number": "number",
    "title": "title",
    "state": "state",
    "url": "html_url",
    "body": "body",
    "createdAt": "created_at",
    "labels": "labels",
    "updatedAt": "updated_at",
    "closedAt": "closed_at",
}


def _normalize_issue(it: dict, fields: list[str] | None = None) -> dict:
    """Map a Gitea issue onto the GitHub-ish shape callers parse."""
    full = {
        "number": it.get("number"),
        "title": it.get("title"),
        "state": it.get("state"),
        "url": it.get("html_url") or it.get("url"),
        "body": it.get("body"),
        "createdAt": it.get("created_at"),
        "updatedAt": it.get("updated_at"),
        "closedAt": it.get("closed_at"),
        "labels": [
            {"name": lbl.get("name")} for lbl in (it.get("labels") or []) if lbl
        ],
    }
    if not fields:
        # Drop the fields nobody asked for so the default shape stays the one
        # tools/issues.py has always seen.
        return {k: v for k, v in full.items() if k not in ("updatedAt", "closedAt")}
    return {k: full[k] for k in fields if k in full}


# ---------------------------------------------------------------------------
# Delegation / passthrough
# ---------------------------------------------------------------------------


def _delegate_to_tea(argv: list[str]) -> int:
    """Hand an unmapped subcommand to `tea`, or error if it is not installed."""
    if os.access(TEA_BIN, os.X_OK):
        _eprint(f"gh-shim: delegating to tea: {' '.join(argv)}")
        return subprocess.run([TEA_BIN, *argv]).returncode
    _eprint(f"gh-shim: unsupported command and no tea fallback: gh {' '.join(argv)}")
    return 2


def _passthrough(argv: list[str]) -> int:
    """No Gitea here — become the real gh so a GitHub-era sandbox is untouched.

    This is the rollout guard: the dist zip reaches existing GitHub sandboxes on
    their next ninja-upgrade tick, and `gh` failing there would take the agent's
    work queue down with it.
    """
    if not os.access(REAL_GH, os.X_OK):
        _eprint(
            f"gh-shim: GITEA_URL is not set and no real gh at {REAL_GH} — "
            "cannot run `gh "
            f"{' '.join(argv)}`"
        )
        return 2
    if os.path.realpath(REAL_GH) == os.path.realpath(__file__):
        _eprint(f"gh-shim: {REAL_GH} is this shim — refusing to exec itself")
        return 2
    os.execv(REAL_GH, ["gh", *argv])
    return 0  # not reached; execv replaces the process


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_auth(args: list[str]) -> int:
    sub = args[0] if args else ""

    if sub == "login":
        # `--with-token` feeds the token on stdin. Drain it so the caller's
        # write never blocks, then confirm the injected token authenticates:
        # token_resolver.py pipes the mcp-token value here and only reads the
        # exit status.
        if not sys.stdin.isatty():
            try:
                sys.stdin.read()
            except OSError:
                pass
        status, _body = _api("GET", "/user")
        if _ok(status):
            _eprint(f"gh-shim: authenticated to Gitea at {gitea_url()}")
            return 0
        _eprint(f"gh-shim: auth login failed (HTTP {status})")
        return 1

    if sub == "status":
        status, body = _api("GET", "/user")
        if _ok(status) and isinstance(body, dict):
            login = body.get("login") or gitea_user()
            print(f"{gitea_url()}\n  ✓ Logged in to {gitea_url()} account {login}")
            return 0
        _eprint(f"gh-shim: not authenticated to Gitea (HTTP {status})")
        return 1

    if sub == "token":
        tok = _token()
        if not tok:
            _eprint("gh-shim: no Gitea token available")
            return 1
        print(tok)
        return 0

    return _delegate_to_tea(["logins", *args]) if sub else _delegate_to_tea(args)


def cmd_issue(args: list[str]) -> int:
    sub = args[0] if args else ""
    rest = list(args[1:])
    owner, repo = _resolve_repo()
    base = f"/repos/{owner}/{repo}/issues"

    if sub == "list":
        fields = _pop_flag(rest, "--json")
        jq = _pop_flag(rest, "-q") or _pop_flag(rest, "--jq")
        limit = _pop_flag(rest, "--limit") or DEFAULT_ISSUE_LIMIT
        state = _pop_flag(rest, "--state") or "open"
        labels = _pop_flag(rest, "--label", multiple=True)
        query = {"state": state, "limit": limit, "type": "issues"}
        if labels:
            query["labels"] = ",".join(labels)
        status, body = _api("GET", f"{base}?{urllib.parse.urlencode(query)}")
        if not _ok(status) or not isinstance(body, list):
            _eprint(f"gh-shim: issue list failed (HTTP {status}): {body}")
            return 1
        wanted = [f.strip() for f in fields.split(",")] if fields else None
        return _emit([_normalize_issue(it, wanted) for it in body], jq)

    if sub == "create":
        title = _pop_flag(rest, "--title") or ""
        body_text = _pop_flag(rest, "--body") or ""
        labels = _pop_flag(rest, "--label", multiple=True)
        payload: dict = {"title": title, "body": body_text}
        if labels:
            payload["labels"] = _label_ids(owner, repo, labels)
        status, resp = _api("POST", base, payload)
        if _ok(status) and isinstance(resp, dict):
            print(resp.get("html_url") or resp.get("url") or "")
            return 0
        _eprint(f"gh-shim: issue create failed (HTTP {status}): {resp}")
        return 1

    if sub == "comment":
        body_text = _pop_flag(rest, "--body") or ""
        number = _first_positional(rest)
        status, resp = _api("POST", f"{base}/{number}/comments", {"body": body_text})
        if _ok(status):
            if isinstance(resp, dict) and resp.get("html_url"):
                print(resp["html_url"])
            return 0
        _eprint(f"gh-shim: issue comment failed (HTTP {status}): {resp}")
        return 1

    if sub == "close":
        comment = _pop_flag(rest, "--comment")
        number = _first_positional(rest)
        if comment:
            _api("POST", f"{base}/{number}/comments", {"body": comment})
        status, resp = _api("PATCH", f"{base}/{number}", {"state": "closed"})
        if _ok(status):
            return 0
        _eprint(f"gh-shim: issue close failed (HTTP {status}): {resp}")
        return 1

    if sub == "reopen":
        number = _first_positional(rest)
        status, resp = _api("PATCH", f"{base}/{number}", {"state": "open"})
        if _ok(status):
            return 0
        _eprint(f"gh-shim: issue reopen failed (HTTP {status}): {resp}")
        return 1

    if sub == "edit":
        add = _pop_flag(rest, "--add-label", multiple=True)
        remove = _pop_flag(rest, "--remove-label", multiple=True)
        title = _pop_flag(rest, "--title")
        body_text = _pop_flag(rest, "--body")
        number = _first_positional(rest)
        ok = True
        if add:
            status, resp = _api(
                "POST",
                f"{base}/{number}/labels",
                {"labels": _label_ids(owner, repo, add)},
            )
            if not _ok(status):
                _eprint(f"gh-shim: add-label failed (HTTP {status}): {resp}")
                ok = False
        for name in remove:
            lid = _label_id(owner, repo, name)
            if lid is None:
                # gh treats removing an absent label as an error; the work queue
                # calls this unconditionally in unblock_issue, so absent means
                # already-removed and the caller's intent is satisfied.
                continue
            status, resp = _api("DELETE", f"{base}/{number}/labels/{lid}")
            if not _ok(status):
                _eprint(f"gh-shim: remove-label failed (HTTP {status}): {resp}")
                ok = False
        patch = {}
        if title is not None:
            patch["title"] = title
        if body_text is not None:
            patch["body"] = body_text
        if patch:
            status, resp = _api("PATCH", f"{base}/{number}", patch)
            if not _ok(status):
                _eprint(f"gh-shim: issue edit failed (HTTP {status}): {resp}")
                ok = False
        return 0 if ok else 1

    if sub == "view":
        fields = _pop_flag(rest, "--json")
        jq = _pop_flag(rest, "-q") or _pop_flag(rest, "--jq")
        number = _first_positional(rest)
        status, resp = _api("GET", f"{base}/{number}")
        if not _ok(status) or not isinstance(resp, dict):
            _eprint(f"gh-shim: issue view failed (HTTP {status}): {resp}")
            return 1
        wanted = [f.strip() for f in fields.split(",")] if fields else None
        if not fields and not jq:
            print(f"#{resp.get('number')} {resp.get('title')}\n")
            print(resp.get("body") or "")
            return 0
        return _emit(_normalize_issue(resp, wanted), jq)

    return _delegate_to_tea(["issues", *args])


def cmd_label(args: list[str]) -> int:
    sub = args[0] if args else ""
    rest = list(args[1:])
    owner, repo = _resolve_repo()

    if sub == "create":
        color = (_pop_flag(rest, "--color") or "").lstrip("#") or "ededed"
        desc = _pop_flag(rest, "--description") or ""
        # `--force` is accepted and ignored: this handler is idempotent, which
        # is the only behaviour tools/issues.py wants from it.
        if "--force" in rest:
            rest.remove("--force")
        name = _first_positional(rest) or _pop_flag(rest, "--name")
        if not name:
            _eprint("gh-shim: label create requires a name")
            return 1
        if _label_id(owner, repo, name) is not None:
            return 0
        if _create_label(owner, repo, name, color, desc) is not None:
            return 0
        # A concurrent create is a success from the caller's point of view.
        if _label_id(owner, repo, name) is not None:
            return 0
        _eprint(f"gh-shim: label create failed for {name!r}")
        return 1

    if sub == "list":
        fields = _pop_flag(rest, "--json")
        jq = _pop_flag(rest, "-q") or _pop_flag(rest, "--jq")
        labels = [
            {"name": name, "id": lid} for name, lid in _labels_map(owner, repo).items()
        ]
        if fields or jq:
            return _emit(labels, jq)
        for lbl in labels:
            print(lbl["name"])
        return 0

    return _delegate_to_tea(["labels", *args])


# gh field name → value, for `gh repo view --json ...`.
def _repo_json(resp: dict, owner: str, name: str) -> dict:
    return {
        "name": resp.get("name") or name,
        "nameWithOwner": resp.get("full_name") or f"{owner}/{name}",
        "owner": {"login": (resp.get("owner") or {}).get("login") or owner},
        "url": resp.get("html_url") or f"{gitea_url()}/{owner}/{name}",
        "description": resp.get("description") or "",
        "isPrivate": bool(resp.get("private")),
        "defaultBranchRef": {"name": resp.get("default_branch") or ""},
        "sshUrl": resp.get("ssh_url") or "",
    }


def cmd_repo(args: list[str]) -> int:
    sub = args[0] if args else ""
    rest = list(args[1:])

    if sub == "create":
        fields = _pop_flag(rest, "--json")
        jq = _pop_flag(rest, "-q") or _pop_flag(rest, "--jq")
        name_arg = _first_positional(rest)
        if not name_arg:
            _eprint("gh-shim: repo create requires a name")
            return 1
        owner, name = _split_repo_arg(name_arg)
        if "/" in name_arg and name_arg.split("/", 1)[0] != owner:
            _eprint(
                f"gh-shim: ignoring owner in {name_arg!r} — a dedicated sandbox "
                f"is single-tenant, creating under {owner!r}"
            )
        payload = {
            "name": name,
            "private": "--public" not in rest,
            "auto_init": "--add-readme" in rest,
        }
        # POST /user/repos, not /orgs/{org}/repos: there is no org here, the
        # token authenticates as GITEA_USER, and that user owns every repo.
        status, resp = _api("POST", "/user/repos", payload)
        if _ok(status) and isinstance(resp, dict):
            if fields or jq:
                return _emit(_repo_json(resp, owner, name), jq)
            print(resp.get("html_url") or f"{gitea_url()}/{owner}/{name}")
            return 0
        if status == 409:
            # Idempotent by design: ninja-install.sh creates the workspace repo
            # on every boot and must tolerate the one that is already there.
            _eprint(f"gh-shim: repo {owner}/{name} already exists")
            print(f"{gitea_url()}/{owner}/{name}")
            return 0
        _eprint(f"gh-shim: repo create failed (HTTP {status}): {resp}")
        return 1

    if sub == "view":
        fields = _pop_flag(rest, "--json")
        jq = _pop_flag(rest, "-q") or _pop_flag(rest, "--jq")
        arg = _first_positional(rest)
        owner, name = _split_repo_arg(arg) if arg else _resolve_repo()
        status, resp = _api("GET", f"/repos/{owner}/{name}")
        if not _ok(status) or not isinstance(resp, dict):
            _eprint(f"gh-shim: repo view failed (HTTP {status}): {resp}")
            return 1
        full = _repo_json(resp, owner, name)
        if not fields and not jq:
            # Bare `gh repo view` prints a human summary; the one thing callers
            # here have ever parsed off it is the owner/name pair.
            print(full["nameWithOwner"])
            return 0
        if fields:
            wanted = [f.strip() for f in fields.split(",")]
            unknown = [f for f in wanted if f not in full]
            if unknown:
                _eprint(f"gh-shim: unsupported repo fields ignored: {unknown}")
            full = {k: full[k] for k in wanted if k in full}
        return _emit(full, jq)

    if sub == "clone":
        arg = _first_positional(rest)
        if not arg:
            _eprint("gh-shim: repo clone requires a name")
            return 1
        owner, name = _split_repo_arg(arg)
        positional = [a for a in rest if not a.startswith("-")]
        dest = positional[1] if len(positional) > 1 else name
        url = f"{gitea_url()}/{owner}/{name}.git"
        extra = rest[rest.index("--") + 1 :] if "--" in rest else []
        return subprocess.run(["git", "clone", url, dest, *extra]).returncode

    if sub == "delete":
        arg = _first_positional(rest)
        owner, name = _split_repo_arg(arg) if arg else _resolve_repo()
        status, resp = _api("DELETE", f"/repos/{owner}/{name}")
        if _ok(status) or status == 404:
            return 0
        _eprint(f"gh-shim: repo delete failed (HTTP {status}): {resp}")
        return 1

    return _delegate_to_tea(["repos", *args])


def cmd_run(args: list[str]) -> int:
    """GitHub Actions. Deliberately unsupported rather than silently delegated.

    Nothing in Phantom calls this; it only ever appeared in the agent system
    prompt, which is being reworded. An explicit error beats a `tea` delegation
    that cannot mean anything here.
    """
    _eprint(
        "gh-shim: `gh run` is not supported — this sandbox's git host is Gitea "
        "and has no GitHub Actions surface."
    )
    return 2


def cmd_pr(args: list[str]) -> int:
    # Not used by Phantom today; delegate so manual/agent use still works.
    return _delegate_to_tea(["pulls", *args])


HANDLERS = {
    "auth": cmd_auth,
    "issue": cmd_issue,
    "label": cmd_label,
    "repo": cmd_repo,
    "pr": cmd_pr,
    "run": cmd_run,
}


def main(argv: list[str]) -> int:
    if not argv:
        _eprint("gh-shim: no subcommand (try `gh issue list`)")
        return 2

    # Fail-safe first, so a GitHub-era sandbox behaves exactly as it did before
    # this dist reached it - including for `gh --version`. An explicit
    # git_provider="github" in sandbox metadata forces this path too, even with
    # Gitea creds mounted (they are mounted on every sandbox regardless of
    # entitlement) - an absent metadata key falls back to probing by credential
    # presence, same as ninja-install.sh's resolve_git_provider().
    provider = _git_provider_from_metadata()
    if (
        os.environ.get("NINJA_GH_SHIM_PASSTHROUGH") == "1"
        or provider == "github"
        or not gitea_url()
    ):
        return _passthrough(argv)

    if argv[0] in ("--version", "version"):
        print(VERSION)
        return 0
    if argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        print(f"\nMapped groups: {', '.join(sorted(HANDLERS))}")
        return 0

    handler = HANDLERS.get(argv[0])
    if handler is None:
        return _delegate_to_tea(argv)
    return handler(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
