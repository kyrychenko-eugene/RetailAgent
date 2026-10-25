"""CLI entry point: python -m retail_agent [--exec ID] [--profile NAME]."""
from __future__ import annotations

import sys

from retail_agent.cli.app import main

if __name__ == "__main__":
    sys.exit(main())
