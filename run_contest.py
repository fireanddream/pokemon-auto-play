"""Direct entry point for a fresh Platinum contest collection run."""

from __future__ import annotations

# Saved after selection. Use full file paths with forward slashes; clear to select again.
DESMUME_PATH = "D:/Program Files (x86)/Pokemon Stuff/Pokemon auto play/desmume-0.9.13-win64/DeSmuME_0.9.13_x64.exe"
ROM_PATH = "D:/Program Files (x86)/Pokemon Stuff/Pokemon auto play/desmume-0.9.13-win64/Roms/Pokemon - Platinum Version (USA) (Rev 1).nds"

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SCRIPT = Path(__file__).resolve()
SUPPORT = SCRIPT.parent / "contest"
MODULE_ROOT = SUPPORT if (SUPPORT / "host" / "paths.py").is_file() else SCRIPT.parent
sys.path.insert(0, str(MODULE_ROOT))
if MODULE_ROOT != SCRIPT.parent:
    sys.path.insert(0, str(SCRIPT.parent))

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    private_console = False
    if os.name == "nt":
        from host.windows import hide_private_console

        private_console = hide_private_console()
    quiet = os.name == "nt" and (private_console or Path(sys.executable).stem.lower() == "pythonw")
    environment = MODULE_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    python = environment / ("pythonw.exe" if quiet else "python.exe" if os.name == "nt" else "python")
    if not python.is_file():
        python = environment / ("python.exe" if os.name == "nt" else "python")
    if not python.is_file():
        python = Path(sys.executable).absolute()
        if quiet and python.with_name("pythonw.exe").is_file():
            python = python.with_name("pythonw.exe")
    if python != Path(sys.executable).absolute() or private_console:
        child = subprocess.Popen([str(python), "-B", str(SCRIPT), *sys.argv[1:]],
            creationflags=subprocess.CREATE_NO_WINDOW if quiet else 0)
        if quiet:
            raise SystemExit(0)
        while True:
            try:
                raise SystemExit(child.wait())
            except KeyboardInterrupt:
                # The child receives the same interrupt and performs its own cleanup.
                continue


from contest.messages import ContestCallerActive, abort_message
from contest.platinum import identify_rom
from host.paths import ROOT, project_path


def remember_paths(*, program=None, rom=None):
    script = Path(__file__).resolve()
    source = script.read_text(encoding="utf-8")
    for name, path in (("DESMUME_PATH", program), ("ROM_PATH", rom)):
        if path is None:
            continue
        assignment = name + " = " + json.dumps(path.as_posix(), ensure_ascii=True)
        source, count = re.subn(rf"^{name}\s*=.*$", lambda _: assignment, source, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"Keep {name} on one line at the top of run_contest.py so the selection can be saved.")
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    temporary = scratch / f"launcher-paths-{os.getpid()}.py"
    temporary.write_text(source, encoding="utf-8", newline="\n")
    temporary.replace(script)


def select_paths(*, choose=False):
    from host.dialogs import pick_file

    for name, value in (("DESMUME_PATH", DESMUME_PATH), ("ROM_PATH", ROM_PATH)):
        if not isinstance(value, str):
            raise ValueError(f"{name} at the top of run_contest.py must be a quoted file path or an empty string.")
    program = project_path(DESMUME_PATH) if DESMUME_PATH and not choose else None
    selected_program = program is None or not program.is_file()
    if selected_program:
        # Deliberately inspect this folder only, never its subfolders.
        programs = [] if choose else sorted(path for path in SCRIPT.parent.glob("*.exe")
            if path.name.lower().startswith("desmume") and "0.9.13" in path.name and path.is_file())
        program = programs[0] if len(programs) == 1 else pick_file(
            "Select the DeSmuME program", SCRIPT.parent, "Windows programs", "*.exe")
        if program is None:
            raise RuntimeError("Emulator selection was cancelled.")
    if not program.is_file() or program.suffix.lower() != ".exe":
        raise ValueError("Select the DeSmuME .exe program.")

    rom = project_path(ROM_PATH) if ROM_PATH and not choose else None
    selected_rom = rom is None or not rom.is_file()
    if selected_rom:
        rom = pick_file(
            "Select a Pokemon Platinum ROM", program.parent / "Roms", "Nintendo DS ROMs", "*.nds")
        if rom is None:
            raise RuntimeError("ROM selection was cancelled.")
    game = identify_rom(rom)
    if selected_program or selected_rom:
        remember_paths(program=program if selected_program else None, rom=rom if selected_rom else None)
    return program, rom, game


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--choose-paths", action="store_true", help="Pick and save a different emulator and ROM.")
    args = parser.parse_args(argv)

    try:
        if sys.version_info < (3, 10):
            raise RuntimeError("Python 3.10 or newer is required.")
        try:
            import PIL
        except ImportError as error:
            raise RuntimeError("Pillow is missing. Install the project requirements, then open the launcher again.") from error
        if os.name != "nt":
            raise RuntimeError("The emulator-control backend currently requires Windows.")
        program, rom, game = select_paths(choose=args.choose_paths)
        from contest.super_contest import run
        from host.controller import ControllerConfig

        print(f"Platinum {game['code']} ({rom.name})", flush=True)
        run_name = datetime.now().strftime("contest-%Y%m%d-%H%M%S-%f")
        mailbox = ROOT / "tmp" / "contest" / run_name / "mailbox"

        result = run(run_name=run_name,
            controller_config=ControllerConfig(executable=program, rom=rom, mailbox_dir=mailbox),
            fresh_start=True)
        if result["outcome"] == "pass":
            return 0
        text = "timeout" if result["outcome"] == "timeout" else result.get("message", result.get("error", "The contest stopped."))
        print(text, flush=True)
    except ContestCallerActive as error:
        text = abort_message(error)
        print(text, flush=True)
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        text = abort_message(error)
        print(text, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
