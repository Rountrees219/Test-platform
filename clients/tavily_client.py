#!/usr/bin/env python3
"""
Tavily MCP Client - Python wrapper for all Tavily MCP tools.
Uses the LiteLLM gateway's /mcp-rest/tools/call endpoint.

Credentials are read from settings.json (same file used by claude-wrapper.sh)
via the shared clients/litellm_client module.

Usage:
    from clients.tavily_client import Tavily

    tavily = Tavily()

    # Search the web
    results = tavily.search("latest AI news", max_results=5)

    # Extract content from URLs
    pages = tavily.extract(["https://example.com"])

    # Crawl a website
    site = tavily.crawl("https://docs.example.com", max_depth=2)

    # Map a website's URL structure
    urls = tavily.map("https://docs.example.com", limit=20)

    # Comprehensive research (polls until complete)
    report = tavily.research("AI trends in 2026")
"""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Configuration — reads from settings.json via litellm_client
# ---------------------------------------------------------------------------

# Add parent dir to path so we can import utils when run standalone
_this_dir = Path(__file__).resolve().parent
if str(_this_dir) not in sys.path:
    sys.path.insert(0, str(_this_dir))

from clients.litellm_client import get_config


@dataclass
class TavilyConfig:
    """Configuration auto-loaded from settings.json (same as claude-wrapper).

    You can override any field explicitly:
        TavilyConfig(base_url="...", api_key="...")
    Otherwise everything is read from settings.json automatically.
    """

    base_url: str = ""
    api_key: str = ""
    server_id: str = ""
    tool_prefix: str = ""

    def __post_init__(self):
        # Load from settings.json via litellm_client if not provided
        if not self.base_url or not self.api_key:
            cfg = get_config()
            if not self.base_url:
                self.base_url = cfg.get("base_url", "")
            if not self.api_key:
                self.api_key = cfg.get("api_key", "")

        if not self.base_url:
            raise ValueError(
                "Base URL not found in settings.json or TavilyConfig(base_url=...)"
            )
        self.base_url = self.base_url.rstrip("/")

        if not self.api_key:
            raise ValueError(
                "API key not found in settings.json or TavilyConfig(api_key=...)"
            )

        if not self.server_id:
            self._discover_server_id()

    def _discover_server_id(self):
        """Auto-discover server_id and tool_prefix from the gateway."""
        try:
            r = requests.get(
                f"{self.base_url}/v1/mcp/server",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if r.status_code == 200:
                servers = r.json()
                for s in servers:
                    if "tavily" in s.get("server_name", "").lower():
                        self.server_id = s["server_id"]
                        self.tool_prefix = (
                            s.get("alias", "") + "-" if s.get("alias") else ""
                        )
                        return
            raise ValueError("Could not auto-discover Tavily server_id from gateway")
        except requests.RequestException as e:
            raise ValueError(f"Failed to connect to {self.base_url}: {e}")
        except ValueError:
            raise
        except Exception:
            raise ValueError("server_id required via TavilyConfig(server_id=...)")


class _MCPSession:
    """MCP REST client using /mcp-rest/tools/call endpoint."""

    def __init__(self, cfg: TavilyConfig):
        self.cfg = cfg
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        }

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        prefixed_name = f"{self.cfg.tool_prefix}{name}"
        r = requests.post(
            f"{self.cfg.base_url}/mcp-rest/tools/call",
            headers=self._headers,
            json={
                "name": prefixed_name,
                "arguments": arguments,
                "server_id": self.cfg.server_id,
            },
        )

        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:500]}")

        data = r.json()

        if isinstance(data, list):
            content = data
        else:
            if data.get("isError"):
                content = data.get("content", [])
                err_msg = (
                    content[0].get("text", "Unknown error")
                    if content
                    else "Unknown error"
                )
                raise Exception(f"Tool error: {err_msg}")
            content = data.get("content", [])

        for item in content:
            if item.get("type") == "text":
                text = item["text"]
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return data

    def list_tools(self) -> List[Dict]:
        r = requests.get(f"{self.cfg.base_url}/v1/mcp/tools", headers=self._headers)
        if r.status_code != 200:
            return []
        all_tools = r.json().get("tools", [])
        prefix = self.cfg.tool_prefix
        return [t for t in all_tools if t.get("name", "").startswith(prefix)]


