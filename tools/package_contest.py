"""Stage the two-item release layout, or create its ZIP at release time."""

import argparse
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
CODE_SUFFIXES = {".py", ".lua", ".json"}


def release_readme():
    source = (ROOT / "README.md").read_text(encoding="utf-8")
    sections = [source.splitlines()[0]]
    for heading in ("Setup and ownership", "Release layout", "Attribution"):
        marker = f"\n## {heading}\n"
        if marker not in source:
            raise ValueError(f"Keep the {heading} section in README.md for release guidance.")
        body = source.split(marker, 1)[1].split("\n## ", 1)[0].strip()
        sections.append(f"## {heading}\n\n{body}")
    return ("\n\n".join(sections) + "\n").replace("](run_contest.py)", "](../run_contest.py)").replace(
        "](contest/README.md", "](DESIGN.md")


def stage_release(destination):
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

    for folder in ("contest", "host", "lua"):
        for source in sorted((ROOT / folder).iterdir()):
            if source.is_file() and source.suffix in CODE_SUFFIXES:
                copy(source)
    for source in sorted((ROOT / "contest/timing_profiles").glob("*.json")):
        copy(source)
    copy(ROOT / "contest" / "debug" / ".gitkeep")
    for folder, pattern in (("contest/states", "*.ds*"), ("contest/evidence", "*.png")):
        for source in sorted((ROOT / folder).glob(pattern)):
            if source.is_file():
                copy(source)
    copy(ROOT / "requirements.txt")
    copy(ROOT / "lua" / "PLATINUM-LICENSE.txt")
    for name in ("region-memory-results.txt", "scarf-test-results.txt"):
        copy(ROOT / "contest" / name)
    design = (ROOT / "contest" / "README.md").read_text(encoding="utf-8")
    (support / "DESIGN.md").write_text(design.replace("](../README.md", "](README.md"), encoding="utf-8", newline="\n")
    (support / "README.md").write_text(release_readme(), encoding="utf-8", newline="\n")
    return destination


def build_zip(output):
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT) or output.suffix.lower() != ".zip":
        raise ValueError("Use a project-local .zip output path.")
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="contest-release-", dir=scratch) as directory:
        staged = stage_release(Path(directory) / "bundle")
        output.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(staged.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staged).as_posix())
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stage", type=Path, help="Write a fresh review folder inside tmp/ or .tmp/.")
    mode.add_argument("--zip", type=Path, help="Create a new release ZIP inside the project.")
    args = parser.parse_args()
    try:
        requested = args.stage if args.stage is not None else args.zip
        path = requested if requested.is_absolute() else ROOT / requested
        result = stage_release(path) if args.stage is not None else build_zip(path)
        print(result)
        return 0
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
