"""Recorded contest transient ownership and cleanup, independent of emulator controls."""

import json
from pathlib import Path
import re

from host.checkpoints import write_json
from host.paths import ROOT


PURPOSE = "contest_runner_owned_transients"
CONFIG_NAMES = {"attach.lua", "handoff-config.lua", "section-config.lua", "prep-plan.lua", "plan.lua", "endpoint.lua", "review.lua",
    "response-plan.lua", "rank-dance-start-context.lua", "rank-dance-offset-context.lua", "rank-dance-finish-context.lua",
    "pointer-calibration-start.lua", "midpoint-adjustment.lua"}
CAPTURE_NAMES = {"source.png", "source-stage4.gd", "failure.png", "failure.gd", "stopped.png", "non-excellent.gd",
                 "boundary.gd", "start.png", "finish.png", "offset-check.png", "midpoint.gd", "midpoint.png", "response-end.gd",
                 "pointer-calibration-start.gd", "feedback-before.gd"}
SEQUENCE_NAMES = {"demo-frames.bin", "frames.bin", "rest-frames.bin", "demo-frames.jsonl", "frames.jsonl", "rest-frames.jsonl",
                  "observation-frames.jsonl"}
ATOMIC_NAMES = {"runner-result.json.tmp", "runner-status.json.tmp", "runner-setup.json.tmp", "runner-progress.json.tmp",
                "review-request.json.tmp", "dress-pages.json.tmp", "checkpoint-progress.json.tmp"}
METADATA_NAMES = {"runner-result.json", "runner-setup.json", "runner-progress.json", "source-snapshot.json", "source-handoff.json",
    "source-checkpoint.json", "review-request.json", "review.json", "dress-pages.json", "endpoint-reading.json", "dance-handoff.json",
    "acting-handoff.json", "dance-progress.json", "initial-navigation.json", "render-preparation.json", "collection-context.json",
    "overworld-reference.json", "retry-context.json", "resolved-plan.json", "config.json", "judgments.json", "non-excellent.json",
    "attempt-result.json", "input-result.json", "stylus-result.json", "lead-dances-result.json", "transition-result.json",
    "midpoint.json", "midpoint-cues.json", "response-plan.json", "demo-reading.json", "dance-result.json",
    "pointer-calibration.json", "pointer-calibration-start.json", "pointer-calibration-end.json", "pointer-calibration-inputs.json",
    "midpoint-adjustments.json"}


def transient_kind(path):
    name = path.name
    if name in CONFIG_NAMES or re.fullmatch(r"retry-slot-(?:2|5|10)\.lua", name):
        return "generated_configuration"
    if name in SEQUENCE_NAMES:
        return "image_sequence"
    if name in ATOMIC_NAMES:
        return "atomic_write_scratch"
    if name in CAPTURE_NAMES or re.fullmatch(
            r"(?:stage[1-6]|dress-page-\d+|round-[1-4](?:-judges)?|result|judgment-\d+-[A-Za-z]+|feedback-before-\d+|feedback-recovery-\d+-\d+|sample-\d+|checkpoint-\d+|midpoint-adjustment-\d+)\.(?:png|gd)", name):
        return "capture"
    if re.fullmatch(r"(?:timing|enrollment)-contact-sheet\.png", name):
        return "contact_sheet"
    return None


def scratch_root(path):
    path = Path(path).resolve()
    if not any(path.is_relative_to((ROOT / name).resolve()) for name in ("tmp", ".tmp")):
        raise RuntimeError("Contest artifact ownership must stay inside project scratch.")
    return path


def runner_directory(parts):
    if not parts:
        return True
    if len(parts) == 1:
        return parts[0] in {"samples", "checkpoints"} or re.fullmatch(r"(?:dance|attempt)-\d+", parts[0]) is not None
    return (len(parts) in (2, 3) and parts[0] == "visits" and re.fullmatch(r"s[1-6]-\d+", parts[1]) is not None
        and (len(parts) == 2 or parts[2] in {"samples", "checkpoints"} or re.fullmatch(r"dance-\d+", parts[2]) is not None))


