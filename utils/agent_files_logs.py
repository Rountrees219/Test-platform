import json
from pathlib import Path

_SESSION_HEADER_LINES = 20
_USER_TEXT_SENTINELS = ("<system-reminder>", "<user-prompt-submit-hook>")
_ASSISTANT_TEXT_SENTINELS = ("<system-reminder>",)


def _content_has_qualifying_text(*, content, sentinels: tuple[str, ...]) -> bool:
    if isinstance(content, str):
        text = content.strip()
        return bool(text) and not any(text.startswith(s) for s in sentinels)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = (block.get("text") or "").strip()
            if text and not any(text.startswith(s) for s in sentinels):
                return True
    return False


def _content_has_tool_use(*, content) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_use" for block in content
    )


def _is_real_user_message(entry: dict) -> bool:
    if entry.get("type") != "user":
        return False
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    message = entry.get("message") or {}
    return _content_has_qualifying_text(
        content=message.get("content"),
        sentinels=_USER_TEXT_SENTINELS,
    )


def _is_qualifying_assistant_turn(entry: dict) -> bool:
    """Count text or tool_use assistants (PostToolUse sees tool_use before text)."""
    if entry.get("type") != "assistant":
        return False
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    message = entry.get("message") or {}
    content = message.get("content")
    return _content_has_qualifying_text(
        content=content,
        sentinels=_ASSISTANT_TEXT_SENTINELS,
    ) or _content_has_tool_use(content=content)


def get_session_name_from_transcript(path: Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            scanned = 0
            for line in handle:
                if not line.strip():
                    continue
                scanned += 1
                if scanned > _SESSION_HEADER_LINES:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "custom-title":
                    return entry.get("customTitle")
                if entry.get("type") == "agent-name":
                    return entry.get("agentName")
    except OSError:
        return None
    return None


def count_turns_since_last_user_message(path: Path) -> int:
    turns = 0
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_real_user_message(entry):
                    turns = 0
                elif _is_qualifying_assistant_turn(entry):
                    turns += 1
    except OSError:
        return 0
    return turns
