"""
Parser for Claude Code ``--output-format json`` subprocess output.

The JSON result from ``claude -p --output-format json`` contains session
metadata, cost estimates, token usage, and the text result in a single
structured payload.  This module extracts those fields so callers never
need to read JSONL transcript files.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ClaudeResult(BaseModel):
    """Parsed fields from a ``--output-format json`` result message."""

    model_config = ConfigDict(populate_by_name=True)

    text: str = Field(default="", alias="result")
    session_id: str = ""
    is_error: bool = False
    total_cost_usd: float = 0.0
    model_usage: dict[str, Any] = Field(default_factory=dict, alias="modelUsage")
    usage: dict[str, Any] = Field(default_factory=dict)
    num_turns: int = 0
    duration_ms: int = 0
    duration_api_ms: int = 0
    subtype: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


def parse_claude_output(stdout: str) -> ClaudeResult:
    """Parse ``--output-format json`` stdout into a :class:`ClaudeResult`.

    Always returns a ``ClaudeResult``.  On parse failure ``is_error`` is
    ``True`` and diagnostic info is stored in ``raw``.
    """
    if not stdout or not stdout.strip():
        return ClaudeResult(is_error=True, raw={"_parse_error": "empty stdout"})

    try:
        result = ClaudeResult.model_validate_json(stdout)
        result.raw = json.loads(stdout)
        return result
    except Exception as exc:
        return ClaudeResult(
            is_error=True,
            raw={"_parse_error": str(exc), "_raw_stdout": stdout[:1000]},
        )


def primary_model(parsed: ClaudeResult) -> str:
    """Return the first model name from ``model_usage``, or ``""``."""
    if parsed.model_usage:
        return next(iter(parsed.model_usage), "")
    return ""