class Tavily:
    """Unified interface to all Tavily MCP tools via the LiteLLM gateway.

    Credentials are automatically loaded from settings.json (the same file
    used by claude-wrapper.sh). No manual configuration needed.
    """

    def __init__(self, config: TavilyConfig = None):
        self.config = config or TavilyConfig()
        self._mcp = _MCPSession(self.config)

    def _call(self, tool: str, args: Dict[str, Any]) -> Any:
        return self._mcp.call_tool(tool, args)

    def list_tools(self) -> List[Dict]:
        """List all available Tavily MCP tools."""
        return self._mcp.list_tools()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        search_depth: str = "basic",
        topic: str = "general",
        time_range: Optional[str] = None,
        include_images: bool = False,
        include_image_descriptions: bool = False,
        include_raw_content: bool = False,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        country: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        include_favicon: bool = False,
    ) -> Any:
        """Search the web using Tavily.

        Args:
            query: Search query string.
            max_results: Max results to return (default 10).
            search_depth: "basic", "advanced", "fast", or "ultra-fast".
            topic: "general", "news", or "finance".
            time_range: "day", "week", "month", or "year".
            include_images: Include image results.
            include_image_descriptions: Include image descriptions.
            include_raw_content: Include cleaned HTML content per result.
            include_domains: Whitelist specific domains.
            exclude_domains: Blacklist specific domains.
            country: Boost results from a specific country.
            start_date: Filter after date (YYYY-MM-DD).
            end_date: Filter before date (YYYY-MM-DD).
            include_favicon: Include favicon URL per result.

        Returns:
            Dict with keys: query, answer, images, results, response_time.
        """
        args = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "topic": topic,
            "include_images": include_images,
            "include_image_descriptions": include_image_descriptions,
            "include_raw_content": include_raw_content,
            "include_favicon": include_favicon,
        }
        if time_range:
            args["time_range"] = time_range
        if include_domains:
            args["include_domains"] = include_domains
        if exclude_domains:
            args["exclude_domains"] = exclude_domains
        if country:
            args["country"] = country
        if start_date:
            args["start_date"] = start_date
        if end_date:
            args["end_date"] = end_date
        return self._call("tavily_search", args)

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------

    def extract(
        self,
        urls: List[str],
        *,
        extract_depth: str = "basic",
        include_images: bool = False,
        format: str = "markdown",
        query: Optional[str] = None,
        include_favicon: bool = False,
    ) -> Any:
        """Extract content from URLs.

        Args:
            urls: List of URLs to extract content from.
            extract_depth: "basic" or "advanced" (for protected sites, tables).
            include_images: Include images from pages.
            format: "markdown" or "text".
            query: Optional query to rerank content by relevance.
            include_favicon: Include favicon URLs.

        Returns:
            Dict with keys: results [{url, raw_content}], failed_results, response_time.
        """
        args = {
            "urls": urls,
            "extract_depth": extract_depth,
            "include_images": include_images,
            "format": format,
            "include_favicon": include_favicon,
        }
        if query:
            args["query"] = query
        return self._call("tavily_extract", args)

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def crawl(
        self,
        url: str,
        *,
        max_depth: int = 1,
        max_breadth: int = 20,
        limit: int = 50,
        instructions: Optional[str] = None,
        select_paths: Optional[List[str]] = None,
        select_domains: Optional[List[str]] = None,
        allow_external: bool = True,
        extract_depth: str = "basic",
        format: str = "markdown",
        include_favicon: bool = False,
    ) -> Any:
        """Crawl a website starting from a URL.

        Args:
            url: Root URL to begin the crawl.
            max_depth: How deep to crawl (1-5).
            max_breadth: Max links per page (1-500).
            limit: Total pages to process.
            instructions: Natural language crawl instructions.
            select_paths: Regex patterns to include paths.
            select_domains: Regex patterns to include domains.
            allow_external: Include external links.
            extract_depth: "basic" or "advanced".
            format: "markdown" or "text".
            include_favicon: Include favicon URLs.

        Returns:
            Dict with keys: base_url, results [{url, raw_content}], response_time.
        """
        args = {
            "url": url,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "limit": limit,
            "allow_external": allow_external,
            "extract_depth": extract_depth,
            "format": format,
            "include_favicon": include_favicon,
        }
        if instructions:
            args["instructions"] = instructions
        if select_paths:
            args["select_paths"] = select_paths
        if select_domains:
            args["select_domains"] = select_domains
        return self._call("tavily_crawl", args)

    # ------------------------------------------------------------------
    # Map
    # ------------------------------------------------------------------

    def map(
        self,
        url: str,
        *,
        max_depth: int = 1,
        max_breadth: int = 20,
        limit: int = 50,
        instructions: Optional[str] = None,
        select_paths: Optional[List[str]] = None,
        select_domains: Optional[List[str]] = None,
        allow_external: bool = True,
    ) -> Any:
        """Map a website's URL structure.

        Args:
            url: Root URL to begin mapping.
            max_depth: How deep to map (1-5).
            max_breadth: Max links per page (1-500).
            limit: Total pages to process.
            instructions: Natural language instructions.
            select_paths: Regex patterns to include paths.
            select_domains: Regex patterns to include domains.
            allow_external: Include external links.

        Returns:
            Dict with keys: base_url, results (list of URLs), response_time.
        """
        args = {
            "url": url,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "limit": limit,
            "allow_external": allow_external,
        }
        if instructions:
            args["instructions"] = instructions
        if select_paths:
            args["select_paths"] = select_paths
        if select_domains:
            args["select_domains"] = select_domains
        return self._call("tavily_map", args)

    # ------------------------------------------------------------------
    # Research
    # ------------------------------------------------------------------

    def research(self, input: str, *, model: str = "auto") -> Any:
        """Perform comprehensive multi-source research.

        Note: The research endpoint is async. This method returns the
        initial response which may have status "pending". Use
        research_and_wait() for automatic polling.

        Args:
            input: Description of the research task.
            model: "mini" (narrow), "pro" (broad), or "auto".

        Returns:
            Dict with research task status and (if complete) content + sources.
        """
        return self._call("tavily_research", {"input": input, "model": model})


