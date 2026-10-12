"""Starts Kyty Live from any working directory: python run.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kyty_live.server import main  # noqa: E402

sys.exit(main())
