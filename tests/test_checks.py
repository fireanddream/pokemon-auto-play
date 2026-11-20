"""Shipped check independence, source/release paths, and input-free Lua parsing."""

import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from contest import checks, super_contest
from contest.messages import ContestCallerActive
from host.lua51 import Lua51
import run_contest
from tests.support.paths import ROOT, lua_dll_path, temporary_directory


DLL = lua_dll_path()


class LuaCompilerChecks(unittest.TestCase):
    def test_unicode_source_is_compiled_without_running_its_body(self):
        with temporary_directory() as folder:
            path = Path(folder) / "本地检查.lua"
            path.write_text('error("must not execute")\n', encoding="utf-8", newline="\n")
            with Lua51(DLL) as lua:
                lua.compile_file(path)

    def test_invalid_lua_is_reported_and_does_not_poison_the_next_compilation(self):
        with temporary_directory() as folder:
            path = Path(folder) / "source.lua"
            path.write_text("local =\n", encoding="utf-8", newline="\n")
            with Lua51(DLL) as lua:
                with self.assertRaises(RuntimeError):
                    lua.compile_file(path)
                path.write_text("return 4\n", encoding="utf-8", newline="\n")
                lua.compile_file(path)


class CheckLayoutChecks(unittest.TestCase):
    def test_source_and_release_layouts_resolve_their_own_selected_dll(self):
        with temporary_directory() as folder:
            base = Path(folder)
            for release in (False, True):
                with self.subTest(release=release):
                    bundle = base / ("release" if release else "source")
                    root = bundle / "contest" if release else bundle
                    root.mkdir(parents=True)
                    emulator = root / "external-emulator"
                    emulator.mkdir()
                    dll = emulator / "lua51.dll"
                    dll.write_bytes(b"test DLL path")
                    launcher = bundle / "run_contest.py"
                    launcher.write_text("DESMUME_PATH = " + json.dumps(str(emulator / "program.exe")) + "\n",
                        encoding="utf-8", newline="\n")
                    self.assertEqual(checks.launcher_path(root), launcher)
                    self.assertEqual(checks.resolve_lua_dll(root=root), dll)

    def test_unconfigured_release_reports_how_to_supply_its_dll(self):
        with temporary_directory() as folder:
            bundle = Path(folder)
            root = bundle / "contest"
            root.mkdir()
            (bundle / "run_contest.py").write_text('DESMUME_PATH = ""\n', encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(FileNotFoundError, "--lua-dll"):
                checks.resolve_lua_dll(root=root)
            self.assertEqual(checks.resolve_lua_dll(DLL, root=root), DLL)

    def test_shipped_runtime_has_no_import_dependency_on_the_test_package(self):
        for directory in (ROOT / "contest", ROOT / "host"):
            for path in directory.glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        names = [node.module or ""]
                    elif isinstance(node, ast.Import):
                        names = [value.name for value in node.names]
                    else:
                        continue
                    self.assertFalse(any(name == "tests" or name.startswith("tests.") for name in names), path.name)

    def test_primary_check_runs_when_the_test_package_is_unavailable(self):
        with patch.dict("sys.modules", {"tests": None}), redirect_stdout(io.StringIO()) as output, \
                patch.object(checks, "check_sources", wraps=checks.check_sources) as sources:
            super_contest.check(lua_dll=DLL)
        self.assertIn("Persistent contest Python/Lua syntax OK", output.getvalue())
        python, lua = sources.call_args.args
        self.assertIn("dance_response.py", {path.name for path in python})
        self.assertIn("memory_boundary.lua", {path.name for path in lua})
        self.assertTrue({"pt_rng_common.lua", "pt_rng_readers.lua"}.issubset(path.name for path in lua))


class LauncherPreflightChecks(unittest.TestCase):
    def setUp(self):
        folder = temporary_directory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.program = self.root / "program.exe"
        self.program.write_bytes(b"synthetic program placeholder")
        self.rom = self.root / "fixture.nds"
        header = bytearray(0x160)
        header[12:16] = b"CPUJ"
        self.rom.write_bytes(header)
        (self.root / "Battery").mkdir()
        self.battery = self.root / "Battery/fixture.dsv"
        self.battery.write_bytes(b"synthetic battery fixture")
        shutil.copy2(DLL, self.root / "lua51.dll")

    def test_missing_or_empty_matching_save_is_rejected_without_gameplay(self):
        self.battery.unlink()
        with self.assertRaises(FileNotFoundError):
            checks.validate_installation(self.program, self.rom)
        self.battery.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            checks.validate_installation(self.program, self.rom)

    def test_launcher_check_validates_and_never_calls_the_game_runner(self):
        selected = (self.program, self.rom, {"code": "CPUJ"})
        with patch.object(run_contest, "select_paths", return_value=selected), \
                patch.object(super_contest, "run") as run, redirect_stdout(io.StringIO()) as output:
            code = run_contest.main(["--check"])
        self.assertEqual(code, 0)
        self.assertIn("Installation OK", output.getvalue())
        run.assert_not_called()

    def test_duplicate_caller_remains_a_quiet_exit(self):
        with patch.object(run_contest, "select_paths", side_effect=ContestCallerActive("already running")), \
                patch.object(run_contest, "show_error_dialog") as popup, redirect_stdout(io.StringIO()):
            code = run_contest.main([])
        self.assertEqual(code, 1)
        popup.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
