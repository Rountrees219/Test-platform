#!/usr/bin/env python3
"""
Claude Monitor - Parses Claude session JSONL files and serves stats via HTTP.

Serves on port 9010 and provides:
- /api/stats - Aggregate token usage, cost, messages, tool uses
- /api/sessions - List of sessions
- /api/tools/summary - Tool usage breakdown
- /api/usage/timeline - Token usage over time
- /api/prompts - Recent user prompts
"""

import glob
import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from utils.cost import compute_cost

# Configuration
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CACHE_TTL = 10  # seconds
PORT = 9010


class SessionData:
    """Parsed data from a single JSONL session file."""

    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_write_5m_tokens = 0
        self.cache_write_1h_tokens = 0
        self.cache_read_tokens = 0
        self.tool_uses = {}  # name -> count
        self.messages = 0
        self.prompts = []  # [{timestamp, content}]
        self.timeline = (
            []
        )  # [{timestamp, input_tokens, output_tokens, cache_read_tokens}]
        self.session_id = ""
        self.model = ""
        self.start_time = None
        self.last_time = None


def parse_jsonl_file(filepath: str) -> SessionData:
    """Parse a Claude JSONL session file and extract stats."""
    data = SessionData()
    data.session_id = Path(filepath).stem

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")
                timestamp = entry.get("timestamp", "")

                # Track time range
                if timestamp:
                    if data.start_time is None or timestamp < data.start_time:
                        data.start_time = timestamp
                    if data.last_time is None or timestamp > data.last_time:
                        data.last_time = timestamp

                msg = entry.get("message", {})

                # Count messages
                if entry_type in ("user", "assistant"):
                    data.messages += 1

                # Extract usage from assistant messages
                usage = msg.get("usage", {})
                if usage:
                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cr = usage.get("cache_read_input_tokens", 0)

                    # Break down cache writes into 5m and 1h tiers
                    cache_creation = usage.get("cache_creation", {})
                    cw_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
                    cw_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
                    # Fallback: if no breakdown, attribute all to 5m
                    if not cw_5m and not cw_1h:
                        cw_5m = usage.get("cache_creation_input_tokens", 0)

                    data.input_tokens += inp
                    data.output_tokens += out
                    data.cache_write_5m_tokens += cw_5m
                    data.cache_write_1h_tokens += cw_1h
                    data.cache_read_tokens += cr

                    # Timeline entry
                    if timestamp and (inp or out or cr):
                        data.timeline.append(
                            {
                                "timestamp": timestamp,
                                "input_tokens": inp,
                                "output_tokens": out,
                                "cache_read_tokens": cr,
                                "cache_write_5m_tokens": cw_5m,
                                "cache_write_1h_tokens": cw_1h,
                            }
                        )

                # Extract model (inside message object)
                if msg.get("model") and not data.model:
                    data.model = msg["model"]

                # Extract tool uses from assistant messages
                content = msg.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            name = item.get("name", "unknown")
                            data.tool_uses[name] = data.tool_uses.get(name, 0) + 1

                # Extract user prompts
                if entry_type == "user":
                    user_content = msg.get("content", "")
                    if isinstance(user_content, str) and user_content.strip():
                        data.prompts.append(
                            {
                                "timestamp": timestamp,
                                "content": user_content[:2000],
                                "response": "",
                                "uuid": entry.get("uuid", ""),
                            }
                        )
                    elif isinstance(user_content, list):
                        # Tool results - skip these as prompts
                        pass

                # Pair assistant text responses with the last prompt
                if entry_type == "assistant" and data.prompts:
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text_val = item.get("text", "").strip()
                                if text_val and not data.prompts[-1].get("response"):
                                    data.prompts[-1]["response"] = text_val[:3000]

    except Exception as e:
        print(f"Error parsing {filepath}: {e}")

    return data


