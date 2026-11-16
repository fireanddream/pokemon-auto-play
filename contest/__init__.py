"""Platinum Super Contest automation and release-local support imports."""

from pathlib import Path
import sys


_directory = Path(__file__).resolve().parent
if (_directory / "host" / "paths.py").is_file() and str(_directory) not in sys.path:
    sys.path.insert(0, str(_directory))
