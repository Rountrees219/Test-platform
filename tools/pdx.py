#!/usr/bin/env python3
"""
tools/pdx.py — Pipedream Connect CLI
======================================

JSON-first CLI for the Pipedream Connect integrations gateway. Routes through LiteLLM
so Phantom never talks to the gateway directly.

Every command prints a single JSON object on stdout and exits 0 on success
or non-zero on error (with {"ok": false, "error": "..."}).

Subcommands
-----------
    pdx status
        Show the resolved unique_channel and gateway URL.
        Use this to verify configuration before running other commands.

    pdx health
        GET /ninja/integrations-gateway/health — no auth required.
        Returns the gateway health response as JSON.

    pdx connect-link [--event-id ID]
        Get a short-lived OAuth connection link for the user (expires 30 min).
        Calls get_connection_link on ninja_integrations_gateway_system via direct MCP REST —
        no LLM involved. Post the returned link to the user in chat.

    pdx apps [--q QUERY] [--limit N]
        Browse the Pipedream app catalog (all apps, connected or not).
        Calls GET /ninja/integrations-gateway/apps on the gateway.

    pdx actions <app_slug>
        Enumerate the actions available for an app.
        Calls GET /ninja/integrations-gateway/actions on the gateway.

    pdx describe <action_key>
        Show the JSON-schema-ish props for a specific action — what the
        LLM needs to supply when running it.
        Calls GET /ninja/integrations-gateway/actions/describe on the gateway.

    pdx run <action_key> [--args JSON] [--arg k=v ...] [--event-id ID]
        Invoke an action on behalf of the onboarded user.
        Calls POST /ninja/integrations-gateway/actions/run on the gateway.

    pdx http <app_slug> <METHOD> <url>
             [--json JSON] [--json-file PATH] [--data STR]
             [--header K:V ...] [--query k=v ...]
             [--event-id ID]
        Make a raw authenticated HTTP request through the Pipedream proxy — no LLM
         involved. The gateway resolves credentials from x-ninja-integration-channel-id
        and proxies the call upstream.

        Positional:
            app_slug           App slug (e.g. 'github', 'gmail').
            METHOD             HTTP method (GET, POST, PUT, PATCH, DELETE).
            url                Upstream URL (e.g. 'https://api.github.com/user').

        Options:
            --json JSON        JSON body as a string.
            --json-file PATH   Path to a file whose contents are the JSON body.
                               Streams a large body past the single-argv per-arg
                               limit. Takes precedence over --json and --data.
            --data STR         Raw body string.
            --header K:V       Extra header to forward upstream (repeatable).
            --query k=v        Query string parameter (repeatable).
            --event-id ID      x-ninja-event-id traceability header.

        Body precedence: --json-file, then --json, then --data.

    pdx tools [--apps SLUG,SLUG] [--limit N]
        Emit OpenAI-style function-calling schema for every action of
        every connected app (or a filtered subset). Feed this directly
        to tools=[...] in an LLM request.

Exit codes
----------
    0   success
    1   usage / bad arguments
    2   configuration error (unique_channel cannot be resolved, agent_settings.json missing fields)
    3   runtime error (HTTP 4xx/5xx, tool returned isError: true, unexpected response)

Examples
--------
    pdx status
    pdx health
    pdx apps --q "google"
    pdx actions github
    pdx describe github-create-issue
    pdx run github-create-issue --arg repoFullname=acme/repo --arg title="Bug fix"
    pdx connect-link
    pdx http github GET https://api.github.com/user
    pdx http github POST https://api.github.com/repos/acme/repo/issues \\
        --json '{"title": "Bug fix", "body": "Details here"}'
    pdx http gmail GET https://www.googleapis.com/gmail/v1/users/me/profile
    pdx http hubspot POST https://api.hubapi.com/crm/v3/objects/contacts \\
        --json '{"properties": {"email": "user@example.com"}}'
    pdx tools
    pdx tools --apps github,gmail --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from clients.litellm_client import get_config
from utils.pipedream import (
    PipedreamClient,
    PipedreamError,
    _get_unique_channel,
    action_to_openai_tool,
)

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _ok(data: dict[str, Any]) -> None:
    print(json.dumps({"ok": True, **data}))


def _fail(message: str, exit_code: int = 3) -> None:
    print(json.dumps({"ok": False, "error": message}))
    sys.exit(exit_code)


def _client() -> PipedreamClient:
    return PipedreamClient()


def _handle_pdx_error(exc: PipedreamError) -> None:
    """Translate a PipedreamError into a _fail() call and exit."""
    messages = {
        400: f"Bad request (HTTP 400) — check unique_channel: {exc.message}",
        403: f"Forbidden (HTTP 403) — API key has no associated Ninja user: {exc.message}",
        502: f"Gateway error (HTTP 502) — Pipedream Connect gateway may be down: {exc.message}",
    }
    _fail(messages.get(exc.status_code, str(exc)), exit_code=3)


# ---------------------------------------------------------------------------
# Props arg parsing
# ---------------------------------------------------------------------------


def _collect_props(args: argparse.Namespace) -> Dict[str, Any]:
    """Combine --args <json> and repeated --arg k=v flags."""
    configured: Dict[str, Any] = {}
    if getattr(args, "args_json", None):
        try:
            parsed = json.loads(args.args_json)
        except json.JSONDecodeError as e:
            _fail(f"invalid JSON in --args: {e}", exit_code=1)
        if not isinstance(parsed, dict):
            _fail("--args must be a JSON object", exit_code=1)
        configured.update(parsed)
    for kv in getattr(args, "arg", None) or []:
        if "=" not in kv:
            _fail(f"--arg expects k=v, got: {kv}", exit_code=1)
        k, v = kv.split("=", 1)
        try:
            configured[k] = json.loads(v)
        except json.JSONDecodeError:
            configured[k] = v
    return configured


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> None:
    """Show resolved configuration."""
    try:
        unique_channel = _get_unique_channel()
    except ValueError as exc:
        _fail(str(exc), exit_code=2)

    cfg = get_config()
    _ok(
        {
            "unique_channel": unique_channel,
            "gateway": cfg.get("base_url", "(not configured)"),
        }
    )


def _cmd_health(args: argparse.Namespace) -> None:
    """Check the gateway health endpoint."""
    try:
        result = _client().check_health()
    except PipedreamError as exc:
        _fail(
            f"Pipedream gateway returned HTTP {exc.status_code}: {exc.message}",
            exit_code=3,
        )
    except Exception as exc:
        _fail(str(exc), exit_code=3)

    _ok(result)


def _cmd_connect_link(args: argparse.Namespace) -> None:
    """Get a short-lived OAuth connection link."""
    try:
        pdx = _client()
        link = pdx.get_connection_link(
            event_id=args.event_id or None,
        )
    except ValueError as exc:
        _fail(str(exc), exit_code=2)
    except PipedreamError as exc:
        _handle_pdx_error(exc)
    except Exception as exc:
        _fail(str(exc), exit_code=3)

    _ok({"link": link})


def _cmd_list(args: argparse.Namespace) -> None:
    """List connected apps for the onboarded user."""
    try:
        pdx = _client()
        accounts = pdx.list_accounts()
    except ValueError as exc:
        _fail(str(exc), exit_code=2)
    except PipedreamError as exc:
        _handle_pdx_error(exc)
    except Exception as exc:
        _fail(str(exc), exit_code=3)

    # Group by app_slug so the LLM sees one row per integration
    grouped: Dict[str, Dict[str, Any]] = {}
    for a in accounts:
        slug = a.get("app_slug") or "?"
        if slug not in grouped:
            grouped[slug] = {
                "app_slug": slug,
                "app_name": a.get("app_name") or slug,
                "account_ids": [],
                "healthy": True,
            }
        if a.get("id"):
            grouped[slug]["account_ids"].append(a["id"])
        if not a.get("healthy", True):
            grouped[slug]["healthy"] = False

    _ok({"count": len(grouped), "data": list(grouped.values())})


def _cmd_apps(args: argparse.Namespace) -> None:
    """Browse the Pipedream app catalog."""
    try:
        pdx = _client()
        apps = pdx.list_apps(q=args.q or None, limit=args.limit)
    except ValueError as exc:
        _fail(str(exc), exit_code=2)
    except PipedreamError as exc:
        _handle_pdx_error(exc)
    except Exception as exc:
        _fail(str(exc), exit_code=3)

    slim = [
        {
            "slug": a.get("name_slug"),
            "name": a.get("name"),
            "description": a.get("description"),
            "auth_type": a.get("auth_type"),
            "categories": a.get("categories", []),
        }
        for a in apps
    ]
    _ok({"count": len(slim), "data": slim})


def _cmd_actions(args: argparse.Namespace) -> None:
    """List available actions for an app."""
    try:
        actions = _client().list_actions(args.app_slug)
    except ValueError as exc:
        _fail(str(exc), exit_code=2)
    except PipedreamError as exc:
        _handle_pdx_error(exc)
    except Exception as exc:
        _fail(str(exc), exit_code=3)

    _ok({"app_slug": args.app_slug, "count": len(actions), "data": actions})


def _cmd_describe(args: argparse.Namespace) -> None:
    """Show full JSON schema for a specific action."""
    try:
        schema = _client().describe_action(args.action_key)
    except ValueError as exc:
        _fail(str(exc), exit_code=2)
    except PipedreamError as exc:
        _handle_pdx_error(exc)
    except Exception as exc:
        _fail(str(exc), exit_code=3)

    _ok(schema)


def _cmd_run(args: argparse.Namespace) -> None:
    """Execute a Pipedream action on behalf of the user."""
    configured = _collect_props(args)
    try:
        pdx = _client()
        result = pdx.run_action(
            args.action_key,
            props=configured,
            event_id=args.event_id or None,
        )
    except ValueError as exc:
        _fail(str(exc), exit_code=2)
    except PipedreamError as exc:
        _handle_pdx_error(exc)
    except Exception as exc:
        _fail(str(exc), exit_code=3)

    _ok(
        {
            "action_key": args.action_key,
            "configured_props": configured,
            "result": result,
        }
    )


def _cmd_http(args: argparse.Namespace) -> None:
    """Send a raw authenticated HTTP request through the Pipedream proxy."""
    # Body precedence: --json-file, then --json, then --data. --json-file reads
    # the body from a file so a large body never rides a single argv element,
    # which execve caps at MAX_ARG_STRLEN (128 KiB).
    json_body = None
    raw_body = None
    if getattr(args, "json_file", ""):
        try:
            with open(args.json_file, "r", encoding="utf-8") as fh:
                json_body = json.load(fh)
        except OSError as exc:
            _fail(f"cannot read --json-file {args.json_file!r}: {exc}", exit_code=1)
        except json.JSONDecodeError as exc:
            _fail(f"invalid JSON in --json-file {args.json_file!r}: {exc}", exit_code=1)
    elif args.json_body:
        try:
            json_body = json.loads(args.json_body)
        except json.JSONDecodeError as exc:
            _fail(f"invalid JSON in --json: {exc}", exit_code=1)
    elif args.data:
        raw_body = args.data

    # Parse --header K:V (repeatable)
    extra_headers: dict[str, str] = {}
    for h in args.header or []:
        if ":" not in h:
            _fail(f"invalid --header (expected 'K:V'): {h!r}", exit_code=1)
        k, v = h.split(":", 1)
        extra_headers[k.strip()] = v.strip()

    # Parse --query k=v (repeatable)
    query: dict[str, str] = {}
    for q in args.query or []:
        if "=" not in q:
            _fail(f"invalid --query (expected 'k=v'): {q!r}", exit_code=1)
        k, v = q.split("=", 1)
        query[k.strip()] = v.strip()

    try:
        pdx = _client()
        response = pdx.http_request(
            args.app_slug,
            args.method,
            args.url,
            json_body=json_body,
            raw_body=raw_body,
            extra_headers=extra_headers or None,
            query=query or None,
            event_id=args.event_id or None,
        )
    except ValueError as exc:
        _fail(str(exc), exit_code=2)
    except PipedreamError as exc:
        _handle_pdx_error(exc)
    except Exception as exc:
        _fail(str(exc), exit_code=3)

    upstream_status = response.get("status", 0)
    _ok(
        {
            "app_slug": args.app_slug,
            "request": {
                "method": args.method.upper(),
                "url": args.url,
                "headers": extra_headers,
                "query": query,
                "json": json_body,
            },
            "response": response,
            "upstream_ok": 200 <= upstream_status < 300,
        }
    )


def _cmd_tools(args: argparse.Namespace) -> None:
    """Emit OpenAI-style tool schema for every action of every connected app."""
    pdx = _client()

    # Determine which app slugs to include
    if args.apps:
        slugs = [s.strip() for s in args.apps.split(",") if s.strip()]
    else:
        try:
            accounts = pdx.list_accounts()
        except ValueError as exc:
            _fail(str(exc), exit_code=2)
        except PipedreamError as exc:
            _handle_pdx_error(exc)
        except Exception as exc:
            _fail(str(exc), exit_code=3)
        slugs = sorted({a.get("app_slug") or "" for a in accounts} - {""})

    tools: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for slug in slugs:
        try:
            actions = pdx.list_actions(slug)
        except Exception as e:
            errors.append({"app_slug": slug, "error": str(e)})
            continue
        for a in actions[: args.limit or 999]:
            try:
                schema = pdx.describe_action(a["key"])
                tools.append(action_to_openai_tool(schema))
            except Exception as e:
                errors.append({"action_key": a["key"], "error": str(e)})

    _ok(
        {
            "apps": slugs,
            "count": len(tools),
            "tools": tools,
            "errors": errors,
        }
    )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdx",
        description="Pipedream Connect CLI — routes through LiteLLM to the Pipedream Connect integrations gateway.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # -- status --------------------------------------------------------------
    sub.add_parser(
        "status",
        help="Show resolved unique_channel and gateway URL.",
    )

    # -- health --------------------------------------------------------------
    sub.add_parser(
        "health",
        help="GET /ninja/integrations-gateway/health — no auth required.",
    )

    # -- connect-link --------------------------------------------------------
    link_p = sub.add_parser(
        "connect-link",
        help="Get a short-lived OAuth connection link for the user (expires 30 min).",
    )
    link_p.add_argument(
        "--event-id",
        dest="event_id",
        default="",
        help="x-ninja-event-id traceability header.",
    )

    # -- list ----------------------------------------------------------------
    sub.add_parser(
        "list",
        help="List connected integrations for the user.",
    )

    # -- apps ----------------------------------------------------------------
    apps_p = sub.add_parser(
        "apps",
        help="Browse the Pipedream app catalog.",
    )
    apps_p.add_argument("--q", "-q", default="", help="Search query.")
    apps_p.add_argument(
        "--limit", "-n", type=int, default=50, help="Max results (default 50)."
    )

    # -- actions -------------------------------------------------------------
    actions_p = sub.add_parser(
        "actions",
        help="List available actions for an app.",
    )
    actions_p.add_argument("app_slug", help="App slug (e.g. 'github', 'gmail').")

    # -- describe ------------------------------------------------------------
    describe_p = sub.add_parser(
        "describe",
        help="Show full JSON schema for a specific action.",
    )
    describe_p.add_argument(
        "action_key", help="Action key (e.g. 'github-create-issue')."
    )

    # -- run -----------------------------------------------------------------
    run_p = sub.add_parser(
        "run",
        help="Execute a Pipedream action on behalf of the user.",
    )
    run_p.add_argument("action_key", help="Action key (e.g. 'github-create-issue').")
    run_p.add_argument(
        "--args",
        dest="args_json",
        default="",
        help='JSON object of action props (e.g. \'{"title": "Bug fix"}\').',
    )
    run_p.add_argument(
        "--arg",
        action="append",
        default=[],
        help="Individual prop in k=v form (repeatable). Values are JSON-parsed if valid.",
    )
    run_p.add_argument(
        "--event-id",
        dest="event_id",
        default="",
        help="x-ninja-event-id traceability header.",
    )

    # -- http ----------------------------------------------------------------
    http_p = sub.add_parser(
        "http",
        help="Send a raw authenticated HTTP request through the Pipedream proxy (no LLM).",
    )
    http_p.add_argument("app_slug", help="App slug (e.g. 'github', 'gmail').")
    http_p.add_argument("method", help="HTTP method (GET, POST, PUT, PATCH, DELETE).")
    http_p.add_argument(
        "url", help="Upstream URL (e.g. 'https://api.github.com/user')."
    )
    http_p.add_argument(
        "--json",
        dest="json_body",
        default="",
        help="JSON body as a string.",
    )
    http_p.add_argument(
        "--json-file",
        dest="json_file",
        default="",
        help=(
            "Path to a file whose contents are the JSON body; streams a large "
            "body past the single-argv per-arg limit. Takes precedence over "
            "--json and --data."
        ),
    )
    http_p.add_argument(
        "--data",
        default="",
        help="Raw body string.",
    )
    http_p.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra header to forward upstream in 'K:V' form (repeatable).",
    )
    http_p.add_argument(
        "--query",
        action="append",
        default=[],
        help="Query string parameter in 'k=v' form (repeatable).",
    )
    http_p.add_argument(
        "--event-id",
        dest="event_id",
        default="",
        help="x-ninja-event-id traceability header.",
    )

    # -- tools ---------------------------------------------------------------
    tools_p = sub.add_parser(
        "tools",
        help="Emit OpenAI-style tool schema for all connected apps.",
    )
    tools_p.add_argument(
        "--apps",
        default="",
        help="Comma-separated app slugs to include (default: all connected).",
    )
    tools_p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max actions per app (default: 0 = no limit).",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "status":
        _cmd_status(args)
    elif args.command == "health":
        _cmd_health(args)
    elif args.command == "connect-link":
        _cmd_connect_link(args)
    elif args.command == "list":
        _cmd_list(args)
    elif args.command == "apps":
        _cmd_apps(args)
    elif args.command == "actions":
        _cmd_actions(args)
    elif args.command == "describe":
        _cmd_describe(args)
    elif args.command == "run":
        _cmd_run(args)
    elif args.command == "http":
        _cmd_http(args)
    elif args.command == "tools":
        _cmd_tools(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
