# Ninja Tools

Reusable utility tools for the Ninja browser automation agent. Each tool works both as a Python importable module and as a standalone CLI command.

## Available Tools

| Tool | Purpose | CLI Usage |
|------|---------|-----------|
| `cron.py` | Schedule recurring agent prompts (see `agent-docs/CRON.md`) | `python tools/cron.py add ...`, `list`, `trigger` |
| `issues.py` | GitHub-issue work queue for the agent loop (see `agent-docs/LOOP.md`) | `python tools/issues.py list`, `count --json`, `create --title ... --body ...`, `comment <n> --body ...`, `close <n> --comment ...` |
| `health_check.py` | System diagnostics | `python tools/health_check.py` |
| `log_analyzer.py` | Parse Claude Code JSONL logs | `python tools/log_analyzer.py <logfile>` |
| `stealth_audit.py` | Browser stealth verification | `python tools/stealth_audit.py` |
| `session_manager.py` | Save/restore browser sessions | `python tools/session_manager.py list` |
| `message_sanitizer.py` | Strip LLM artifacts from text | `python tools/message_sanitizer.py "text"` |
| `pause.py` | Stop or resume the orchestrator and every cron job | `python tools/pause.py pause`, `resume`, `status --json` |

`pdx.py` is installed as `/usr/local/bin/pdx` by `install.sh`; see `agent-docs/PIPEDREAM_CONNECT.md`.

## Tool Design Principles

1. **One thing well** — Each tool has a single clear purpose
2. **CLI + Python API** — Every tool has `if __name__ == "__main__"` and importable functions
3. **Structured output** — Use JSON output (`--json`) where possible for composability
4. **Self-documenting** — `--help` explains everything
5. **Error handling** — Helpful error messages, non-zero exit on failure
6. **Independent** — Minimal cross-dependencies

## Adding New Tools

1. Create `tools/<name>.py` with a clear docstring
2. Add both Python API functions and CLI entry point
3. Test: `python tools/<name>.py --help` and `python tools/<name>.py <test_args>`
4. Add an entry to this README table
5. Commit with a descriptive message

## Quick Examples

```bash
# Check system health
python tools/health_check.py
python tools/health_check.py --json

# Analyze a log file for costs
python tools/log_analyzer.py /workspace/logs/Ninja_2025-03-20.log
python tools/log_analyzer.py /workspace/logs/ --summary

# Run stealth audit on live browser
python tools/stealth_audit.py
python tools/stealth_audit.py --json

# Manage browser sessions
python tools/session_manager.py list
python tools/session_manager.py save my_session
python tools/session_manager.py restore my_session

# Sanitize text
python tools/message_sanitizer.py "Here's some text with 🚀 emojis — and fancy punctuation!!!"

# Issue work queue (agent loop — see agent-docs/LOOP.md)
python tools/issues.py list
python tools/issues.py count --json
python tools/issues.py create --title "Fix flaky test" --body "details"
python tools/issues.py close 42 --comment "done in PR #99"
```
