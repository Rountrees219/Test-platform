import logging
import os
from logging import FileHandler, LoggerAdapter
from pathlib import Path

from clients.super_ninja_client import get_thread_id
from pythonjsonlogger.json import JsonFormatter

LOG_DIR = Path("/workspace/logs")
SHARED_HANDLER = None


def get_logger(service: str, **context) -> LoggerAdapter:
    logger = logging.getLogger(f"ninja.{service}")
    if not logger.handlers:
        logger.setLevel(os.environ.get("NINJA_LOG_LEVEL", "INFO").upper())
        logger.propagate = False
        logger.addHandler(get_handler())

    return LoggerAdapter(
        logger, {"service": service, "thread_id": get_thread_id(), **context}
    )


def get_handler() -> FileHandler:
    # Plain append handler: logrotate rotates /workspace/logs/*.log with
    # copytruncate, so the fd stays valid across rotations and owns retention itself.
    global SHARED_HANDLER
    if SHARED_HANDLER is None:
        # In tests /workspace/logs is never created, and FileHandler surfaces the missing path
        # as an unswallowed FileNotFoundError on first emit, so route logs to the null device.
        if os.environ.get("NINJA_TEST_MODE") == "1":
            log_path = Path(os.devnull)
        else:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOG_DIR / "ninja.log"
        handler = FileHandler(log_path, encoding="utf-8", delay=True)
        handler.setFormatter(
            JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
                rename_fields={
                    "asctime": "timestamp",
                    "levelname": "level",
                    "name": "logger",
                },
            )
        )
        SHARED_HANDLER = handler
    return SHARED_HANDLER
