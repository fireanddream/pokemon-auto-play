"""High-level emulator operations shared by packages and host runners."""

from __future__ import annotations

import configparser
from datetime import datetime
from pathlib import Path
import time

from .paths import project_path
from .savestates import StateSlots


class DeSmuME:
    def __init__(self, controller=None):
        if controller is None:
            from .controller import Controller

            controller = Controller()
        self.controller = controller
        self.states = StateSlots(controller)

    def status(self) -> dict:
        return self.controller.status()

    def command(self, action: str, *arguments: object, timeout: float | None = 45,
                deadline: float | None = None) -> dict:
        options = {"deadline": deadline} if deadline is not None else {}
        result = self.controller.bridge_command(action, [str(item) for item in arguments], timeout, **options)
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result

    def combo(self, name: str, *arguments: object, timeout: float = 45) -> dict:
        return self.command("combo", name, *arguments, timeout=timeout)

    def inputs(self, steps: list[str], *, timeout: float = 45) -> dict:
        return self.combo("inputs", *steps, timeout=timeout) if steps else self.status()

    def launch(self, rom: str, lua_script: str) -> dict:
        root = self.controller.ROOT
        data = self.controller.launch(project_path(rom, root))
        attached = self.controller.load_lua(project_path(lua_script, root))
        return {**data, **attached, "emulator_paused": True}

    def attach(self, script: str) -> dict:
        return {**self.controller.load_lua(project_path(script, self.controller.ROOT)), "emulator_paused": True}

    def menu_state(self, identifier: int) -> int:
        window = self.controller.session()["window"]
        self.controller.send(window, 0x211)
        try:
            state = self.controller.USER32.GetMenuState(self.controller.USER32.GetMenu(window), identifier, 0)
        finally:
            self.controller.send(window, 0x212)
        if state == 0xFFFFFFFF:
            raise RuntimeError(f"Emulator menu item {identifier} was not found.")
        return state

    def set_frame_limit(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean.")
        self.controller.set_pause(True)
        if bool(self.menu_state(40005) & 0x8) != enabled:
            self.controller.send(self.controller.session()["window"], 0x111, 40005)
        if bool(self.menu_state(40005) & 0x8) != enabled:
            raise RuntimeError("The emulator frame limiter did not reach the requested setting.")
        return {"frame_limit": enabled, "emulator_paused": True}

    def configuration(self) -> configparser.ConfigParser:
        settings = configparser.ConfigParser(interpolation=None, strict=False)
        path = self.controller.EMULATOR / "desmume.ini"
        if path.is_file():
            with path.open(encoding="utf-8-sig") as stream:
                settings.read_file(stream)
        return settings

    def restore_normal_speed(self, *, frame_limit: bool = True) -> dict:
        """Restore 100% and release fast-forward, with the requested frame limiter."""
        if not isinstance(frame_limit, bool):
            raise ValueError("frame_limit must be a boolean.")
        self.controller.set_pause(True)
        settings = self.configuration()
        keys = {}
        for name, default in (("IncreaseSpeed", 187), ("DecreaseSpeed", 189), ("FastForward", 9)):
            code = settings.getint("Hotkeys", name, fallback=default)
            modifiers = settings.getint("Hotkeys", f"{name} MOD", fallback=0)
            if not 1 <= code <= 255 or modifiers & ~0x7:
                raise RuntimeError(f"A keyboard binding is required for {name} to restore normal speed.")
            keys[name] = code | ((modifiers or 0x8) << 16)
        window = self.controller.session()["window"]
        def post(message: int, key: int, flags: int) -> None:
            if not self.controller.USER32.PostMessageW(window, message, key, flags):
                raise RuntimeError("The emulator hotkey could not be queued.")

        # Key-up invokes the hold handler's explicit FastForward = 0, even after a toggle.
        # Use the same queued custom-key messages as DeSmuME's input thread;
        # handlers must not reenter a paused emulator through SendMessage.
        post(0x433, keys["FastForward"], 0xC0000001)
        # The stored INI index can differ from the fresh process's live index.
        # Saturate the 13-entry table, then choose its 100% entry explicitly.
        for name, count in (("IncreaseSpeed", 12), ("DecreaseSpeed", 5)):
            for _ in range(count):
                post(0x432, keys[name], 1)
                post(0x433, keys[name], 0xC0000001)
                time.sleep(0.05)
        deadline = time.monotonic() + 5 if getattr(self.controller, "timeouts_enabled", True) else None
        while self.configuration().getint("Video", "FPS Scaler Index", fallback=-1) != 5:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("timeout")
            time.sleep(0.05)
        limited = self.set_frame_limit(frame_limit)
        return {**limited, "speed_percent": 100, "fast_forward": False}

    def stop_movie(self) -> dict:
        self.controller.set_pause(True)
        self.controller.send(self.controller.session()["window"], 0x111, 40019)
        if not self.menu_state(40019) & 0x3:
            raise RuntimeError("The emulator movie remains active after Stop Movie.")
        return {"movie_active": False, "emulator_paused": True}

    def start_rtc(self, rtc_start: str, movie_path: str, sram_path: str | None = None) -> dict:
        from .rtc import start_recording

        when = datetime.fromisoformat(rtc_start)
        if when.tzinfo is not None or when.microsecond:
            raise ValueError("RTC start uses local emulator time with whole seconds and no timezone offset.")
        root = self.controller.ROOT
        movie = project_path(movie_path, root)
        if not movie.is_relative_to(root) or movie.suffix.lower() != ".dsm" or not movie.parent.is_dir():
            raise ValueError("Use a .dsm path in an existing project-local working directory.")
        if movie.exists():
            raise ValueError(f"Movie output already exists: {movie}")
        rom = Path(self.controller.session().get("rom", self.controller.ROM))
        matching_save = self.controller.EMULATOR / "Battery" / f"{rom.stem}.dsv"
        sram = project_path(sram_path, root) if sram_path is not None else matching_save if matching_save.is_file() else None
        if sram is not None and not sram.is_file():
            raise ValueError(f"Battery save not found: {sram}")
        self.stop_movie()
        start_recording(self.controller, when, movie, sram)
        self.controller.set_pause(True)
        notify = getattr(self.controller, "state_changed", None)
        if notify is not None:
            notify("reset")
        return {
            "rtc_start": when.isoformat(sep=" "), "movie_path": str(movie),
            "sram_path": str(sram) if sram is not None else None,
            "movie_active": True, "emulator_paused": True,
        }

    def slot_menu(self, operation: str, slot: int) -> dict:
        return self.states.menu(operation, slot)

    def state_path(self, slot: int) -> Path:
        return self.states.path(slot)

    def save_state(self, slot: int) -> dict:
        return self.states.save(slot)

    def load_state(self, slot: int, refresh: bool = True, timeout: float = 45) -> dict:
        return self.states.load(slot, refresh, timeout)

    def screenshot(self, path: str, refresh: bool = False, timeout: float = 45) -> dict:
        if refresh:
            self.command("snapshot", timeout=timeout)
        return self.controller.screenshot(project_path(path, self.controller.ROOT))
