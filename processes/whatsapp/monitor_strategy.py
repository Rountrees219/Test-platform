from __future__ import annotations

import threading

from processes.common import PollingMonitorStrategy
from services.whatsapp.monitor_service import main as legacy_main


class WhatsAppMonitorStrategy(PollingMonitorStrategy):
    def run(self) -> int:
        threading.Thread(
            target=lambda: legacy_main(argv=[]),
            name="legacy-wa-monitor",
            daemon=True,
        ).start()
        return super().run()
