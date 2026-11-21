"""Direct entry point for a fresh Platinum contest collection run."""

from __future__ import annotations

# Saved after selection. Use full file paths with forward slashes; clear to select again.
DESMUME_PATH = "D:/Program Files (x86)/Pokemon Stuff/Pokemon auto play/desmume-0.9.13-win64/DeSmuME_0.9.13_x64.exe"
ROM_PATH = "D:/Program Files (x86)/Pokemon Stuff/Pokemon auto play/desmume-0.9.13-win64/Roms/\u767d\u91d1\u3000\u4e2d\u6587.nds"
debug = 0

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback

RELEASE = "0.92"
SCRIPT = Path(__file__).resolve()
SUPPORT = SCRIPT.parent / "contest"
MODULE_ROOT = SUPPORT if (SUPPORT / "host" / "paths.py").is_file() else SCRIPT.parent
sys.path.insert(0, str(MODULE_ROOT))
if MODULE_ROOT != SCRIPT.parent:
    sys.path.insert(0, str(SCRIPT.parent))

def clean_diagnostic(text):
    text = re.sub(r"[a-fA-F0-9]{32,}", "[redacted]", str(text))
    return re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", text)


def safe_print(text):
    try:
        print(clean_diagnostic(text), flush=True)
    except (OSError, UnicodeError, ValueError):
        pass


def append_startup_log(text):
    path = MODULE_ROOT / "tmp" / "startup.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(clean_diagnostic(text).rstrip("\r\n") + "\n")
    return path


def setup_output(text):
    append_startup_log(text)
    safe_print(text)


def show_error_dialog(text):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(None, text, f"Pokemon Auto Play {RELEASE}", 0x10 | 0x10000)


def report_startup_error(error):
    try:
        from contest.messages import abort_message

        message = abort_message(error)
    except ImportError:
        message = str(error) or "The launcher could not start."
    if isinstance(error, ModuleNotFoundError):
        message += "\nThe support folder is incomplete. Extract run_contest.py together with the full contest folder."
    elif isinstance(error, PermissionError):
        message += "\nUse writable folders for both the project and the emulator."
    message = clean_diagnostic(message)
    details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    try:
        path = append_startup_log(f"{datetime.now().isoformat(timespec='seconds')} Startup failed:\n{details}")
        message += f"\n\nDetails: {path}\nResolve the error, then open the launcher again."
    except OSError:
        message += "\n\nThe startup log could not be written. Extract the project into a writable folder and try again."
    safe_print(message)
    try:
        show_error_dialog(message)
    except OSError:
        pass


class LauncherParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message + "\nRun the launcher with --help to see the supported options.")


def argument_parser():
    parser = LauncherParser(description=__doc__)
    parser.add_argument("--version", action="version", version=RELEASE)
    parser.add_argument("--choose-paths", action="store_true", help="Pick and save a different emulator and ROM.")
    parser.add_argument("--check", action="store_true", help="Check the installation without starting gameplay.")
    return parser


def remember_paths(*, program=None, rom=None):
    from host.paths import ROOT

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


def select_paths(*, choose=False, interactive=True):
    from host.dialogs import pick_file
    from host.paths import project_path
    from contest.platinum import identify_rom

    for name, value in (("DESMUME_PATH", DESMUME_PATH), ("ROM_PATH", ROM_PATH)):
        if not isinstance(value, str):
            raise ValueError(f"{name} at the top of run_contest.py must be a quoted file path or an empty string.")
    program = project_path(DESMUME_PATH) if DESMUME_PATH and not choose else None
    selected_program = program is None or not program.is_file()
    if selected_program:
        if not interactive:
            raise RuntimeError("Select the emulator first: open run_contest.py with --choose-paths.")
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
        if not interactive:
            raise RuntimeError("Select the ROM first: open run_contest.py with --choose-paths.")
        rom = pick_file(
            "Select a Pokemon Platinum ROM", program.parent / "Roms", "Nintendo DS ROMs", "*.nds")
        if rom is None:
            raise RuntimeError("ROM selection was cancelled.")
    game = identify_rom(rom)
    if selected_program or selected_rom:
        remember_paths(program=program if selected_program else None, rom=rom if selected_rom else None)
    return program, rom, game


