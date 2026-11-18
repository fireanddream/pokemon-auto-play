"""Explicit maintainer preparation of the Windows x64 wheel bundle for releases."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
from zipfile import BadZipFile, ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host.bootstrap import _run_command, dependency_requirements
from run_contest import clean_diagnostic


PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14", "3.15")


def check_wheel_coverage(names):
    for version in PYTHON_VERSIONS:
        tag = "cp" + version.replace(".", "")
        if not any(name.lower().startswith("pillow-") and name.endswith(f"-{tag}-{tag}-win_amd64.whl") for name in names):
            raise RuntimeError(f"The wheel payload is missing a standard Windows x64 Pillow wheel for CPython {version}. Complete wheel preparation first.")


def output(text):
    text = clean_diagnostic(text)
    if sys.stdout is None:
        return
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, errors="backslashreplace").decode(encoding), flush=True)


def prepare_dependencies(*, cached=False):
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    destination = ROOT / "wheels"
    requirements = dependency_requirements()
    work = scratch / "dependency-preparation"
    previous = work / "dependencies.json"
    retained = previous if previous.is_file() else destination / "manifest.json"
    if (work.exists() and retained.is_file()
            and json.loads(retained.read_text(encoding="utf-8")).get("requirements") != requirements):
        work.rename(work.with_name("dependency-preparation-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
    work.mkdir(exist_ok=True)
    previous.write_text(json.dumps({"requirements": requirements}, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    environment = {**os.environ, "TMP": str(work), "TEMP": str(work), "TMPDIR": str(work),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    python = work / "installer/Scripts/python.exe"
    base = getattr(sys, "_base_executable", None) or sys.executable
    if not cached and not python.is_file():
        _run_command([base, "-B", "-m", "venv", python.parent.parent], environment, output, "Installer preparation")
    wheels = work / "wheels"
    wheels.mkdir(exist_ok=True)
    for version in (() if cached else PYTHON_VERSIONS):
        output(f"Preparing Windows x64 dependency wheels for CPython {version}...")
        _run_command([python, "-B", "-m", "pip", "download", "--disable-pip-version-check",
            "--no-cache-dir", "--retries", "0", "--only-binary", ":all:", "--platform", "win_amd64",
            "--implementation", "cp", "--abi", "cp" + version.replace(".", ""),
            "--python-version", version, "--dest", wheels, *requirements.splitlines()],
            environment, output, "Wheel preparation")
    sources = sorted(wheels.glob("*.whl"))
    if not sources:
        raise RuntimeError("Wheel preparation produced no dependency files.")
    check_wheel_coverage([path.name for path in sources])
    for path in sources:
        try:
            with ZipFile(path) as archive:
                if archive.testzip() is not None:
                    raise BadZipFile("Wheel archive validation failed.")
        except BadZipFile as error:
            raise RuntimeError(f"The dependency wheel is unreadable: {path.name}") from error
    destination.mkdir(exist_ok=True)
    for source in sources:
        target = destination / source.name
        if target.exists():
            if target.read_bytes() != source.read_bytes():
                raise RuntimeError(f"A different local wheel already uses this filename: {source.name}. Review it before replacing it.")
        else:
            shutil.copy2(source, target)
    manifest = {"requirements": requirements, "platform": "win_amd64", "implementation": "cp",
        "python_versions": list(PYTHON_VERSIONS), "wheels": [path.name for path in sources]}
    temporary = destination / "manifest.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(destination / "manifest.json")
    output(f"Prepared {len(sources)} bundled wheels in {destination}. Startup and release checks use these files offline.")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cached", action="store_true", help="Assemble the complete retained wheel cache without network access.")
    args = parser.parse_args()
    try:
        prepare_dependencies(cached=args.cached)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(clean_diagnostic(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
