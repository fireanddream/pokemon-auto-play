"""Main-run folder cleanup; the current run and private assets remain untouched."""

from pathlib import Path
import shutil

from host.checkpoints import write_json
from host.paths import ROOT


def cleanup_run_folders(work, *, check=None):
    """Remove older folders in the main runner's disposable namespace after Lua is ready."""
    parent = ROOT.resolve() / "tmp" / "contest"
    work = Path(work).absolute()
    if (parent.resolve() != parent or work.parent != parent or work.resolve() != work
            or not work.is_dir()):
        raise RuntimeError("Run-folder cleanup requires the current folder inside project tmp/contest.")
    record = {"status": "running", "hook": "after_emulator_and_lua_ready", "removed_folders": []}
    try:
        for folder in sorted(parent.iterdir()):
            if check:
                check()
            if folder == work or not folder.is_dir() or folder.is_symlink() or folder.resolve() != folder:
                continue
            shutil.rmtree(folder)
            record["removed_folders"].append(folder.name)
        if check:
            check()
        record["status"] = "complete"
    except BaseException as error:
        record.update(status="interrupted", error=str(error) or type(error).__name__)
        raise
    finally:
        write_json(work / "startup-cleanup.json", record)
    return record
