"""Stage the two-item release layout, or create its ZIP at release time."""

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from contest.checks import RUNTIME_SOURCES


def release_readme():
    return (ROOT / "readme.md").read_text(encoding="utf-8")


def stage_release(destination):
    from host.bootstrap import bundled_wheels

    wheels = bundled_wheels(ROOT)
    destination = Path(destination).resolve()
    if not any(destination.is_relative_to((ROOT / name).resolve()) for name in ("tmp", ".tmp")):
        raise ValueError("Stage releases inside project tmp/ or .tmp/.")
    destination.mkdir(parents=True, exist_ok=False)
    support = destination / "contest"
    support.mkdir()

    launcher = (ROOT / "run_contest.py").read_text(encoding="utf-8")
    for name in ("DESMUME_PATH", "ROM_PATH"):
        launcher, count = re.subn(rf"^{name}\s*=.*$", name + ' = ""', launcher, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"Keep {name} on one line in run_contest.py.")
    (destination / "run_contest.py").write_text(launcher, encoding="utf-8", newline="\n")

    def copy(source):
        relative = source.relative_to(ROOT)
        if relative.parts[0] == "contest":
            relative = Path(*relative.parts[1:])
        target = support / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    for folder, names in RUNTIME_SOURCES.items():
        for name in names:
            copy(ROOT / folder / name)
    copy(ROOT / "contest" / "debug" / ".gitkeep")
    copy(wheels / "manifest.json")
    for name in json.loads((wheels / "manifest.json").read_text(encoding="utf-8"))["wheels"]:
        copy(wheels / name)
    algorithm = (ROOT / "ALGORITHM.md").read_text(encoding="utf-8")
    algorithm = algorithm.replace("](run_contest.py)", "](../run_contest.py)").replace("](contest/", "](")
    (support / "ALGORITHM.md").write_text(algorithm, encoding="utf-8", newline="\n")
    (support / "readme.md").write_text(release_readme(), encoding="utf-8", newline="\n")
    copy(ROOT / "buyable-poffin-guide.md")
    return destination


def write_archive(staged, output):
    """Write a candidate archive for the release gate or its local review input."""
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(Path(staged).rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staged).as_posix())


def build_zip(output, *, lua_dll=None):
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT) or output.suffix.lower() != ".zip":
        raise ValueError("Use a project-local .zip output path.")
    if output.exists():
        raise FileExistsError(f"Use a fresh release ZIP path: {output}")
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="contest-release-", dir=scratch) as directory:
        staged = stage_release(Path(directory) / "bundle")
        candidate = Path(directory) / "candidate.zip"
        write_archive(staged, candidate)
        from tests.release import verify_release

        verification = verify_release(candidate, lua_dll=lua_dll)
        if verification.get("outcome") != "pass":
            raise RuntimeError("Release verification failed; the requested ZIP was not created.")
        output.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with output.open("xb") as destination, candidate.open("rb") as source:
                created = True
                shutil.copyfileobj(source, destination)
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stage", type=Path, help="Write a fresh review folder inside tmp/ or .tmp/.")
    mode.add_argument("--zip", type=Path, help="Create a new release ZIP inside the project.")
    parser.add_argument("--lua-dll", type=Path, help="Use this matching local Lua DLL for the required release gate.")
    args = parser.parse_args()
    try:
        requested = args.stage if args.stage is not None else args.zip
        path = requested if requested.is_absolute() else ROOT / requested
        if args.stage is not None and args.lua_dll is not None:
            parser.error("Use --lua-dll with --zip.")
        result = stage_release(path) if args.stage is not None else build_zip(path, lua_dll=args.lua_dll)
        print(result)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
