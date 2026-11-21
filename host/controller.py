"""Configured DeSmuME connection and Lua mailbox protocol."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import time

from .paths import ROOT, project_path


@dataclass(frozen=True)
class ControllerConfig:
    root: Path = ROOT
    emulator: Path | None = None
    rom: Path | None = None
    mailbox_dir: Path | None = None
    executable: Path | None = None

    def __post_init__(self):
        root = Path(self.root).resolve()
        emulator = project_path(self.emulator, root) if self.emulator is not None else root / "desmume-0.9.13-win64"
        executable = project_path(self.executable, root) if self.executable is not None else emulator / "DeSmuME_0.9.13_x64.exe"
        if self.executable is not None:
            emulator = executable.parent
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "emulator", emulator)
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "rom", project_path(self.rom, root) if self.rom is not None else emulator / "Roms" / "Pocket Monsters - Platinum (Japan).nds")
        object.__setattr__(self, "mailbox_dir", project_path(self.mailbox_dir, root) if self.mailbox_dir is not None else root / "tmp")


class Controller:
    """One host caller, with paths owned by this connection rather than module globals."""

    def __init__(self, config: ControllerConfig | None = None, *, windows_api=None):
        self.config = config if config is not None else ControllerConfig()
        self.ROOT, self.EMULATOR, self.ROM = self.config.root, self.config.emulator, self.config.rom
        self.EXE = self.config.executable
        self.SESSION = self.config.mailbox_dir / "session.json"
        self.STATUS = self.config.mailbox_dir / "status.json"
        self.COMMAND = self.config.mailbox_dir / "command.txt"
        self.CHECKPOINT_COMMAND = self.config.mailbox_dir / "checkpoint-command.txt"
        self.CHECKPOINT_STATUS = self.config.mailbox_dir / "checkpoint-status.json"
        self.INPUT_BRIDGE = self.ROOT / "lua" / "input_bridge.lua"
        if windows_api is None:
            from . import windows as windows_api
        self._windows = windows_api
        self.timeouts_enabled = True
        self.bridge_observer = None

    def _observe_bridge(self, event, **details):
        if self.bridge_observer is not None:
            try:
                self.bridge_observer(event, details)
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._windows, name)

    @contextmanager
    def without_timeouts(self):
        """Apply an explicitly selected unbounded host policy to this caller only."""
        previous = self.timeouts_enabled
        self.timeouts_enabled = False
        try:
            yield
        finally:
            self.timeouts_enabled = previous

    def deadline(self, seconds: float | None) -> float | None:
        return time.monotonic() + seconds if self.timeouts_enabled and seconds is not None else None

    @staticmethod
    def waiting(deadline: float | None) -> bool:
        return deadline is None or time.monotonic() < deadline

    def send(self, window: int, message: int, wparam: int = 0, lparam: int = 0) -> int:
        if self.timeouts_enabled:
            return self._windows.send(window, message, wparam, lparam)
        return self._windows.send(window, message, wparam, lparam, timeout=None)

    def session(self) -> dict:
        data = json.loads(self.SESSION.read_text(encoding="utf-8"))
        process_id = wintypes.DWORD()
        self.USER32.GetWindowThreadProcessId(data["window"], ctypes.byref(process_id))
        if (not self.USER32.IsWindow(data["window"]) or process_id.value != data["pid"]
                or not self.process_alive(data["pid"])):
            raise SystemExit("The saved emulator window is no longer open. Run launch first.")
        return data

    def status(self) -> dict:
        result = json.loads(self.STATUS.read_text(encoding="utf-8"))
        result["emulator_paused"] = self.paused(self.session())
        return result

    def inspect(self, menus: bool = False) -> dict:
        data = self.session()
        top = self.windows(data["pid"])
        result = {**data, "paused": self.paused(data), "windows": top}
        if menus:
            result["menus"] = self.menu_items(self.USER32.GetMenu(data["window"]))
        for item in top:
            item["children"] = self.windows(data["pid"], item["handle"])
        return result

    def log_lua(self, message: str) -> None:
        """Append a controller event to the attached Lua script's output console."""
        data = self.session()
        dialog = data.get("lua_window", 0)
        process_id = wintypes.DWORD()
        self.USER32.GetWindowThreadProcessId(dialog, ctypes.byref(process_id))
        if not self.USER32.IsWindow(dialog) or process_id.value != data["pid"]:
            raise RuntimeError("The attached Lua script window is unavailable.")
        console = self.USER32.GetDlgItem(dialog, 309)
        if not console:
            raise RuntimeError("The Lua output console was not found.")
        text = message.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        buffer = ctypes.create_unicode_buffer(text.replace("\n", "\r\n") + "\r\n")
        length = self.send(console, 0xE)
        self.send(console, 0xB1, length, length)  # EM_SETSEL: append at the end.
        self.send(console, 0xC2, 0, ctypes.addressof(buffer))  # EM_REPLACESEL.
        self.send(console, 0xB7)  # EM_SCROLLCARET: make the event visible.

    def launch(self, rom: Path | None = None, *, check_existing: bool = True) -> dict:
        rom = (rom if rom is not None else self.ROM).resolve()
        if not self.EXE.is_file() or not rom.is_file() or not self.SESSION.parent.is_dir():
            raise SystemExit("Expected portable emulator, ROM, and project scratch directory.")
        if check_existing:
            existing = sorted(set(self.emulator_process_ids()) | set(self.emulator_window_process_ids()))
            if existing:
                identifiers = ", ".join(str(pid) for pid in existing)
                raise SystemExit(f"DeSmuME is still running or shutting down (PIDs {identifiers}). Complete shutdown before launching another instance.")
        if self.SESSION.exists():
            try:
                self.session()
            except SystemExit:
                pass
            else:
                raise SystemExit("The controlled emulator is already open.")
        process = subprocess.Popen(
            [str(self.EXE), "--start-paused", str(rom)], cwd=self.EMULATOR,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = self.deadline(30)
        while self.waiting(deadline):
            if process.poll() is not None:
                raise SystemExit(f"DeSmuME exited with code {process.returncode}.")
            main = [item for item in self.windows(process.pid) if self.USER32.GetMenu(item["handle"])]
            if main:
                data = {"pid": process.pid, "window": main[0]["handle"], "rom": str(rom)}
                self.SESSION.write_text(json.dumps(data, indent=2), encoding="utf-8")
                return data
            time.sleep(0.2)
        raise TimeoutError("timeout")

    def close_existing(self) -> list[int]:
        """End prior DeSmuME processes before a fresh ROM boot; caller owns the host lock."""
        owners = sorted(set(self.emulator_process_ids()) | set(self.emulator_window_process_ids()))
        deadline = self.deadline(None)  # The caller's existing whole-run deadline applies.
        for pid in owners:
            if not self.waiting(deadline):
                raise TimeoutError("timeout")
            for window in self.windows(pid):
                if self.USER32.GetMenu(window["handle"]):
                    try:
                        self.send(window["handle"], 0x10)  # Request normal shutdown first.
                    except (OSError, RuntimeError):
                        pass
            if self.process_alive(pid):
                self.terminate_process(pid)
        while self.emulator_process_ids() or self.emulator_window_process_ids():
            if not self.waiting(deadline):
                raise TimeoutError("timeout")
            time.sleep(0.05)
        self.SESSION.unlink(missing_ok=True)
        return owners

    def load_lua(self, script: Path) -> dict:
        data = self.session()
        script = script.resolve()
        if not script.is_file():
            raise SystemExit(f"Lua script not found: {script}")

        def attach(dialog: int) -> dict:
            started = time.time_ns()
            path = ctypes.create_unicode_buffer(str(script))
            self.send(self.USER32.GetDlgItem(dialog, 310), 0xC, 0, ctypes.addressof(path))
            data["lua_window"], data["lua_script"] = dialog, str(script)
            self.SESSION.write_text(json.dumps(data, indent=2), encoding="utf-8")
            if script == self.INPUT_BRIDGE:
                deadline = self.deadline(10)
                while self.waiting(deadline):
                    try:
                        ready = self.STATUS.stat().st_mtime_ns >= started
                        status = json.loads(self.STATUS.read_text(encoding="utf-8"))
                    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                        ready, status = False, {}
                    if ready and status.get("bridge") == "input":
                        self.set_pause(True)
                        return data
                    time.sleep(0.05)
                raise TimeoutError("timeout")
            return data

        existing = data.get("lua_window", 0)
        process_id = wintypes.DWORD()
        self.USER32.GetWindowThreadProcessId(existing, ctypes.byref(process_id))
        if self.USER32.IsWindow(existing) and process_id.value == data["pid"] and self.USER32.GetDlgItem(existing, 310):
            return attach(existing)
        before = {item["handle"] for item in self.windows(data["pid"])}
        candidates = [item for item in self.menu_items(self.USER32.GetMenu(data["window"])) if "new lua" in item["text"].lower()]
        if len(candidates) != 1:
            raise SystemExit("Could not find DeSmuME's New Lua Script menu item.")
        self.post_menu(data["window"], candidates[0]["id"])
        deadline = self.deadline(10)
        while self.waiting(deadline):
            for item in self.windows(data["pid"]):
                dialog = item["handle"]
                path_box = self.USER32.GetDlgItem(dialog, 310)
                if dialog not in before and path_box:
                    return attach(dialog)
            time.sleep(0.1)
        raise TimeoutError("timeout")

    def paused(self, data: dict) -> bool:
        # DeSmuME refreshes pause check marks on WM_ENTERMENULOOP.
        self.send(data["window"], 0x211)
        try:
            state = self.USER32.GetMenuState(self.USER32.GetMenu(data["window"]), 113, 0)
        finally:
            self.send(data["window"], 0x212)
        if state == 0xFFFFFFFF:
            raise RuntimeError("DeSmuME's pause menu item was not found.")
        paused = bool(state & 0x8)
        self._observe_bridge("pause_observed", paused=paused)
        return paused

    def stop_lua(self) -> dict:
        """Stop the controlled attachment at a native pause before removing its mailbox."""
        data = self.session()
        self.set_pause(True)
        dialog = data.get("lua_window", 0)
        process_id = wintypes.DWORD()
        self.USER32.GetWindowThreadProcessId(dialog, ctypes.byref(process_id))
        if not self.USER32.IsWindow(dialog) or process_id.value != data["pid"]:
            raise RuntimeError("The controlled Lua script window is unavailable.")
        stop = self.USER32.GetDlgItem(dialog, 312)
        if not stop:
            raise RuntimeError("The Lua Stop control was not found.")
        if self.USER32.IsWindowEnabled(stop):
            self.send(dialog, 0x111, 312, stop)
            deadline = self.deadline(None)
            while self.USER32.IsWindowEnabled(stop):
                if not self.waiting(deadline):
                    raise TimeoutError("timeout")
                time.sleep(0.01)
        if not self.paused(data):
            raise RuntimeError("Native pause was lost while stopping the Lua script.")
        data.pop("lua_script", None)
        self.SESSION.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return {"lua_stopped": True, "emulator_paused": True}

    def set_pause(self, wanted: bool) -> None:
        self._observe_bridge("pause_requested", wanted=wanted)
        data = self.session()
        if self.paused(data) != wanted:
            self.post_menu(data["window"], 113)
            self._observe_bridge("pause_toggle_queued", wanted=wanted)
            deadline = self.deadline(5)
            while self.waiting(deadline):
                if self.paused(data) == wanted:
                    return
                time.sleep(0.05)
            raise TimeoutError("timeout")

    def checkpoint_command(self, action: str, arguments: list[str]) -> dict:
        """Synchronous paused Lua RPC through DeSmuME's menu-start callback."""
        fields = [action, *arguments]
        if any(not isinstance(value, str) or any(char in value for char in "|\r\n") for value in fields):
            raise ValueError("Checkpoint command fields cannot contain mailbox delimiters.")
        data = self.session()
        self.set_pause(True)
        if not self.paused(data):
            raise RuntimeError("Checkpoint RPC requires a confirmed native pause.")
        identifier = str(time.time_ns())
        temporary = self.CHECKPOINT_COMMAND.with_suffix(".new")
        temporary.write_text("|".join([identifier, *fields]) + "\n", encoding="utf-8")
        temporary.replace(self.CHECKPOINT_COMMAND)
        # The Windows frontend invokes registermenustart on WM_ENTERMENULOOP.
        # A synchronous message acknowledges the callback without resuming a frame.
        try:
            self.send(data["window"], 0x211)
        finally:
            self.send(data["window"], 0x212)
        result = json.loads(self.CHECKPOINT_STATUS.read_text(encoding="utf-8"))
        if result.get("command_id") != identifier or result.get("command_done") is not True:
            raise RuntimeError("The paused checkpoint callback did not acknowledge this request.")
        if not self.paused(data):
            self.set_pause(True)
            raise RuntimeError("The emulator lost its pause during checkpoint RPC.")
        result["emulator_paused"] = True
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result

    def state_changed(self, operation: str, checkpoint_visit: int | None = None) -> None:
        """Notify the attached checkpoint observer after every controlled native reset/load."""
        try:
            status = json.loads(self.STATUS.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if status.get("checkpoint_protocol") == 1:
            self.checkpoint_command("state-changed", [operation, str(checkpoint_visit) if checkpoint_visit is not None else ""])

    def bridge_command(self, action: str, arguments: list[str], timeout: float | None,
                       *, deadline: float | None = None) -> dict:
        """Run an action; an explicit monotonic deadline also applies under without_timeouts()."""
        action_deadline = deadline
        self._observe_bridge("command_begin", action=action, arguments=arguments)
        try:
            data = self.session()
            self.set_pause(True)
            if not self.waiting(action_deadline):
                raise TimeoutError("timeout")
            identifier = time.time_ns()
            temporary = self.COMMAND.with_suffix(".new")
            temporary.write_text("|".join([str(identifier), action, *arguments]) + "\n", encoding="ascii")
            temporary.replace(self.COMMAND)
            self._observe_bridge("command_issued", command_id=str(identifier), action=action)
            # Resume once: short commands can finish before a host poll sees them running.
            self.post_menu(data["window"], 113)
            self._observe_bridge("pause_toggle_queued", wanted=False)
            deadline = action_deadline if action_deadline is not None else self.deadline(timeout)
            while self.waiting(deadline):
                try:
                    status = json.loads(self.STATUS.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError, PermissionError) as error:
                    self._observe_bridge("status_read_error", error_type=type(error).__name__)
                    time.sleep(0.05)
                    continue
                self._observe_bridge("status", status=status)
                if status.get("command_id") == str(identifier) and status.get("command_done"):
                    self._observe_bridge("completion_reported")
                    pause_deadline = action_deadline if action_deadline is not None else self.deadline(5 if timeout is not None else None)
                    while not self.paused(data):
                        if not self.waiting(pause_deadline):
                            self._observe_bridge("deadline_expired")
                            self.set_pause(True)
                            raise TimeoutError("timeout")
                        time.sleep(0.02)
                    status["emulator_paused"] = True
                    if status.get("matched") and status.get("phase") == "target_hit":
                        self.notify_seed_hit(data, status)
                        status["alert_sent"] = True
                    self._observe_bridge("command_returned")
                    return status
                time.sleep(0.05)
            self._observe_bridge("deadline_expired")
            self.set_pause(True)
            raise TimeoutError("timeout")
        except BaseException as error:
            self._observe_bridge("command_failed", error_type=type(error).__name__, error=str(error))
            raise

    def combo(self, name: str, *arguments: object, timeout: float = 45) -> dict:
        """Execute a complete Lua-side input plan, pausing once at its endpoint."""
        return self.bridge_command("combo", [name, *(str(value) for value in arguments)], timeout)

    def startup(self, timeout: float = 45) -> dict:
        """Run continuously from ROM boot through loading the save with Wi-Fi off."""
        result = self.combo("startup", timeout=timeout)
        result["startup_complete"] = result.get("stop_reason") == "combo_complete" and not result.get("error")
        result["startup_commands"] = result.get("combo_history") or []
        return result

    def notify_seed_hit(self, data: dict, status: dict) -> None:
        self.alert(data["window"])
        print(f"TARGET HIT: {status['initial_seed']} at emulator frame {status['frame']}. Emulator paused.")

    def screenshot(self, path: Path) -> dict:
        """Convert the latest Lua framebuffer capture into a PNG image."""
        self.session()
        path = path.resolve()
        if not path.parent.is_dir():
            raise SystemExit("Screenshot output directory must already exist.")
        import struct
        import zlib

        raw = (self.config.mailbox_dir / "screen.gd").read_bytes()
        magic, width, height = struct.unpack_from(">HHH", raw)
        if magic != 65534 or raw[6] != 1 or len(raw) != 11 + width * height * 4:
            raise SystemExit("Lua screenshot is not a supported true-color GD image.")

        def png_chunk(kind: bytes, content: bytes) -> bytes:
            return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))

        rows = bytearray()
        for row in range(height):
            rows.append(0)
            start = 11 + row * width * 4
            for column in range(width):
                offset = start + column * 4
                rows.extend(raw[offset + 1:offset + 4])
        png = b"\x89PNG\r\n\x1a\n"
        png += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        png += png_chunk(b"IDAT", zlib.compress(rows))
        png += png_chunk(b"IEND", b"")
        path.write_bytes(png)
        return {"screenshot": str(path), "width": width, "height": height}
