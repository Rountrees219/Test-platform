"""Entry point so ``python -m processes.upgrade [run ...]`` works."""

import sys

from processes.upgrade.engine import main

if __name__ == "__main__":
    sys.exit(main())
