"""Project paths and scratch directories used by offline verification."""

from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]


def temporary_directory() -> TemporaryDirectory:
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    return TemporaryDirectory(dir=scratch)
