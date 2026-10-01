"""
Bot Detection Audit — MOVED to tools/stealth_audit.py

This file is a compatibility shim. Import from tools.stealth_audit instead.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.stealth_audit import print_audit, quick_check, run_stealth_audit

if __name__ == "__main__":
    from tools.stealth_audit import main

    main()