class OwnedArtifacts:
    def __init__(self, work, run_id, host_pid, *, existing=None):
        self.work = scratch_root(work)
        self.path = self.work / "owned-artifacts.json"
        self.data = existing or {"schema": 1, "purpose": PURPOSE, "run_id": run_id, "host_pid": host_pid, "files": {}}
        if (self.data.get("schema") != 1 or self.data.get("purpose") != PURPOSE
                or self.data.get("run_id") != run_id or not isinstance(self.data.get("files"), dict)):
            raise RuntimeError("Invalid contest artifact ownership record.")
        if any(not isinstance(item, dict) or not isinstance(item.get("kind"), str) for item in self.data["files"].values()):
            raise RuntimeError("Invalid contest transient file records.")
        self.save()

    def save(self):
        write_json(self.path, self.data)

    def owned_path(self, path):
        path = Path(path)
        path = path if path.is_absolute() else self.work / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.work) or resolved == self.work:
            raise RuntimeError("The artifact path escaped its owned run folder.")
        return resolved

    def register(self, path, *, kind="scratch", protected=False, save=True):
        path = self.owned_path(path)
        key = path.relative_to(self.work).as_posix()
        previous = self.data["files"].get(key, {})
        self.data["files"][key] = {"kind": kind, "protected": protected or previous.get("protected", False),
            "removed": False, "bytes": path.stat().st_size if path.is_file() else None}
        if save:
            self.save()

    def register_tree(self, folder):
        folder = Path(folder).resolve()
        if not folder.is_relative_to(self.work):
            raise RuntimeError("Only this runner's fresh stage folders can claim generated artifacts.")
        for path in folder.rglob("*"):
            relative = path.relative_to(folder)
            if not runner_directory(relative.parts[:-1]):
                continue
            if path.is_file() and (kind := transient_kind(path)):
                self.register(path, kind=kind, save=False)
        self.save()

    def unprotect(self, path):
        key = self.owned_path(path).relative_to(self.work).as_posix()
        if key in self.data["files"]:
            self.data["files"][key]["protected"] = False
            self.save()

    def cleanup(self, name, *, protected=(), check=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Use a plain cleanup manifest name.")
        directory = self.work / "cleanup"
        directory.mkdir(exist_ok=True)
        manifest = directory / f"{name}.json"
        if manifest.exists():
            raise FileExistsError("Cleanup manifests must be unique.")
        protected = {Path(path).resolve() for path in protected}
        record = {"schema": 1, "run_id": self.data["run_id"], "hook": name, "status": "running", "removed": [],
            "bytes_removed": 0, "protected_files": 0, "reference_policy": "durable metadata marks removed artifacts through this manifest"}
        try:
            for key, item in self.data["files"].items():
                if check:
                    check()
                if item.get("removed"):
                    continue
                path = self.owned_path(key)
                if item.get("protected") or path in protected:
                    record["protected_files"] += 1
                    continue
                if path.is_file():
                    size = path.stat().st_size
                    path.unlink()
                    record["removed"].append({"path": str(path), "kind": item["kind"], "bytes": size})
                    record["bytes_removed"] += size
                elif path.exists():
                    raise RuntimeError("An owned transient file was replaced by a directory.")
                item.update(removed=True, cleanup_manifest=str(manifest))
            record["status"] = "complete"
        except BaseException as error:
            record.update(status="interrupted", error=str(error) or type(error).__name__)
            raise
        finally:
            write_json(manifest, record)
            self.save()
            self.mark_references(record["removed"], manifest)
        return manifest

    def mark_references(self, removed, manifest):
        paths = {str(self.owned_path(key)): item["cleanup_manifest"] for key, item in self.data["files"].items()
                 if item.get("removed") and item.get("cleanup_manifest")}
        paths.update({str(Path(item["path"]).resolve()): str(manifest) for item in removed})
        if not paths:
            return

        def mark(value):
            changed = False
            if isinstance(value, dict):
                for key, item in list(value.items()):
                    if key == "artifact_retention":
                        continue
                    if isinstance(item, str) and ("/" in item or "\\" in item):
                        try:
                            removed_manifest = paths.get(str(Path(item).resolve()))
                        except (OSError, ValueError):
                            removed_manifest = None
                        if removed_manifest:
                            value.setdefault("artifact_retention", {})[key] = {"retained": False, "manifest": removed_manifest}
                            if "retained" in value and Path(item).suffix.lower() in {".png", ".gd"}:
                                value.update(retained=False, cleanup_manifest=removed_manifest)
                            changed = True
                    changed = mark(item) or changed
            elif isinstance(value, list):
                for item in value:
                    changed = mark(item) or changed
            return changed

        for path in self.work.rglob("*.json"):
            if path == self.path or path.parent == self.work / "cleanup":
                continue
            if not runner_directory(path.relative_to(self.work).parts[:-1]) or not path.resolve().is_relative_to(self.work):
                continue
            if (path.name not in METADATA_NAMES and not re.fullmatch(r"round-[1-4]\.json", path.name)
                    and not (path.parent.name == "checkpoints" and re.fullmatch(r"\d+-[A-Za-z0-9_-]+\.json", path.name))):
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            changed = mark(value)
            if path.name == "runner-result.json" and isinstance(value, dict):
                value["transient_cleanup"] = {"manifest": str(manifest), "consumed_images_retained": False}
                changed = True
            if changed:
                write_json(path, value)


def inactive_runs(parent, *, current_pid, check=None, process_alive=None):
    """Read ownership/liveness without touching the emulator or deleting files."""
    parent = scratch_root(parent)
    if process_alive is None:
        from host.windows import process_alive

    candidates, live = [], {}
    for folder in parent.iterdir():
        if check:
            check()
        if not folder.is_dir() or folder.is_symlink() or not folder.resolve().is_relative_to(parent):
            continue
        ledger = folder / "owned-artifacts.json"
        setup_path = folder / "runner-setup.json"
        if ledger.is_file():
            data = json.loads(ledger.read_text(encoding="utf-8"))
            if data.get("purpose") != PURPOSE or data.get("schema") != 1:
                continue
            run_id, pid = data.get("run_id"), data.get("host_pid")
            setup = None
        elif setup_path.is_file():
            setup = json.loads(setup_path.read_text(encoding="utf-8"))
            run_id, pid = setup.get("run_id"), setup.get("host_pid")
            declared = setup.get("work_dir", setup.get("work"))
            if (run_id != folder.name or declared is None or Path(declared).resolve() != folder.resolve()
                    or not (setup.get("stage") in range(1, 7) or setup.get("settings", {}).get("game") == "CPUJ")):
                continue
            data = None
        else:
            continue
        if not isinstance(run_id, str) or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            continue
        if pid == current_pid:
            continue
        if pid not in live:
            live[pid] = process_alive(pid)
        if live[pid]:
            raise RuntimeError(f"An earlier contest host caller is still active (PID {pid}).")
        candidates.append((folder, run_id, pid, data, setup))
    return candidates


def cleanup_stale(parent, *, current_pid, protected=(), check=None, process_alive=None, candidates=None):
    """Delete recorded inactive scratch after the master has settled initial ownership."""
    parent = scratch_root(parent)
    if candidates is None:
        candidates = inactive_runs(parent, current_pid=current_pid, check=check, process_alive=process_alive)
    result = {"status": "complete", "hook": "fresh_run_startup", "inactive_runs": [], "removed_files": 0, "bytes_removed": 0}
    for folder, run_id, pid, data, setup in candidates:
        if check:
            check()
        artifacts = OwnedArtifacts(folder, run_id, pid, existing=data)
        # A terminated host may not have indexed its last Lua-emitted captures yet.
        artifacts.register_tree(folder)
        terminal = folder / "runner-result.json"
        outcome = json.loads(terminal.read_text(encoding="utf-8")) if terminal.is_file() else None
        if outcome and (outcome.get("temporary_slot_9_restored") is True
                or outcome.get("temporary_slot_10_restored") is True) and not outcome.get("cleanup_errors"):
            for item in artifacts.data["files"].values():
                if item["kind"] == "slot_backup":
                    item["protected"] = False
            artifacts.save()
        if data is None:
            if outcome:
                if not outcome.get("cleanup_errors") and not outcome.get("cleanup", {}).get("errors"):
                    for path in folder.glob("*-slot-*.ds*"):
                        if re.fullmatch(r"(?:previous|original)-slot-\d+\.ds\d", path.name):
                            artifacts.register(path, kind="obsolete_slot_backup", save=False)
                    artifacts.save()
        count = len(tuple((folder / "cleanup").glob("startup-*.json"))) if (folder / "cleanup").is_dir() else 0
        manifest = artifacts.cleanup(f"startup-{count + 1:03d}", protected=protected, check=check)
        facts = json.loads(manifest.read_text(encoding="utf-8"))
        result["inactive_runs"].append({"run_id": run_id, "manifest": str(manifest), "removed_files": len(facts["removed"])})
        result["removed_files"] += len(facts["removed"])
        result["bytes_removed"] += facts["bytes_removed"]
    return result
