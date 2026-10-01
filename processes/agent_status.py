import threading
import time

import requests
from constants import MONITOR_SERVICE_NAME
from core.logging import get_logger

logger = get_logger(MONITOR_SERVICE_NAME)

AGENT_STATUS_INTERVAL = 60


class AgentStatusReporter:
    def __init__(
        self, agent_id: str, status_url: str, interval: float = AGENT_STATUS_INTERVAL
    ):
        self._agent_id = agent_id
        self._status_url = status_url
        self._interval = interval
        self._working = False
        self._lock = threading.Lock()

    def set_working(self, working: bool) -> None:
        with self._lock:
            self._working = working

    def emit(self) -> None:
        with self._lock:
            working = self._working
        try:
            response = requests.post(
                self._status_url,
                json={"phantom_id": self._agent_id, "working": working},
                timeout=5,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning(f"Agent status report failed: {exc}")

    def start(self) -> None:
        def report() -> None:
            while True:
                self.emit()
                time.sleep(self._interval)

        threading.Thread(target=report, daemon=True).start()
