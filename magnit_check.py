#!/usr/bin/env python3
"""Совместимость со старым запуском: python3 magnit_check.py ... → magnit_mcp.cli."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from magnit_mcp.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
