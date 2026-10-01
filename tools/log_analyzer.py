#!/usr/bin/env python3
"""
Log Analyzer — Parse Claude Code JSONL logs for cost, errors, and performance.

Reads the JSONL streaming logs produced by Claude Code sessions and provides
token usage breakdowns, cost calculations, error summaries, and trajectory analysis.

Usage:
    python tools/log_analyzer.py <logfile>                # Analyze single log
    python tools/log_analyzer.py <logdir> --summary       # Summary of all logs
    python tools/log_analyzer.py <logfile> --errors        # Show only errors
    python tools/log_analyzer.py <logfile> --json          # JSON output
    python tools/log_analyzer.py <logfile> --cost          # Cost breakdown only

Python API:
    from tools.log_analyzer import analyze_log, analyze_directory
    result = analyze_log("/workspace/logs/session.jsonl")
    print(result["total_cost"])
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

from utils.cost import compute_cost_breakdown


def analyze_log(filepath: str) -> dict:
    """
    Analyze a single JSONL log file.

    Returns:
        Dict with keys: tokens, cost, errors, model, duration, tool_calls, messages
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return {"error": f"File not found: {filepath}"}

    tokens = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
    }
    errors = []
    tool_calls = []
    model = "unknown"
    messages = 0
    first_ts = None
    last_ts = None

    # Deduplication: track last usage to skip duplicate streaming chunks
    last_usage_key = None

    try:
        with open(filepath, "r") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Track timestamps
                ts = entry.get("timestamp") or entry.get("ts")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                msg = entry.get("message", {})

                # Extract model (inside message object)
                if msg.get("model"):
                    model = msg["model"]

                # Extract usage (with deduplication)
                usage = msg.get("usage", {})
                if usage:
                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cr = usage.get(
                        "cache_read_input_tokens", usage.get("cache_read", 0)
                    )

                    # Break down cache writes into 5m and 1h tiers
                    cache_creation = usage.get("cache_creation", {})
                    cw_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
                    cw_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
                    # Fallback: if no breakdown, attribute all to 5m
                    if not cw_5m and not cw_1h:
                        cw_5m = usage.get(
                            "cache_creation_input_tokens", usage.get("cache_write", 0)
                        )

                    usage_key = (inp, out, cr, cw_5m, cw_1h)

                    if usage_key != last_usage_key and usage_key != (0, 0, 0, 0, 0):
                        tokens["input"] += inp
                        tokens["output"] += out
                        tokens["cache_read"] += cr
                        tokens["cache_write_5m"] += cw_5m
                        tokens["cache_write_1h"] += cw_1h
                        messages += 1
                        last_usage_key = usage_key

                # Extract errors
                entry_type = entry.get("type", "")
                if (
                    entry_type == "error"
                    or "error" in str(entry.get("message", "")).lower()
                ):
                    errors.append(
                        {
                            "line": line_num,
                            "message": entry.get(
                                "message", entry.get("error", "unknown")
                            ),
                            "timestamp": ts,
                        }
                    )

                # Extract tool calls
                if entry_type == "tool_use" or entry.get("tool_name"):
                    tool_calls.append(
                        {
                            "tool": entry.get(
                                "tool_name", entry.get("name", "unknown")
                            ),
                            "timestamp": ts,
                        }
                    )

    except IOError as e:
        return {"error": f"Cannot read file: {e}"}

    cost = compute_cost_breakdown(
        model,
        tokens["input"],
        tokens["output"],
        tokens["cache_write_5m"],
        tokens["cache_write_1h"],
        tokens["cache_read"],
    )
    cost["total"] = sum(cost.values())

    # Calculate duration
    duration = None
    if first_ts and last_ts:
        try:
            t1 = datetime.fromisoformat(str(first_ts).replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            duration = (t2 - t1).total_seconds()
        except (ValueError, TypeError):
            pass

    return {
        "file": str(filepath),
        "model": model,
        "tokens": tokens,
        "cost": cost,
        "total_cost": round(cost["total"], 4),
        "messages": messages,
        "errors": errors,
        "error_count": len(errors),
        "tool_calls": tool_calls,
        "tool_call_count": len(tool_calls),
        "duration_seconds": duration,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
    }


def analyze_directory(dirpath: str, pattern: str = "*.jsonl") -> dict:
    """
    Analyze all log files in a directory.

    Returns:
        Dict with per-file results and aggregate summary.
    """
    dirpath = Path(dirpath)
    if not dirpath.is_dir():
        return {"error": f"Not a directory: {dirpath}"}

    files = sorted(dirpath.glob(pattern))
    if not files:
        # Also try .log files
        files = sorted(dirpath.glob("*.log"))

    results = []
    total_cost = 0
    total_tokens = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
    }
    total_errors = 0

    for f in files:
        result = analyze_log(str(f))
        if "error" not in result:
            results.append(result)
            total_cost += result.get("total_cost", 0)
            total_errors += result.get("error_count", 0)
            for key in total_tokens:
                total_tokens[key] += result.get("tokens", {}).get(key, 0)

    return {
        "directory": str(dirpath),
        "file_count": len(results),
        "results": results,
        "summary": {
            "total_cost": round(total_cost, 4),
            "total_tokens": total_tokens,
            "total_errors": total_errors,
        },
    }


