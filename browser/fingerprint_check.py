"""
Fingerprint Check — MOVED to tools/stealth_audit.py

This file is a compatibility shim. Use tools/stealth_audit.py instead.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.stealth_audit import quick_check, run_stealth_audit

if __name__ == "__main__":
    from tools.stealth_audit import main

    main()