if __name__ == "__main__":
    pp = lambda x: print(
        json.dumps(x, indent=2)[:500] if isinstance(x, (dict, list)) else str(x)[:500]
    )

    print("=== Tavily MCP Client Tests ===\n")

    try:
        tavily = Tavily()
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Config: base_url={tavily.config.base_url}")
    print(f"Server ID: {tavily.config.server_id}")
    print(f"Tool prefix: {tavily.config.tool_prefix}\n")

    print("1. List tools...")
    tools = tavily.list_tools()
    print(f"   {len(tools)} Tavily tools available")
    for t in tools:
        tname = t.get("name", "?")
        print(f"   - {tname}")
    print()

    print("2. Search: 'latest AI news'...")
    try:
        result = tavily.search("latest AI news", max_results=3)
        query = result.get("query", "?")
        rtime = result.get("response_time", "?")
        print(f"   Query: {query}")
        print(f"   Response time: {rtime}s")
        for r in result.get("results", [])[:3]:
            title = r.get("title", "No title")[:60]
            url = r.get("url", "")[:60]
            print(f"   - {title}: {url}")
    except Exception as e:
        print(f"   Error: {e}")

    print("\n3. Extract: 'https://www.ninjatech.ai/'...")
    try:
        result = tavily.extract(["https://www.ninjatech.ai/"])
        results = result.get("results", [])
        print(f"   Extracted {len(results)} pages")
        for r in results:
            content = r.get("raw_content", "")
            url = r.get("url", "?")
            print(f"   - {url} ({len(content)} chars)")
    except Exception as e:
        print(f"   Error: {e}")

    print("\n4. Crawl: 'https://docs.tavily.com' (depth=1, limit=3)...")
    try:
        result = tavily.crawl("https://docs.tavily.com", max_depth=1, limit=3)
        pages = result.get("results", [])
        base = result.get("base_url", "?")
        print(f"   Crawled {len(pages)} pages from {base}")
        for r in pages:
            content = r.get("raw_content", "")
            url = r.get("url", "?")
            print(f"   - {url} ({len(content)} chars)")
    except Exception as e:
        print(f"   Error: {e}")

    print("\n5. Map: 'https://docs.tavily.com' (limit=5)...")
    try:
        result = tavily.map("https://docs.tavily.com", limit=5)
        urls = result.get("results", [])
        print(f"   Mapped {len(urls)} URLs")
        for u in urls[:5]:
            if isinstance(u, str):
                print(f"   - {u}")
            elif isinstance(u, dict):
                print(f"   - {u.get('url', u)}")
    except Exception as e:
        print(f"   Error: {e}")

    print("\n=== Tests complete ===")