class StatsCache:
    """Caches aggregated stats with TTL."""

    def __init__(self):
        self._cache = {}
        self._last_update = 0
        self._lock = threading.Lock()

    def get_stats(self):
        with self._lock:
            now = time.time()
            if now - self._last_update < CACHE_TTL and self._cache:
                return self._cache
            self._cache = self._compute_stats()
            self._last_update = now
            return self._cache

    def _find_jsonl_files(self):
        """Find all JSONL session files."""
        files = []
        if CLAUDE_PROJECTS_DIR.exists():
            for jsonl in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
                files.append(str(jsonl))
        return files

    def _compute_stats(self):
        """Parse all session files and compute aggregate stats."""
        files = self._find_jsonl_files()
        sessions = []
        total = SessionData()
        all_tool_uses = {}
        all_prompts = []
        all_timeline = []

        total_cost = 0.0
        for f in files:
            sd = parse_jsonl_file(f)

            session_cost = compute_cost(
                sd.model,
                sd.input_tokens,
                sd.output_tokens,
                sd.cache_write_5m_tokens,
                sd.cache_write_1h_tokens,
                sd.cache_read_tokens,
            )
            total_cost += session_cost

            sessions.append(
                {
                    "session_id": sd.session_id,
                    "messages": sd.messages,
                    "input_tokens": sd.input_tokens,
                    "output_tokens": sd.output_tokens,
                    "cache_write_5m_tokens": sd.cache_write_5m_tokens,
                    "cache_write_1h_tokens": sd.cache_write_1h_tokens,
                    "cache_read_tokens": sd.cache_read_tokens,
                    "tool_uses": sum(sd.tool_uses.values()),
                    "start_time": sd.start_time,
                    "last_time": sd.last_time,
                    "model": sd.model,
                    "cost": round(session_cost, 4),
                }
            )

            total.input_tokens += sd.input_tokens
            total.output_tokens += sd.output_tokens
            total.cache_write_5m_tokens += sd.cache_write_5m_tokens
            total.cache_write_1h_tokens += sd.cache_write_1h_tokens
            total.messages += sd.messages

            for name, count in sd.tool_uses.items():
                all_tool_uses[name] = all_tool_uses.get(name, 0) + count

            all_prompts.extend(sd.prompts)
            all_timeline.extend(sd.timeline)

        cost = total_cost

        total_tool_uses = sum(all_tool_uses.values())

        # Sort tool uses by count
        tool_summary = sorted(
            [{"name": k, "count": v} for k, v in all_tool_uses.items()],
            key=lambda x: x["count"],
            reverse=True,
        )

        # Sort prompts by timestamp (newest first)
        all_prompts.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        # Sort timeline by timestamp
        all_timeline.sort(key=lambda x: x.get("timestamp", ""))

        return {
            "stats": {
                "total_input_tokens": total.input_tokens,
                "total_output_tokens": total.output_tokens,
                "total_cache_write_tokens": total.cache_write_5m_tokens
                + total.cache_write_1h_tokens,
                "total_cache_write_5m_tokens": total.cache_write_5m_tokens,
                "total_cache_write_1h_tokens": total.cache_write_1h_tokens,
                "total_cache_read_tokens": total.cache_read_tokens,
                "total_messages": total.messages,
                "total_tool_uses": total_tool_uses,
                "total_cost": round(cost, 4),
            },
            "sessions": sessions,
            "tools": {
                "summary": tool_summary,
                "total": total_tool_uses,
            },
            "prompts": all_prompts[:50],
            "timeline": all_timeline,
        }


cache = StatsCache()


class MonitorHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Claude Monitor API."""

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/stats":
            data = cache.get_stats()
            self._json_response({"stats": data["stats"]})

        elif path == "/api/sessions":
            data = cache.get_stats()
            self._json_response({"sessions": data["sessions"]})

        elif path == "/api/tools/summary":
            data = cache.get_stats()
            self._json_response(data["tools"])

        elif path == "/api/usage/timeline":
            data = cache.get_stats()
            self._json_response({"timeline": data["timeline"]})

        elif path == "/api/prompts":
            data = cache.get_stats()
            self._json_response({"prompts": data["prompts"]})

        elif path == "/health":
            self._json_response({"status": "ok"})

        else:
            self._json_response({"error": "Not found"}, 404)

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        # Suppress default logging
        pass


def main():
    print(f"🔍 Claude Monitor starting on port {PORT}...")
    print(f"   Watching: {CLAUDE_PROJECTS_DIR}")

    # Initial stats load
    data = cache.get_stats()
    stats = data["stats"]
    print(f"   Sessions found: {len(data['sessions'])}")
    print(f"   Total messages: {stats['total_messages']}")
    print(f"   Total tool uses: {stats['total_tool_uses']}")
    print(f"   Total cost: ${stats['total_cost']:.4f}")

    server = HTTPServer(("0.0.0.0", PORT), MonitorHandler)
    print(f"   Serving on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Claude Monitor stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
