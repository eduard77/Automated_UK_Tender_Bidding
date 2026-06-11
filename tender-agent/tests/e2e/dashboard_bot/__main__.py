"""Entry point so `python -m tests.e2e.dashboard_bot ...` runs the CLI."""
from __future__ import annotations

import sys

from tests.e2e.dashboard_bot.cli import main

if __name__ == "__main__":
    sys.exit(main())