def print_analysis(result: dict, show_errors: bool = False, cost_only: bool = False):
    """Pretty-print analysis results."""
    if "error" in result:
        print(f"❌ {result['error']}")
        return

    print(f"\n{'=' * 60}")
    print(f"📊 LOG ANALYSIS: {result.get('file', 'unknown')}")
    print(f"{'=' * 60}")

    # Model and duration
    print(f"\n  Model: {result['model']}")
    dur = result.get("duration_seconds")
    if dur:
        mins = int(dur // 60)
        secs = int(dur % 60)
        print(f"  Duration: {mins}m {secs}s")
    print(f"  Messages: {result['messages']}")

    # Tokens
    if not cost_only:
        tokens = result["tokens"]
        print(f"\n  📈 Token Usage:")
        print(f"    Input:           {tokens['input']:>12,}")
        print(f"    Output:          {tokens['output']:>12,}")
        print(f"    Cache Read:      {tokens['cache_read']:>12,}")
        print(f"    Cache Write 5m:  {tokens['cache_write_5m']:>12,}")
        print(f"    Cache Write 1h:  {tokens['cache_write_1h']:>12,}")

    # Cost
    cost = result["cost"]
    print(f"\n  💰 Cost Breakdown:")
    print(f"    Input:       ${cost['input']:>8.4f}")
    print(f"    Output:      ${cost['output']:>8.4f}")
    print(f"    Cache Read:  ${cost['cache_read']:>8.4f}")
    print(f"    Cache Write 5m: ${cost['cache_write_5m']:>8.4f}")
    print(f"    Cache Write 1h: ${cost['cache_write_1h']:>8.4f}")
    print(f"    ─────────────────────")
    print(f"    TOTAL:       ${result['total_cost']:>8.4f}")

    # Tool calls
    if not cost_only and result.get("tool_calls"):
        print(f"\n  🔧 Tool Calls ({result['tool_call_count']}):")
        # Count by tool name
        tool_counts = {}
        for tc in result["tool_calls"]:
            name = tc["tool"]
            tool_counts[name] = tool_counts.get(name, 0) + 1
        for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
            print(f"    {name:30s} × {count}")

    # Errors
    if result.get("errors"):
        print(f"\n  ⚠️  Errors ({result['error_count']}):")
        for err in result["errors"][:10]:  # Show max 10
            msg = str(err.get("message", ""))[:80]
            print(f"    Line {err.get('line', '?')}: {msg}")
        if result["error_count"] > 10:
            print(f"    ... and {result['error_count'] - 10} more")
    elif show_errors:
        print(f"\n  ✅ No errors found")

    print(f"\n{'=' * 60}\n")


def print_summary(result: dict):
    """Pretty-print directory summary."""
    if "error" in result:
        print(f"❌ {result['error']}")
        return

    summary = result["summary"]

    print(f"\n{'=' * 60}")
    print(f"📊 LOG SUMMARY: {result['directory']}")
    print(f"{'=' * 60}")
    print(f"\n  Files analyzed: {result['file_count']}")
    print(f"  Total cost: ${summary['total_cost']:.4f}")
    print(f"  Total errors: {summary['total_errors']}")

    tokens = summary["total_tokens"]
    print(f"\n  📈 Aggregate Tokens:")
    print(f"    Input:           {tokens['input']:>12,}")
    print(f"    Output:          {tokens['output']:>12,}")
    print(f"    Cache Read:      {tokens['cache_read']:>12,}")
    print(f"    Cache Write 5m:  {tokens['cache_write_5m']:>12,}")

    if result.get("results"):
        print(f"\n  📁 Per-File Costs:")
        for r in result["results"]:
            fname = Path(r["file"]).name
            print(f"    {fname:40s} ${r['total_cost']:>8.4f}")

    print(f"\n{'=' * 60}\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Log Analyzer — Parse Claude Code logs for cost, errors, and performance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/log_analyzer.py session.jsonl          Analyze a single log
  python tools/log_analyzer.py /workspace/logs/       Summary of all logs
  python tools/log_analyzer.py session.jsonl --cost   Cost breakdown only
  python tools/log_analyzer.py session.jsonl --errors Show errors only
  python tools/log_analyzer.py session.jsonl --json   JSON output
        """,
    )
    parser.add_argument("path", help="Log file or directory to analyze")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--summary", action="store_true", help="Summary mode for directories"
    )
    parser.add_argument("--errors", action="store_true", help="Show only errors")
    parser.add_argument("--cost", action="store_true", help="Show only cost breakdown")

    args = parser.parse_args()
    path = Path(args.path)

    if path.is_dir() or args.summary:
        result = analyze_directory(str(path))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print_summary(result)
    else:
        result = analyze_log(str(path))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print_analysis(result, show_errors=args.errors, cost_only=args.cost)

    # Exit code
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
