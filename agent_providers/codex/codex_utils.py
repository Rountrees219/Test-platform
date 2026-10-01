import glob
import json
from datetime import datetime
from pathlib import Path
from typing import List

from constants import CODEX_LOG_DIR


def get_file_time(file_path):
    # Hypothetically, the latest updated file is the current conversation
    # The logic might fail with subagents
    # Returns a stat_result object containing file metadata
    file_stat = file_path.stat()

    # Fallback mechanism for Unix/Mac/Windows systems
    try:
        timestamp = file_stat.st_birthtime  # Works on macOS and some Linux
    except AttributeError:
        timestamp = file_stat.st_ctime  # Creation on Windows, metadata change on Linux

    readable_time = datetime.fromtimestamp(timestamp)
    return readable_time


def session_id_from_file(file_path):
    """
    Extract the session_id from a given log .jsonl file.
    """
    with open(file_path, encoding="utf-8") as f:
        for raw in f:
            try:
                obj = json.loads(raw)
                payload = obj.get("payload", None)
                if payload:
                    sessions_id = payload.get("session_id", None)
                    if sessions_id:
                        return sessions_id
            except json.JSONDecodeError:
                continue


def get_latest_traces_session_id() -> str | None:
    """
    Get the session_id from the latest trace/log file in the codex logs in sessions folder.
    """
    transcript_glob = str(CODEX_LOG_DIR / "**" / "*.jsonl")
    transcripts = sorted(
        glob.glob(transcript_glob, recursive=True), key=lambda x: get_file_time(Path(x))
    )
    if transcripts:
        latest = transcripts[-1]
        return session_id_from_file(latest)


def get_all_session_ids(sessions_folder: Path) -> List[str]:
    """Return a list of all session_ids from the .jsonl files in the sessionsn folder"""
    return [session_id_from_file(path) for path in sessions_folder.rglob("*.jsonl")]


def check_session_id(id_to_check: str) -> bool:
    return id_to_check in get_all_session_ids(CODEX_LOG_DIR)