def main(argv=None):
    caller_active = ()
    try:
        args = argument_parser().parse_args(argv)
        from contest.messages import ContestCallerActive
        from host.paths import ROOT

        caller_active = ContestCallerActive
        if sys.version_info < (3, 10):
            raise RuntimeError("Python 3.10 or newer is required.")
        try:
            import PIL
        except ImportError as error:
            raise RuntimeError("Pillow could not be loaded. Open the launcher again to repair the local environment.") from error
        if os.name != "nt":
            raise RuntimeError("The emulator-control backend currently requires Windows.")
        program, rom, game = select_paths(choose=args.choose_paths, interactive=not args.check or args.choose_paths)
        from contest.checks import validate_installation
        from contest.super_contest import check, run
        from host.controller import ControllerConfig

        validate_installation(program, rom)
        if args.check:
            check(lua_dll=program.parent / "lua51.dll")
            message = f"Installation OK: {RELEASE}; ROM header accepted as {game['code']}. Gameplay was not started."
            append_startup_log(message)
            safe_print(message)
            return 0

        safe_print(f"Platinum {game['code']} ({rom.name}); {RELEASE}")
        run_name = datetime.now().strftime("contest-%Y%m%d-%H%M%S-%f")
        mailbox = ROOT / "tmp" / "contest" / run_name / "mailbox"

        result = run(run_name=run_name,
            controller_config=ControllerConfig(executable=program, rom=rom, mailbox_dir=mailbox),
            fresh_start=True, debug=debug)
        if result["outcome"] == "pass":
            return 0
        text = "timeout" if result["outcome"] == "timeout" else result.get("message", result.get("error", "The contest stopped."))
        if not result.get("lifecycle", {}).get("lua_attachments"):
            report_startup_error(RuntimeError(text))
        else:
            safe_print(text)
    except SystemExit as error:
        if error.code in (0, None):
            return 0
        report_startup_error(error)
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, caller_active):
            safe_print(str(error))
        else:
            report_startup_error(error)
    return 1


def entry_point(argv=None):
    """Bootstrap before importing Pillow-dependent runners or opening the emulator."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        argument_parser().parse_args(arguments)
        if sys.version_info < (3, 10):
            raise RuntimeError("Python 3.10 or newer is required.")
        if os.name != "nt":
            raise RuntimeError("The emulator-control backend currently requires Windows.")
        from host.windows import hide_private_console
        from host.bootstrap import EnvironmentBusy, ensure_environment

        private_console = hide_private_console()
        quiet = private_console or Path(sys.executable).stem.lower() == "pythonw" or os.environ.get("AUTOPLAY_WINDOWLESS") == "1"
        try:
            directory = ensure_environment(MODULE_ROOT, quiet=quiet, output=setup_output)
        except EnvironmentBusy:
            return 1
        python = directory / ("Scripts/pythonw.exe" if quiet else "Scripts/python.exe")
        if not python.is_file():
            python = directory / "Scripts/python.exe"
        if python.resolve() != Path(sys.executable).resolve() or private_console:
            environment = dict(os.environ)
            if quiet:
                environment["AUTOPLAY_WINDOWLESS"] = "1"
            child = subprocess.Popen([str(python), "-B", str(SCRIPT), *arguments], env=environment,
                stdin=subprocess.DEVNULL if quiet else None, stdout=subprocess.DEVNULL if quiet else None,
                stderr=subprocess.DEVNULL if quiet else None,
                creationflags=subprocess.CREATE_NO_WINDOW if quiet else 0)
            if quiet:
                return 0
            while True:
                try:
                    return child.wait()
                except KeyboardInterrupt:
                    # The child receives the same interrupt and performs its own cleanup.
                    continue
        return main(arguments)
    except SystemExit as error:
        if error.code in (0, None):
            return 0
        report_startup_error(error)
    except (Exception, KeyboardInterrupt) as error:
        report_startup_error(error)
    return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(entry_point())
