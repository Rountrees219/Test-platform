"""
MCP (Model Context Protocol) Client Utility
=============================================

Connects to the LiteLLM gateway's MCP endpoint to discover and call
external tools (travel APIs, booking services, etc.).

The LiteLLM gateway has quirks that this module works around:
1. Returns 400 on ``notifications/initialized``
2. Session management is unreliable — sessions can fail intermittently

This module patches the MCP SDK and adds connection-level retry logic.

Quick-start
-----------
    from utils.mcp import MCPClient

    async with MCPClient() as client:
        tools = await client.list_tools()
        result = await client.call_tool("booking_com-Search_Hotels", {
            "dest_id": "-2601889",
            "search_type": "CITY",
        })

CLI usage (from ninja/):
    python -m utils.mcp list                  # list all tools
    python -m utils.mcp search booking        # search tools by name
    python -m utils.mcp call <tool> '{...}'   # call a tool
    python -m utils.mcp groups                # show tool groups
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

import httpx
from clients.litellm_client import get_config, get_headers
from mcp import ClientSession
from mcp.client.streamable_http import StreamableHTTPTransport, streamable_http_client
from mcp.types import CallToolResult, InitializedNotification, JSONRPCNotification, Tool

__all__ = [
    "MCPClient",
    "list_tools",
    "call_tool",
    "search_tools",
    "group_tools",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patch 1: Swallow HTTP errors on notifications
# ---------------------------------------------------------------------------
_original_handle_post = StreamableHTTPTransport._handle_post_request


async def _patched_handle_post(self: StreamableHTTPTransport, ctx: Any) -> None:
    """Wrap the original handler to swallow HTTP errors on notifications."""
    message = ctx.session_message.message
    is_notification = isinstance(message.root, JSONRPCNotification)
    if is_notification:
        try:
            await _original_handle_post(self, ctx)
        except httpx.HTTPStatusError as exc:
            logger.debug(
                "Swallowed HTTP %s on notification '%s'",
                exc.response.status_code,
                getattr(message.root, "method", "?"),
            )
        except Exception as exc:
            logger.debug("Swallowed error on notification: %s", exc)
    else:
        await _original_handle_post(self, ctx)


StreamableHTTPTransport._handle_post_request = _patched_handle_post  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Patch 2: Always include Mcp-Protocol-Version in headers
# ---------------------------------------------------------------------------
_original_prepare_headers = StreamableHTTPTransport._prepare_headers


def _patched_prepare_headers(self: StreamableHTTPTransport) -> dict[str, str]:
    headers = _original_prepare_headers(self)
    if "Mcp-Protocol-Version" not in headers and not self.protocol_version:
        headers["Mcp-Protocol-Version"] = "2024-11-05"
    return headers


StreamableHTTPTransport._prepare_headers = _patched_prepare_headers  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Patch 3: Skip the InitializedNotification
# ---------------------------------------------------------------------------
_original_send_notification = ClientSession.send_notification


async def _patched_send_notification(
    self: ClientSession, notification: Any, *args: Any, **kwargs: Any
) -> None:
    inner = getattr(notification, "root", notification)
    if isinstance(inner, InitializedNotification):
        logger.debug("Dropped InitializedNotification (gateway workaround)")
        return
    return await _original_send_notification(self, notification, *args, **kwargs)


ClientSession.send_notification = _patched_send_notification  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# MCPClient — with full connection-level retry
# ---------------------------------------------------------------------------


class MCPClient:
    """
    Async context-manager MCP client for the LiteLLM gateway.

    Includes connection-level retry: if the session breaks, it reconnects
    from scratch (up to ``max_retries`` times).

    Usage::

        async with MCPClient() as client:
            tools = await client.list_tools()
            result = await client.call_tool("tool_name", {"arg": "val"})
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        read_timeout: float = 300.0,
        max_retries: int = 3,
    ) -> None:
        cfg = get_config()
        self._url = url or (cfg["base_url"].rstrip("/") + "/mcp/")
        self._api_key = api_key or cfg["api_key"]
        self._timeout = timeout
        self._read_timeout = read_timeout
        self._max_retries = max_retries
        self._tools_cache: list[Tool] | None = None

        # Connection state
        self._http_client: httpx.AsyncClient | None = None
        self._ctx: Any = None
        self._session: ClientSession | None = None
        self._get_session_id: Any = None

    # -- internal connect/disconnect -----------------------------------------

    async def _connect(self) -> None:
        """Establish a fresh MCP session."""
        await self._disconnect()  # clean up any prior state

        headers = {
            k: v for k, v in get_headers().items() if k.lower() != "content-type"
        }
        headers["Authorization"] = f"Bearer {self._api_key}"
        tracking = {k: v for k, v in headers.items() if k.lower().startswith("x-ninja")}
        logger.info("MCP connect: tracking headers %s", tracking or "(none)")
        self._http_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self._timeout, read=self._read_timeout),
        )
        self._ctx = streamable_http_client(
            self._url,
            http_client=self._http_client,
            terminate_on_close=False,
        )
        read_stream, write_stream, self._get_session_id = await self._ctx.__aenter__()
        self._session = ClientSession(read_stream, write_stream)
        await self._session.__aenter__()
        await self._session.initialize()
        logger.info("MCP session %s established", self.session_id)

    async def _disconnect(self) -> None:
        """Tear down the current session silently."""
        # Close in reverse order; swallow ALL exceptions including BaseException
        session, ctx, http = self._session, self._ctx, self._http_client
        self._session = None
        self._ctx = None
        self._http_client = None
        self._get_session_id = None

        for resource in (session, ctx):
            if resource is not None:
                try:
                    await resource.__aexit__(None, None, None)
                except BaseException:
                    pass
        if http is not None:
            try:
                await http.aclose()
            except BaseException:
                pass

    async def _with_retry(self, operation: str, func, *args, **kwargs):
        """Execute func with connection-level retry."""
        last_err = None
        for attempt in range(1, self._max_retries + 1):
            try:
                if not self._session:
                    await self._connect()
                return await func(*args, **kwargs)
            except BaseException as e:
                # Catch BaseException to handle CancelledError from anyio
                last_err = e
                logger.debug(
                    "%s attempt %d/%d failed: %s — reconnecting",
                    operation,
                    attempt,
                    self._max_retries,
                    e,
                )
                await self._disconnect()
                self._tools_cache = None
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(
            f"{operation} failed after {self._max_retries} attempts: {last_err}"
        )

    # -- context manager -----------------------------------------------------

    async def __aenter__(self) -> "MCPClient":
        await self._connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self._disconnect()
        except BaseException:
            pass

    # -- properties ----------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._get_session_id() if self._get_session_id else None

    @property
    def connected(self) -> bool:
        return self._session is not None

    # -- core operations (with retry) ----------------------------------------

    async def list_tools(self, *, use_cache: bool = True) -> list[Tool]:
        """List all available MCP tools (cached after first call)."""
        if use_cache and self._tools_cache is not None:
            return self._tools_cache

        async def _do():
            result = await self._session.list_tools()
            self._tools_cache = result.tools
            return self._tools_cache

        return await self._with_retry("list_tools", _do)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Call a tool by name with optional arguments."""

        async def _do():
            return await self._session.call_tool(name=name, arguments=arguments or {})

        return await self._with_retry(f"call_tool({name})", _do)

    # -- convenience helpers -------------------------------------------------

    async def search_tools(self, query: str) -> list[Tool]:
        """Search tools by name or description (case-insensitive)."""
        tools = await self.list_tools()
        q = query.lower()
        return [
            t
            for t in tools
            if q in t.name.lower() or q in (t.description or "").lower()
        ]

    async def group_tools(self) -> dict[str, list[Tool]]:
        """Group tools by service prefix (text before first hyphen)."""
        tools = await self.list_tools()
        groups: dict[str, list[Tool]] = {}
        for t in tools:
            prefix = t.name.split("-")[0] if "-" in t.name else t.name.split("_")[0]
            groups.setdefault(prefix, []).append(t)
        return groups

    async def tool_names(self) -> list[str]:
        """Return a sorted list of all tool names."""
        tools = await self.list_tools()
        return sorted(t.name for t in tools)

    def format_tool(self, tool: Tool) -> str:
        """Format a single tool for display."""
        schema = (
            getattr(tool, "input_schema", None)
            or getattr(tool, "inputSchema", None)
            or {}
        )
        props = schema.get("properties", {})
        required = schema.get("required", [])
        visible = {k: v for k, v in props.items() if not v.get("hidden", False)}
        lines = [f"  Name: {tool.name}"]
        if tool.description and tool.description != "-":
            lines.append(f"  Description: {tool.description}")
        if visible:
            params = []
            for k, v in visible.items():
                req = " (required)" if k in required else ""
                desc = v.get("description", "")[:60].replace("\n", " ")
                default = v.get("default", "")
                param_str = f"{k}{req}"
                if default:
                    param_str += f" [default: {default}]"
                if desc:
                    param_str += f" — {desc}"
                params.append(param_str)
            lines.append("  Parameters:")
            for p in params:
                lines.append(f"    • {p}")
        return "\n".join(lines)

    async def format_result(self, result: CallToolResult) -> str:
        """Format a CallToolResult for display."""
        parts = []
        if result.isError:
            parts.append("❌ ERROR")
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif hasattr(item, "model_dump"):
                parts.append(json.dumps(item.model_dump(), indent=2))
            else:
                parts.append(str(item))
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Standalone helper functions
# ---------------------------------------------------------------------------


async def list_tools() -> list[Tool]:
    """One-shot: connect, list tools, disconnect."""
    async with MCPClient() as client:
        return await client.list_tools()


async def call_tool(
    name: str, arguments: dict[str, Any] | None = None
) -> CallToolResult:
    """One-shot: connect, call a tool, disconnect."""
    async with MCPClient() as client:
        return await client.call_tool(name, arguments)


async def search_tools(query: str) -> list[Tool]:
    """One-shot: connect, search tools, disconnect."""
    async with MCPClient() as client:
        return await client.search_tools(query)


async def group_tools() -> dict[str, list[Tool]]:
    """One-shot: connect, group tools, disconnect."""
    async with MCPClient() as client:
        return await client.group_tools()


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------


def _print_tool_brief(tool: Tool) -> None:
    """Print a one-line tool summary."""
    desc = (tool.description or "-")[:70].replace("\n", " ")
    schema = (
        getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    )
    props = schema.get("properties", {})
    visible = [k for k, v in props.items() if not v.get("hidden", False)]
    param_str = (
        f" ({', '.join(visible[:4])}{'...' if len(visible) > 4 else ''})"
        if visible
        else ""
    )
    print(f"  • {tool.name}{param_str}")
    if desc != "-":
        print(f"    {desc}")


async def _cli_list() -> None:
    async with MCPClient() as client:
        tools = await client.list_tools()
        print(f"\n📋 Available MCP Tools ({len(tools)} total)\n")
        for tool in tools:
            _print_tool_brief(tool)
        print()


async def _cli_search(query: str) -> None:
    async with MCPClient() as client:
        tools = await client.search_tools(query)
        print(f"\n🔍 Tools matching '{query}' ({len(tools)} results)\n")
        for tool in tools:
            _print_tool_brief(tool)
        print()


async def _cli_groups() -> None:
    async with MCPClient() as client:
        groups = await client.group_tools()
        total = sum(len(v) for v in groups.values())
        print(f"\n📦 Tool Groups ({len(groups)} services, {total} tools)\n")
        for prefix, tools in sorted(groups.items()):
            print(f"  {prefix} ({len(tools)} tools):")
            for t in tools[:3]:
                print(f"    • {t.name}")
            if len(tools) > 3:
                print(f"    ... +{len(tools) - 3} more")
            print()


async def _cli_call(tool_name: str, args_json: str) -> None:
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    async with MCPClient() as client:
        print(f"🔧 Calling {tool_name}...")
        result = await client.call_tool(tool_name, args)
        output = await client.format_result(result)
        print(output)


async def _cli_info(tool_name: str) -> None:
    async with MCPClient() as client:
        tools = await client.list_tools()
        matches = [t for t in tools if t.name == tool_name]
        if not matches:
            matches = [t for t in tools if tool_name.lower() in t.name.lower()]
        if not matches:
            print(f"❌ Tool '{tool_name}' not found", file=sys.stderr)
            sys.exit(1)
        for t in matches:
            print(f"\n{client.format_tool(t)}")
        print()


def main() -> None:
    """CLI entry point."""
    usage = """Usage: python -m utils.mcp <command> [args]

Commands:
  list                     List all available tools
  search <query>           Search tools by name/description
  groups                   Show tools grouped by service
  info <tool_name>         Show detailed tool info
  call <tool_name> [json]  Call a tool with arguments
"""
    if len(sys.argv) < 2:
        print(usage)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        asyncio.run(_cli_list())
    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: python -m utils.mcp search <query>")
            sys.exit(1)
        asyncio.run(_cli_search(sys.argv[2]))
    elif cmd == "groups":
        asyncio.run(_cli_groups())
    elif cmd == "info":
        if len(sys.argv) < 3:
            print("Usage: python -m utils.mcp info <tool_name>")
            sys.exit(1)
        asyncio.run(_cli_info(sys.argv[2]))
    elif cmd == "call":
        if len(sys.argv) < 3:
            print(
                "Usage: python -m utils.mcp call <tool_name> ['{&quot;arg&quot;: &quot;val&quot;}']"
            )
            sys.exit(1)
        tool_name = sys.argv[2]
        args_json = sys.argv[3] if len(sys.argv) > 3 else "{}"
        asyncio.run(_cli_call(tool_name, args_json))
    else:
        print(f"Unknown command: {cmd}")
        print(usage)
        sys.exit(1)


if __name__ == "__main__":
    main()
