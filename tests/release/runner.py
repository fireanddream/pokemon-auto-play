"""Verify a fresh release and its bundled dependencies without controlling a game."""

import ast
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from zipfile import ZipFile

from host.bootstrap import bundled_wheels
from host.lua51 import Lua51
from contest.checks import RUNTIME_SOURCES
from run_contest import clean_diagnostic
from tests.support.paths import ROOT, fixture_path, lua_dll_path
from tools.prepare_dependencies import PYTHON_VERSIONS, check_wheel_coverage


CHECK_MODULES = ("super_contest",)
RUNTIME_MEMBERS = {"contest/" + (name if folder == "contest" else folder + "/" + name)
    for folder, names in RUNTIME_SOURCES.items() for name in names}


def safe_output(text):
    text = clean_diagnostic(text)
    if sys.stdout is None:
        return
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, errors="backslashreplace").decode(encoding), flush=True)


class ReleaseChecks:
    def __init__(self, lua_dll=None, output=safe_output):
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + f"-{os.getpid()}"
        self.work = ROOT / "tmp/release-checks" / name
        self.work.mkdir(parents=True)
        self.bundle = self.work / "bundle"
        self.lua_dll = Path(lua_dll).resolve() if lua_dll is not None else lua_dll_path()
        self.output = output
        self.stage = "prerequisites"
        self.result = {"outcome": "running", "stages": [], "report": str(self.work / "report.json")}
        self.environment = {key: value for key, value in os.environ.items()
            if not key.upper().startswith("PIP_") and key not in {"AUTOPLAY_WINDOWLESS", "PYTHONPATH"}}
        self.environment.update(TMP=str(self.work), TEMP=str(self.work), TMPDIR=str(self.work),
            PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
            PIP_CONFIG_FILE=os.devnull, AUTOPLAY_TEST_LUA_DLL=str(self.lua_dll),
            HTTP_PROXY="http://127.0.0.1:1", HTTPS_PROXY="http://127.0.0.1:1", ALL_PROXY="http://127.0.0.1:1",
            NO_PROXY="", no_proxy="")

    def log(self, text):
        text = clean_diagnostic(text).rstrip("\r\n")
        with (self.work / "checks.log").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text + "\n")
        self.output(text)

    def command(self, arguments, *, cwd=None):
        chunks = []
        with subprocess.Popen([str(value) for value in arguments], cwd=cwd or ROOT,
                env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as child:
            try:
                for line in child.stdout:
                    chunks.append(clean_diagnostic(line))
                    self.log(line)
                code = child.wait()
            except BaseException:
                child.terminate()
                child.wait()
                raise
        if code:
            raise RuntimeError(f"Verification process exited with code {code}.")
        return "".join(chunks)

    def run_stage(self, name, callback):
        self.stage = name
        self.log(f"Release check: {name}")
        value = callback()
        self.result["stages"].append({"name": name, "outcome": "pass"})
        return value

    def prerequisites(self):
        if os.name != "nt" or sys.version_info < (3, 10):
            raise RuntimeError("Release checks require Windows and Python 3.10 or newer.")
        self.wheel_bundle(ROOT)
        fixture_path("completed.png")
        with Lua51(self.lua_dll):
            pass

    def wheel_bundle(self, root):
        wheels = bundled_wheels(root)
        manifest = json.loads((wheels / "manifest.json").read_text(encoding="utf-8"))
        versions = manifest.get("python_versions")
        if (manifest.get("implementation") != "cp" or not isinstance(versions, list)
                or len(versions) != len(PYTHON_VERSIONS)
                or any(version not in versions for version in PYTHON_VERSIONS)):
            raise RuntimeError("Release wheels must cover standard Windows x64 CPython " + ", ".join(PYTHON_VERSIONS) + ".")
        check_wheel_coverage(manifest["wheels"])
        return manifest

    def syntax_and_endings(self):
        paths = set()
        for folder in (ROOT, ROOT / "host", ROOT / "contest", ROOT / "lua", ROOT / "tools",
                ROOT / "contest/timing_profiles"):
            for extension in ("py", "md", "lua", "json"):
                paths.update(folder.glob(f"*.{extension}"))
        for extension in ("py", "md", "json"):
            paths.update((ROOT / "tests").rglob(f"*.{extension}"))
        count = 0
        for path in sorted(paths):
            data = path.read_bytes()
            if b"\r" in data:
                raise RuntimeError(f"Use LF-only source endings: {path.relative_to(ROOT)}")
            if path.suffix == ".py":
                ast.parse(data.decode("utf-8"), filename=str(path))
                count += 1
        self.result["python_sources"] = count
        self.log(f"Python syntax and source/Markdown LF endings OK: {count} Python files.")

    def inspect_archive(self, archive_path):
        with ZipFile(archive_path) as archive:
            names = archive.namelist()
            if archive.testzip() is not None or len(names) != len(set(names)):
                raise RuntimeError("Release ZIP failed its internal validation.")
            if {PurePosixPath(name).parts[0] for name in names} != {"run_contest.py", "contest"}:
                raise RuntimeError("Release extraction must contain exactly run_contest.py and contest/.")
            documents = {"contest/" + name for name in ("readme.md", "ALGORITHM.md", "buyable-poffin-guide.md")}
            if not documents.issubset(names):
                raise RuntimeError("Release ZIP is missing its current Markdown documentation.")
            if not RUNTIME_MEMBERS.issubset(names):
                raise RuntimeError("Release ZIP is missing a retained main-runner dependency.")
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name:
                    raise RuntimeError("Release ZIP contains an invalid member path.")
                if path.name in {"requirements.txt", "readme.txt"}:
                    raise RuntimeError(f"Release ZIP contains an obsolete file: {name}")
                if path.suffix in {".py", ".lua", ".json"} and name not in RUNTIME_MEMBERS \
                        and name not in {"run_contest.py", "contest/wheels/manifest.json"}:
                    raise RuntimeError(f"Release ZIP contains code or timing data outside the main runtime: {name}")
                if any(part in {"states", "evidence", ".venv", "tmp", ".tmp", "tests", "__pycache__"} for part in path.parts):
                    raise RuntimeError(f"Release ZIP contains private/generated data: {name}")
                if path.suffix.lower() in {".exe", ".dll", ".nds", ".dsv", ".sav", ".png", ".pyc"} or re.search(r"\.ds[0-9]$", name, re.I):
                    raise RuntimeError(f"Release ZIP contains an excluded binary: {name}")
                if path.suffix in {".py", ".lua", ".json", ".md"}:
                    data = archive.read(name)
                    if b"\r" in data:
                        raise RuntimeError(f"Release source endings are not LF-only: {name}")
                    if path.suffix == ".py":
                        ast.parse(data.decode("utf-8"), filename=name)
                if path.suffix == ".whl":
                    if path.parent.as_posix() != "contest/wheels":
                        raise RuntimeError("Dependency wheels belong inside contest/wheels/.")
                    with ZipFile(BytesIO(archive.read(name))) as wheel:
                        if wheel.testzip() is not None:
                            raise RuntimeError(f"A bundled dependency wheel is unreadable: {path.name}")
            tree = ast.parse(archive.read("run_contest.py").decode("utf-8"))
            values = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in {"DESMUME_PATH", "ROM_PATH", "RELEASE"}}
            if values.get("DESMUME_PATH") != "" or values.get("ROM_PATH") != "":
                raise RuntimeError("Release launcher must start with empty emulator/ROM selections.")
            self.result.update(release=values["RELEASE"], archive_members=len(names))
            archive.extractall(self.bundle)
        manifest = self.wheel_bundle(self.bundle / "contest")
        self.result["bundled_python_versions"] = manifest["python_versions"]
        self.result["bundled_wheels"] = manifest["wheels"]
        self.log(f"Release ZIP OK: {len(names)} members; two root items; private assets excluded; dependency wheels included.")

    def prepare_inputs(self):
        fixture = self.work / "synthetic-inputs"
        fixture.mkdir()
        program, rom = fixture / "program.exe", fixture / "fixture.nds"
        program.write_bytes(b"synthetic program; check mode must never execute this file")
        header = bytearray(0x160)
        header[12:16] = b"CPUJ"
        rom.write_bytes(header)
        battery = fixture / "Battery"
        battery.mkdir()
        (battery / "fixture.dsv").write_bytes(b"synthetic battery fixture")
        shutil.copy2(self.lua_dll, fixture / "lua51.dll")
        code = "\n".join(("import sys; from pathlib import Path",
            f"sys.path.insert(0, {str(self.bundle)!r})", "import run_contest",
            f"run_contest.remember_paths(program=Path({str(program)!r}), rom=Path({str(rom)!r}))"))
        self.command([sys.executable, "-I", "-B", "-c", code], cwd=self.bundle)
        self.rom = rom
        self.launcher = self.bundle / "run_contest.py"
        self.python = self.bundle / "contest/.venv/Scripts/python.exe"

    def launcher_check(self):
        text = self.command([sys.executable, "-I", "-B", self.launcher, "--check"], cwd=self.bundle)
        if "Installation OK" not in text or "Downloading" in text or "Looking in indexes:" in text:
            raise RuntimeError("The packaged launcher did not complete its offline installation check.")
        return text

    def environment_checks(self):
        self.prepare_inputs()
        if "Local environment ready." not in self.launcher_check():
            raise RuntimeError("Fresh launch did not create its local environment.")
        if "Preparing the local" in self.launcher_check():
            raise RuntimeError("Healthy launch unexpectedly ran setup again.")
        code = "import sys; from pathlib import Path; from PIL import Image; assert Path(Image.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()); print('Isolated Pillow:', Image.__version__)"
        self.command([self.python, "-I", "-B", "-c", code], cwd=self.bundle)
        self.command([self.python, "-B", "-m", "pip", "uninstall", "--yes", "Pillow"], cwd=self.bundle)
        if "Local environment ready." not in self.launcher_check():
            raise RuntimeError("Missing dependency was not repaired from the bundled wheels.")
        partial = self.work / "partial-environment"
        partial.mkdir()
        self.command([self.python, "-B", "-m", "venv", "--without-pip", partial / ".venv"])
        code = "\n".join(("import sys", f"sys.path.insert(0, {str(self.bundle)!r})", "import run_contest",
            "from host.bootstrap import ensure_environment",
            f"ensure_environment({str(partial)!r}, wheel_directory={str(self.bundle / 'contest/wheels')!r}, output=run_contest.safe_print)"))
        text = self.command([self.python, "-I", "-B", "-c", code], cwd=self.bundle)
        if "Repairing the local package installer" not in text or "Local environment ready." not in text:
            raise RuntimeError("Interrupted environment creation did not recover offline.")
        header = bytearray(self.rom.read_bytes())
        header[12:16] = b"CPUE"
        self.rom.write_bytes(header)
        if "accepted as CPUE" not in self.launcher_check():
            raise RuntimeError("The packaged installation check did not accept the English header.")

    def module_checks(self, directory):
        for name in CHECK_MODULES:
            self.command([self.python, "-I", "-B", directory / "contest" / f"{name}.py",
                "--check", "--lua-dll", self.lua_dll], cwd=directory)

    def offline_suite(self):
        code = "\n".join(("import sys", f"sys.path.insert(0, {str(ROOT)!r})",
            "from tests.__main__ import main", "raise SystemExit(main([]))"))
        text = self.command([self.python, "-I", "-B", "-c", code])
        match = re.search(r"Ran (\d+) tests in", text)
        if not match or int(match.group(1)) == 0:
            raise RuntimeError("The complete offline suite did not publish a nonempty test result.")
        self.result["offline_tests"] = int(match.group(1))

    def finish(self, outcome):
        self.result["outcome"] = outcome
        Path(self.result["report"]).write_text(json.dumps(self.result, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        self.log(f"Release checks {outcome}: {self.result['report']}")
        return self.result


def verify_release(archive=None, *, lua_dll=None, output=safe_output):
    checks = ReleaseChecks(lua_dll=lua_dll, output=output)
    try:
        checks.run_stage("prerequisites", checks.prerequisites)
        checks.run_stage("syntax and LF endings", checks.syntax_and_endings)
        if archive is None:
            from tools.package_contest import stage_release, write_archive

            staged = stage_release(checks.work / "staged")
            archive = checks.work / "candidate.zip"
            write_archive(staged, archive)
        checks.run_stage("release archive", lambda: checks.inspect_archive(Path(archive)))
        checks.run_stage("offline setup, reuse, and repair", checks.environment_checks)
        checks.run_stage("complete offline suite with bundled Pillow", checks.offline_suite)
        checks.run_stage("main source check command", lambda: checks.module_checks(ROOT))
        checks.run_stage("main packaged check command", lambda: checks.module_checks(checks.bundle))
        from tests.release.windows_ui import verify_windowless

        checks.run_stage("native windowless errors and progress", lambda: verify_windowless(checks))
        return checks.finish("pass")
    except BaseException as error:
        checks.result.update(failed_stage=checks.stage, error=clean_diagnostic(error))
        checks.finish("fail")
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise RuntimeError(f"Release verification failed at {checks.stage}: {clean_diagnostic(error)}. Report: {checks.result['report']}") from error
