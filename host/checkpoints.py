"""Game-independent paused checkpoint transport and retained evidence receipts."""

from __future__ import annotations

import filecmp
import json
from pathlib import Path
import shutil

from .savestates import StateSlots


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class CheckpointStopped(RuntimeError):
    def __init__(self, record: dict):
        self.record = record
        super().__init__(f"{record.get('checkpoint', 'checkpoint')}: {record.get('error', 'checkpoint did not authorize progress')}")


class CheckpointClient:
    """One host caller acknowledges evidence; Lua owns predicates and branch decisions."""

    def __init__(self, emulator, work: Path, run_id: str):
        self.emulator, self.controller = emulator, emulator.controller
        self.work, self.run_id = work, run_id
        self.directory = work / "checkpoints"

    def command(self, action: str, *arguments: object) -> dict:
        return self.controller.checkpoint_command(action, [str(value) for value in arguments])

    def _retain(self, slot: int, destination: Path, expected: dict) -> dict:
        if destination.exists():
            raise RuntimeError(f"Checkpoint evidence already exists: {destination}")
        saved = self.emulator.states.save(slot, expected=expected, timeout=None)
        source = Path(saved["path"])
        shutil.copy2(source, destination)
        StateSlots.validate_header(destination, slot)
        if not filecmp.cmp(source, destination, shallow=False):
            raise RuntimeError("The retained state differs from the native slot save.")
        if not self.controller.paused(self.controller.session()):
            raise RuntimeError("Native pause was lost while retaining checkpoint evidence.")
        return {**saved, "retained_path": str(destination)}

    def acknowledge(self, response: dict, *, slot: int | None = None, previous_state: str | None = None) -> dict:
        """Acknowledge one paused visit, saving only when its definition requires a state."""
        record = response["checkpoint"]
        saving = record.get("save_required", True)
        if not isinstance(saving, bool):
            raise RuntimeError("Checkpoint save policy must be a boolean.")
        pending_status = "awaiting_save" if saving else "awaiting_ack"
        if record["status"] != pending_status:
            raise CheckpointStopped(record)
        if saving:
            StateSlots.validate_slot(slot)
        elif slot is not None or previous_state is not None:
            raise ValueError("A log-only checkpoint must not select a state slot.")
        if record["run_id"] != self.run_id or response.get("emulator_paused") is not True:
            raise RuntimeError("Checkpoint identity or native pause does not match this run.")
        name, visit = record["checkpoint"], record["visit"]
        stem = f"{visit:04d}-{name}"
        receipt_path = self.directory / f"{stem}.json"
        destination = self.directory / f"{stem}.ds{slot % 10}" if saving else receipt_path
        if not saving and receipt_path.exists():
            raise RuntimeError(f"Checkpoint evidence already exists: {receipt_path}")
        evidence = {"pending": record}
        before = self.emulator.status()
        if (before.get("frame") != record["frame"] or before.get("checkpoint_epoch") != record["epoch"]
                or before.get("checkpoint_visit") != visit or before.get("checkpoint_status") != pending_status
                or (record["decision"] != "abort" and before.get("observation_stale"))
                or before.get("emulator_paused") is not True):
            raise RuntimeError("The published checkpoint identity changed before its acknowledgement.")
        write_json(receipt_path, evidence)
        acknowledged = False
        try:
            saved = self._retain(slot, destination, before) if saving else None
            evidence["host_savestate"] = saved
            arguments = [self.run_id, visit, record["frame"], record["epoch"], destination.as_posix(), "true"]
            if not saving:
                arguments.append("false")
            completed = self.command("complete", *arguments)
            acknowledged = completed["checkpoint"]["proceed"]
            evidence["completed"] = completed["checkpoint"]
            write_json(receipt_path, evidence)
            write_json(self.work / "checkpoint-progress.json", completed["progress"])
            if not completed["checkpoint"]["proceed"]:
                raise CheckpointStopped(completed["checkpoint"])
        except BaseException as error:
            if saving and not acknowledged and previous_state is not None:
                shutil.copy2(previous_state, self.emulator.states.path(slot))
            evidence["host_error"] = str(error) or type(error).__name__
            write_json(receipt_path, evidence)
            raise
        completed["host_savestate"] = saved
        completed["host_receipt"] = str(receipt_path)
        return completed

    def stop(self, message: str, *, slot: int | None) -> dict:
        message = message.replace("|", "/").replace("\r", " ").replace("\n", " ")
        response = self.command("abort", message)
        record = response.get("checkpoint")
        if record and record.get("status") in {"awaiting_save", "awaiting_ack"}:
            if record.get("save_required", True) and slot is None:
                response = self.command("abort", message)
                record = response.get("checkpoint")
            else:
                try:
                    return self.acknowledge(response, slot=slot if record.get("save_required", True) else None)
                except CheckpointStopped as error:
                    return {**response, "checkpoint": error.record}
        if slot is not None and (not record or not record.get("savestate") and record.get("save_required", True)):
            # A drift or transport/storage failure can cancel the original visit.
            # Label this separately as the current stopped state, not its old snapshot.
            current = self.emulator.status()
            response["stopped_state"] = self._retain(slot, self.directory / f"stopped-state.ds{slot % 10}", current)
            response["stopped_state"].update(frame=response["frame"], epoch=response["epoch"],
                                            observation_stale=current.get("observation_stale", False))
        return response
