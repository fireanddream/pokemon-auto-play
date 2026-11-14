"""Shared menu-driven savestate I/O, with zero-frame saves and optional load refresh."""

from __future__ import annotations

import json
from pathlib import Path
import re
import time


class StateSlots:
    def __init__(self, controller):
        self.controller = controller

    @staticmethod
    def validate_slot(slot: int) -> None:
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 10:
            raise ValueError("State slot must be an integer from 1 through 10.")

    def menu(self, operation: str, slot: int, *, data: dict | None = None) -> dict:
        self.validate_slot(slot)
        if operation not in {"Save", "Load"}:
            raise ValueError("State slot operation must be Save or Load.")
        data = self.controller.session() if data is None else data
        shortcut = f"Shift+F{slot}" if operation == "Save" else f"F{slot}"
        label_pattern = re.compile(rf"/{operation} State/&{slot % 10}(?:\s+.*)?$")
        matches = []
        for item in self.controller.menu_items(self.controller.USER32.GetMenu(data["window"])):
            label, separator, key = item["text"].partition("\t")
            if separator and label_pattern.search(label) and key == shortcut:
                matches.append(item)
        if len(matches) != 1:
            raise RuntimeError(f"The {operation.lower()} menu for state slot {slot} was not found uniquely ({len(matches)} matches).")
        return matches[0]

    def path(self, slot: int, *, data: dict | None = None) -> Path:
        self.validate_slot(slot)
        data = self.controller.session() if data is None else data
        rom = data.get("rom")
        if rom is None:
            rom = self.controller.ROM
        return self.controller.EMULATOR / "StateSlots" / f"{Path(rom).stem}.ds{slot % 10}"

    @staticmethod
    def validate_header(path: Path, slot: int) -> None:
        with path.open("rb") as stream:
            if not stream.read(16).startswith(b"DeSmuME SState"):
                raise RuntimeError(f"State slot {slot} has an unexpected file header.")

    def cached_status(self) -> dict | None:
        try:
            return json.loads(self.controller.STATUS.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def save(self, slot: int, *, expected: dict | None = None, timeout: float | None = 20) -> dict:
        self.validate_slot(slot)
        data = self.controller.session()
        self.controller.set_pause(True)
        if not self.controller.paused(data):
            raise RuntimeError(f"The emulator did not pause before saving state slot {slot}.")
        path = self.path(slot, data=data)
        if not path.parent.is_dir():
            raise RuntimeError("The emulator state-slot directory is missing.")
        item = self.menu("Save", slot, data=data)
        before = expected if expected is not None else self.cached_status()
        previous = path.stat().st_mtime_ns if path.exists() else 0
        if not self.controller.paused(data):
            self.controller.set_pause(True)
            raise RuntimeError(f"The emulator did not remain paused before saving state slot {slot}.")
        self.controller.send(data["window"], 0x111, item["id"])
        deadline = time.monotonic() + timeout if timeout is not None else None
        while deadline is None or time.monotonic() < deadline:
            if path.is_file():
                written = path.stat()
                if written.st_mtime_ns > previous and written.st_size:
                    self.validate_header(path, slot)
                    current = self.cached_status() if before is not None else None
                    changed = before is not None and (current is None or any(
                        current.get(name) != before[name] for name in
                        ("frame", "current_seed", "checkpoint_epoch", "checkpoint_visit", "checkpoint_status") if name in before
                    ))
                    if not self.controller.paused(data):
                        self.controller.set_pause(True)
                        raise RuntimeError("The game changed while the state was saved.")
                    if changed:
                        raise RuntimeError("The game changed while the state was saved.")
                    return {"slot": slot, "path": str(path), "bytes": written.st_size, "emulator_paused": True}
            time.sleep(0.05)
        raise RuntimeError(f"State slot {slot} was not written.")

    def load(self, slot: int, refresh: bool = True, timeout: float | None = 45,
             *, checkpoint_visit: int | None = None) -> dict:
        self.validate_slot(slot)
        if not isinstance(refresh, bool):
            raise ValueError("refresh must be a boolean.")
        data = self.controller.session()
        path = self.path(slot, data=data)
        if not path.is_file():
            raise ValueError(f"State slot file not found: {path}")
        self.validate_header(path, slot)
        item = self.menu("Load", slot, data=data)
        self.controller.set_pause(True)
        self.controller.send(data["window"], 0x111, item["id"])
        if not self.controller.paused(data):
            self.controller.set_pause(True)
            raise RuntimeError(f"State slot {slot} did not remain paused on loading.")
        notify = getattr(self.controller, "state_changed", None)
        if notify is not None:
            notify("load", checkpoint_visit)
        if refresh:
            result = self.controller.bridge_command("snapshot", [], timeout)
            if result.get("error"):
                raise RuntimeError(result["error"])
        else:
            result = {"observation_stale": True}
        return {**result, "slot": slot, "path": str(path), "emulator_paused": True}
