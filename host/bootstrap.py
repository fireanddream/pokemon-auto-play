"""Create and repair the caller's isolated, project-local Python environment."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig


DEPENDENCIES = ("Pillow",)


def dependency_requirements():
    """Canonical package-list text for bundled manifests and environment markers."""
    return "\n".join(DEPENDENCIES) + "\n"


class EnvironmentBusy(RuntimeError):
    """Another launcher is preparing this same local environment."""


@contextmanager
def setup_lock(scratch):
    path = Path(scratch) / "environment-setup.lock"
    with path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise EnvironmentBusy("Another launcher is preparing the local environment.") from error
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _probe(python, code, environment, directory):
    if not python.is_file():
        return False
    try:
        result = subprocess.run([str(python), "-I", "-B", "-c", code, str(directory)],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError:
        return False
    return result.returncode == 0


def _run_command(arguments, environment, output, label):
    with subprocess.Popen([str(value) for value in arguments], env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as child:
        try:
            for line in child.stdout:
                output(line.rstrip("\r\n"))
            code = child.wait()
        except BaseException:
            child.terminate()
            child.wait()
            raise
    if code:
        raise RuntimeError(f"{label} failed (exit code {code}). Check the startup log and try again after resolving the error.")


def _marker_matches(path, requirements):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("requirements") == requirements


def bundled_wheels(root, *, wheel_directory=None):
    """Require the prepared local wheel bundle, never an online package source."""
    root = Path(root).resolve()
    directory = root / "wheels" if wheel_directory is None else Path(wheel_directory).resolve()
    manifest = directory / "manifest.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(2, "Bundled packages are missing. Extract the complete release ZIP; source checkouts use tools/prepare_dependencies.py.", str(manifest)) from error
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("The bundled package manifest is unreadable. Extract a fresh complete release ZIP.") from error
    requirements = dependency_requirements()
    if not isinstance(value, dict) or value.get("requirements") != requirements:
        raise RuntimeError("The bundled packages do not match the declared dependencies. Prepare an updated wheel bundle before creating the release.")
    names = value.get("wheels")
    if (value.get("platform") != "win_amd64" or not isinstance(names, list) or not names
            or any(not isinstance(name, str) or Path(name).name != name or not name.endswith(".whl") for name in names)):
        raise RuntimeError("The bundled package manifest is invalid. Extract a fresh complete release ZIP.")
    for name in names:
        if not (directory / name).is_file():
            raise FileNotFoundError(2, "A bundled dependency wheel is missing. Extract the complete release ZIP.", str(directory / name))
    return directory


def ensure_environment(root, *, quiet=False, output=print, wheel_directory=None):
    """Return a usable .venv; successful later launches do not run pip again."""
    root = Path(root).resolve()
    requirements = dependency_requirements()
    wheels = bundled_wheels(root, wheel_directory=wheel_directory)
    manifest = json.loads((wheels / "manifest.json").read_text(encoding="utf-8"))
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if (sys.implementation.name != "cpython" or sys.maxsize <= 2 ** 32
            or sysconfig.get_config_var("Py_GIL_DISABLED") or version not in manifest.get("python_versions", [])):
        raise RuntimeError("Use a standard Windows x64 CPython build supported by the bundled packages: "
            + ", ".join(manifest.get("python_versions", [])) + ".")
    directory = root / ".venv"
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = directory / "environment-ready.json"
    scratch = root / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    interpreter_check = ("import sys; from pathlib import Path; "
        "assert sys.version_info >= (3, 10); "
        "assert Path(sys.prefix).resolve() == Path(sys.argv[1]).resolve()")
    dependency_check = ("import sys; from pathlib import Path; from PIL import Image; "
        "assert Path(Image.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())")
    installer_check = ("import sys; from pathlib import Path; import pip; "
        "assert Path(pip.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())")

    with setup_lock(scratch):
        compatible = _probe(python, interpreter_check, environment, directory)
        dependencies_ready = compatible and _probe(python, dependency_check, environment, directory)
        if dependencies_ready and _marker_matches(marker, requirements):
            return directory

        if os.name == "nt":
            from .windows import setup_console

            progress = setup_console(quiet)
        else:
            progress = nullcontext()
        with progress:
            output("Preparing the local Python environment. Please wait...")
            marker.unlink(missing_ok=True)
            if not compatible:
                base = getattr(sys, "_base_executable", None) or sys.executable
                output("Creating .venv...")
                _run_command([base, "-B", "-m", "venv", directory], environment, output, "Environment creation")
            if not _probe(python, installer_check, environment, directory):
                output("Repairing the local package installer...")
                _run_command([python, "-B", "-m", "ensurepip", "--upgrade"],
                    environment, output, "Installer repair")
            output("Installing bundled project requirements (offline)...")
            arguments = [python, "-B", "-m", "pip", "install", "--no-index", "--find-links", wheels,
                "--disable-pip-version-check", "--no-cache-dir"]
            if compatible and not dependencies_ready:
                arguments.append("--force-reinstall")
            arguments.extend(requirements.splitlines())
            _run_command(arguments, environment, output, "Dependency installation")
            if not (_probe(python, interpreter_check, environment, directory)
                    and _probe(python, dependency_check, environment, directory)):
                raise RuntimeError("The local environment could not load its dependencies after setup. Check the startup log and try again.")
            temporary = marker.with_suffix(".tmp")
            try:
                temporary.write_text(json.dumps({"requirements": requirements}, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
                temporary.replace(marker)
            finally:
                temporary.unlink(missing_ok=True)
            output("Local environment ready.")
    return directory
