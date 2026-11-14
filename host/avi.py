"""Native AVI dialogs; starting recording automatically resumes emulation."""

from __future__ import annotations

import ctypes
from pathlib import Path
import time


class AviRecording:
    MENU = 40015
    CODEC = "Full Frames (Uncompressed)"
    DIALOGS = ("Video Compression", "Compression Options", "Save AVI as")

    def __init__(self, emulator, path: Path):
        self.emulator, self.controller = emulator, emulator.controller
        self.path = path.resolve()
        if (not self.path.is_relative_to(self.controller.ROOT) or self.path.suffix.lower() != ".avi"
                or not self.path.parent.is_dir() or self.path.exists()):
            raise ValueError("AVI recording requires a fresh project-local .avi path with an existing parent.")
        self.requested, self.started = False, False
        self.previous_windows = set()

    def active(self):
        self.emulator.menu_state(self.MENU)
        window = self.controller.session()["window"]
        item = next(item for item in self.controller.menu_items(self.controller.USER32.GetMenu(window))
            if item["id"] == self.MENU)
        label = item["text"].rsplit("/", 1)[-1].replace("&", "").strip().lower()
        if label not in {"record avi", "stop avi"}:
            raise RuntimeError(f"Unrecognized AVI menu state: {label}")
        return label == "stop avi"

    def dialogs(self):
        return [item for item in self.controller.windows(self.controller.session()["pid"])
            if item["class"] == "#32770" and item["handle"] not in self.previous_windows
            and item["text"] in self.DIALOGS]

    def start(self, *, deadline: float):
        """Start from pause; the caller must arm its Lua endpoint before the native resume."""
        controller = self.controller
        controller.set_pause(True)
        if self.active():
            raise RuntimeError("An AVI recording is already active.")
        if self.path.exists():
            raise ValueError("The requested AVI output already exists.")
        data = controller.session()
        self.previous_windows = {item["handle"] for item in controller.windows(data["pid"])}
        self.requested = True
        controller.post_menu(data["window"], self.MENU)
        submitted, codec_confirmed = set(), False
        while time.monotonic() < deadline:
            dialogs = self.dialogs()
            for title in ("Save AVI as", "Video Compression", "Compression Options"):
                matching = [item for item in dialogs if item["text"] == title]
                if not matching:
                    continue
                if len(matching) != 1:
                    raise RuntimeError(f"Multiple AVI setup dialogs: {title}")
                dialog = matching[0]["handle"]
                if dialog in submitted:
                    break
                if title == "Save AVI as":
                    edits = [item for item in controller.windows(data["pid"], dialog)
                        if item["class"] == "Edit" and item["id"] == 1001]
                    if len(edits) != 1:
                        time.sleep(0.01)
                        break
                    text = ctypes.create_unicode_buffer(str(self.path))
                    controller.send(edits[0]["handle"], 0xC, 0, ctypes.addressof(text))
                elif title == "Video Compression":
                    combo = controller.USER32.GetDlgItem(dialog, 880)
                    if not combo:
                        time.sleep(0.01)
                        break
                    text = ctypes.create_unicode_buffer(self.CODEC)
                    index = controller.send(combo, 0x158, -1, ctypes.addressof(text))  # CB_FINDSTRINGEXACT.
                    if not 0 <= index < controller.send(combo, 0x146):  # CB_GETCOUNT.
                        raise RuntimeError("The uncompressed AVI codec is unavailable.")
                    controller.send(combo, 0x14E, index)  # CB_SETCURSEL.
                    controller.send(dialog, 0x111, 880 | (1 << 16), combo)  # CBN_SELCHANGE.
                    if controller.control_text(combo) != self.CODEC:
                        raise RuntimeError("The uncompressed AVI codec was not selected.")
                    codec_confirmed = True
                elif not codec_confirmed:
                    break
                controller.post_menu(dialog, 1)
                submitted.add(dialog)
                break
            if not dialogs and self.active():
                self.started = True
                if not codec_confirmed:
                    raise RuntimeError("AVI setup did not confirm the uncompressed codec.")
                return {"avi_path": str(self.path), "avi_active": True,
                    "codec": self.CODEC, "native_auto_resume": True}
            time.sleep(0.01)
        raise TimeoutError("AVI setup exceeded the dance-run host deadline.")

    def cancel_setup(self):
        if self.requested:
            for title in self.DIALOGS:
                for item in self.dialogs():
                    if item["text"] == title:
                        self.controller.send(item["handle"], 0x111, 2,
                            self.controller.USER32.GetDlgItem(item["handle"], 2))

    def stop(self):
        self.controller.set_pause(True)
        if self.active():
            self.controller.send(self.controller.session()["window"], 0x111, self.MENU)
        if self.active():
            raise RuntimeError("AVI recording remains active after stopping.")
        size = self.path.stat().st_size if self.path.exists() else 0
        if self.started:
            with self.path.open("rb") as stream:
                header = stream.read(12)
            if size <= 12 or header[:4] != b"RIFF" or header[8:] != b"AVI ":
                raise RuntimeError("The finalized recording is not a nonempty AVI file.")
        return {"avi_path": str(self.path), "avi_active": False, "bytes": size,
            "emulator_paused": self.controller.paused(self.controller.session())}
