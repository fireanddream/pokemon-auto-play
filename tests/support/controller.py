"""Independent menu/state model for shared host I/O checks."""

from copy import deepcopy
import json
import os
from types import SimpleNamespace


class SlotController:
    def __init__(self, work):
        self.EMULATOR = work / "emulator"
        (self.EMULATOR / "StateSlots").mkdir(parents=True)
        self.ROM = work / "Prepared Platinum.nds"
        self.STATUS = work / "status.json"
        self.USER32 = SimpleNamespace(GetMenu=lambda window: 22)
        self.observation = {
            "frame": 100, "party_count": 3, "held_item_id": 260,
            "input_guard_active": False, "emulator_paused": True, "buttons": "",
        }
        self.menu_calls = []
        self.bridge_calls = []
        self.pause_requests = []
        self.is_paused = True
        self.write_saves = True
        self.save_header = b"DeSmuME SState" + b"\0" * 2
        self.after_save = None
        self.resume_on_load = False
        self.publish()

    def session(self):
        return {"window": 11, "rom": str(self.ROM)}

    def menu_items(self, menu):
        assert menu == 22
        return [
            {"id": base + slot,
             "text": f"&File/{operation} State/&{0 if slot == 10 else slot}"
                     + ("    30-Oct-2026 16:22:16" if self.state_file(slot).is_file() else "")
                     + f"\t{prefix}F{slot}"}
            for operation, base, prefix in (("Save", 100, "Shift+"), ("Load", 200, ""))
            for slot in (1, 2, 3, 10)
        ]

    def state_file(self, slot):
        suffix = 0 if slot == 10 else slot
        return self.EMULATOR / "StateSlots" / f"{self.ROM.stem}.ds{suffix}"

    def publish(self):
        self.STATUS.write_text(json.dumps(self.observation), encoding="utf-8")

    def status(self):
        return {**json.loads(self.STATUS.read_text(encoding="utf-8")), "emulator_paused": self.is_paused}

    def set_pause(self, wanted):
        self.pause_requests.append(wanted)
        self.is_paused = wanted

    def paused(self, data):
        assert data["window"] == 11
        return self.is_paused

    def send(self, window, message, identifier):
        assert (window, message) == (11, 0x111)
        operation = "Save" if identifier < 200 else "Load"
        slot = identifier - (100 if operation == "Save" else 200)
        self.menu_calls.append((operation, slot))
        path = self.state_file(slot)
        if operation == "Save":
            if self.write_saves:
                previous = path.stat().st_mtime_ns if path.exists() else 0
                path.write_bytes(self.save_header + json.dumps(self.observation).encode("utf-8"))
                # Model a completed write independently of Windows timestamp granularity.
                written = path.stat()
                os.utime(path, ns=(written.st_atime_ns, max(written.st_mtime_ns, previous + 1_000_000)))
            if self.after_save is not None:
                self.after_save(self)
        else:
            self.observation = json.loads(path.read_bytes()[16:].decode("utf-8"))
            self.is_paused = not self.resume_on_load
            # A native load leaves the host's cached Lua publication stale.

    def bridge_command(self, action, arguments, timeout):
        assert action == "snapshot" and arguments == []
        self.bridge_calls.append((action, arguments, timeout))
        self.observation["frame"] += 1
        self.observation["emulator_paused"] = self.is_paused = True
        self.publish()
        return deepcopy(self.observation)
