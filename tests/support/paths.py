"""Project paths and scratch directories used by offline verification."""

import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]


def lua_dll_path():
    value = os.environ.get("AUTOPLAY_TEST_LUA_DLL")
    return Path(value).resolve() if value else ROOT / "desmume-0.9.13-win64/lua51.dll"


def fixture_path(name):
    """Retain a private test input in durable storage, preserving any existing copy."""
    if Path(name).name != name:
        raise ValueError("Use a fixture filename, without a folder path.")
    target = ROOT / "tests/fixtures/local" / name
    if target.is_file():
        return target
    if target.exists():
        raise FileExistsError(f"The private fixture path is not a file: {target}")
    source = ROOT / "contest/evidence" / name
    if not source.is_file():
        raise FileNotFoundError(2, "A required private test fixture is missing; see tests/fixtures/README.md", str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def temporary_directory() -> TemporaryDirectory:
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    return TemporaryDirectory(dir=scratch)
