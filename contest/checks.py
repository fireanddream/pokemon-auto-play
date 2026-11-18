"""Portable, input-free installation and source checks for both project layouts."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
from tempfile import TemporaryFile

from host.lua51 import Lua51
from host.paths import ROOT, project_path
from contest.platinum import identify_rom


HERE = Path(__file__).resolve().parent


def launcher_path(root=ROOT):
    root = Path(root).resolve()
    for candidate in (root / "run_contest.py", root.parent / "run_contest.py"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(2, "The launcher is missing from this installation", str(root / "run_contest.py"))


def resolve_lua_dll(value=None, *, root=ROOT):
    root = Path(root).resolve()
    if value is not None:
        dll = Path(value).resolve()
    else:
        tree = ast.parse(launcher_path(root).read_text(encoding="utf-8"))
        program = ""
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "DESMUME_PATH"):
                program = ast.literal_eval(node.value)
                if not isinstance(program, str):
                    raise ValueError("DESMUME_PATH must be a quoted emulator path or an empty string.")
        dll = project_path(program, root).parent / "lua51.dll" if program else root / "desmume-0.9.13-win64/lua51.dll"
    if not dll.is_file():
        raise FileNotFoundError(2, "Lua 5.1 DLL missing: select the emulator in the launcher or pass --lua-dll for --check", str(dll))
    return dll


def check_sources(python_files, lua_files, *, lua_dll=None, root=ROOT):
    python_files = tuple(dict.fromkeys(Path(path).resolve() for path in python_files))
    lua_files = tuple(dict.fromkeys(Path(path).resolve() for path in lua_files))
    for path in python_files:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            raise RuntimeError(f"Python syntax check failed: {error}") from error
    with Lua51(resolve_lua_dll(lua_dll, root=root)) as lua:
        for path in lua_files:
            lua.compile_file(path)
    return {"python_files": len(python_files), "lua_files": len(lua_files)}


def check_module(filename, lua_names, message, *, lua_dll=None):
    path = Path(filename).resolve()
    check_sources([path], [path.parent / name for name in lua_names], lua_dll=lua_dll)
    print(message)


def _require_file(path):
    if not path.is_file():
        raise FileNotFoundError(2, "A required installation file is missing", str(path))


def _check_writable(directory):
    with TemporaryFile(dir=directory) as probe:
        probe.write(b"write-check")
        probe.flush()


def validate_installation(program, rom, *, root=ROOT):
    """Validate local prerequisites; never launch, pause, or otherwise control a game."""
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required.")
    if os.name != "nt":
        raise RuntimeError("The emulator-control backend currently requires Windows.")
    from PIL import Image

    root = Path(root).resolve()
    program, rom = Path(program).resolve(), Path(rom).resolve()
    _require_file(program)
    if program.suffix.lower() != ".exe":
        raise ValueError("Select the DeSmuME .exe program.")
    _require_file(rom)
    if rom.suffix.lower() != ".nds":
        raise ValueError("Select a Pokemon Platinum .nds file.")
    game = identify_rom(rom)
    for relative in ("host/bootstrap.py", "host/lua51.py",
                     "host/controller.py", "lua/platinum.lua", "lua/checkpoint_master.lua"):
        _require_file(root / relative)
    _require_file(launcher_path(root))
    for name in ("super_contest.py", "super_contest.lua", "checks.py",
                 "timing_profiles/CPUJ.json", "timing_profiles/CPUE.json"):
        _require_file(HERE / name)
    battery = program.parent / "Battery" / f"{rom.stem}.dsv"
    _require_file(battery)
    if not battery.stat().st_size:
        raise ValueError(f"The matching battery save is empty: {battery}")
    with Lua51(program.parent / "lua51.dll"):
        pass
    scratch = root / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    for directory in (scratch, program.parent):
        _check_writable(directory)
    slots = program.parent / "StateSlots"
    if slots.exists():
        if not slots.is_dir():
            raise ValueError(f"StateSlots must be a folder beside the emulator: {slots}")
        _check_writable(slots)
    return {"game": game, "battery_save": str(battery), "pillow_version": Image.__version__}
