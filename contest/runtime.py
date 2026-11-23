"""The main contest runner's connection, deadlines, native helpers, and cleanup."""

import filecmp
import json
from pathlib import Path
import shutil
import time

from host.controller import Controller, ControllerConfig
from host.checkpoints import CheckpointClient, write_json
from host.emulator import DeSmuME
from host.paths import ROOT
from host.savestates import StateSlots
from contest.platinum import identify_rom
from contest.messages import CHECK_MESSAGES, abort_message
from contest.timing_profiles import load_profile


ROM = ROOT / "desmume-0.9.13-win64/Roms/Pocket Monsters - Platinum (Japan).nds"
ACTING_ENTRY_SLOT = 5
DIAGNOSTIC_SLOT = 9


def checkpoint_identity(record):
    return {key: record[key] for key in ("checkpoint", "run_id", "visit", "frame", "epoch")}


class TimedController(Controller):
    live_deadline: float | None = None
    live_session: dict | None = None

    def remaining(self) -> float:
        if self.live_deadline is None:
            return float("inf")
        value = self.live_deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("timeout")
        return value

    def deadline(self, seconds):
        value = super().deadline(seconds)
        if self.live_deadline is not None:
            return min(value, self.live_deadline) if value is not None else self.live_deadline
        return value

    def waiting(self, deadline):
        if self.live_session is not None:
            session = self.live_session
            if not self.process_alive(session["pid"]) or not self.USER32.IsWindow(session["window"]):
                raise SystemExit("The controlled emulator was closed.")
            if session.get("lua_window") and not self.USER32.IsWindow(session["lua_window"]):
                raise RuntimeError("The contest Lua window was closed.")
        return super().waiting(deadline)

    def send(self, window, message, wparam=0, lparam=0):
        return self._windows.send(window, message, wparam, lparam, timeout=min(3, self.remaining()))


class TimedSlots(StateSlots):
    def __init__(self, controller, *, saves_enabled=True, loads_enabled=True):
        super().__init__(controller)
        self.saves_enabled, self.loads_enabled = saves_enabled, loads_enabled

    def save(self, slot, *, expected=None, timeout=20):
        if not self.saves_enabled:
            raise RuntimeError("Quick saves are disabled for this run.")
        remaining = self.controller.remaining()
        limit = min(20 if timeout is None else timeout, remaining)
        return super().save(slot, expected=expected, timeout=limit)

    def load(self, slot, refresh=True, timeout=45, *, checkpoint_visit=None):
        if not self.loads_enabled:
            raise RuntimeError("Quick-loads are disabled for this run; continue from the current game state.")
        return super().load(slot, refresh=refresh, timeout=timeout, checkpoint_visit=checkpoint_visit)


def lua_value(value):
    if value is None:
        return "nil"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "{" + ",".join(lua_value(item) for item in value) + "}"
    if isinstance(value, dict):
        return "{" + ",".join(f"[{lua_value(key)}]={lua_value(item)}" for key, item in value.items()) + "}"
    raise TypeError(type(value).__name__)


def attach(controller, attachment):
    """Attach the persistent bridge once and wait for its fresh ready publication."""
    session = controller.session()
    old_dialog = session.get("lua_window", 0)
    if controller.USER32.IsWindow(old_dialog):
        stop_button = controller.USER32.GetDlgItem(old_dialog, 312)
        if stop_button and controller.USER32.IsWindowEnabled(stop_button):
            controller.send(old_dialog, 0x111, 312, stop_button)
            while controller.USER32.IsWindowEnabled(stop_button):
                controller.remaining()
                time.sleep(0.01)
    started = time.time_ns()
    controller.load_lua(attachment)
    dialog = controller.session()["lua_window"]
    stop_button = controller.USER32.GetDlgItem(dialog, 312)
    if not controller.USER32.IsWindowEnabled(stop_button):
        controller.send(dialog, 0x111, 311, controller.USER32.GetDlgItem(dialog, 311))
    while True:
        controller.remaining()
        try:
            ready = controller.STATUS.stat().st_mtime_ns >= started
            status = json.loads(controller.STATUS.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
            ready, status = False, {}
        if ready and status.get("bridge") == "pt-super-contest" and status.get("checkpoint_protocol") == 1:
            break
        time.sleep(0.01)
    controller.set_pause(True)


def settle_native_osd(controller, seconds):
    """Let native messages expire while the accepted game frame remains paused."""
    started = time.monotonic()
    if seconds:
        controller.set_pause(True)
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            time.sleep(min(controller.remaining(), max(0, until - time.monotonic())))
        if not controller.paused(controller.session()):
            controller.set_pause(True)
            raise RuntimeError("Native pause was lost while settling the save overlay.")
    return time.monotonic() - started


def settle_native_overlay(controller, capture, seconds, *, name):
    """Bind a paused host wait to fresh released-input captures at one frame/epoch."""
    if not seconds:
        return None
    message = "Native overlay settling must retain released inputs and a fresh capture at the same paused frame and epoch."

    def confirmed_image(packet):
        observation, image = packet["observation"], packet["observation"]["image"]
        if (packet.get("emulator_paused") is not True or observation.get("released") is not True
                or image["frame"] != packet["frame"] or image["epoch"] != packet["epoch"]
                or image["source"] != "fresh_zero_frame_framebuffer"
                or image["capture_callback"] != "paused_lua_menu" or image["direct_read_frames"] != 0):
            raise RuntimeError(message)
        return image

    before = capture(f"overlay-before-{name}")
    before_image = confirmed_image(before)
    elapsed = settle_native_osd(controller, seconds)
    after = capture(f"overlay-settled-{name}")
    image = confirmed_image(after)
    if after["frame"] != before["frame"] or after["epoch"] != before["epoch"]:
        raise RuntimeError(message)
    return {"section": name, "method": "paused_native_save_overlay_wait",
        "requested_host_seconds": seconds, "elapsed_host_seconds": elapsed, "emulator_frames": 0,
        "emulator_paused": True, "input_policy": "none", "frame": after["frame"], "epoch": after["epoch"],
        "before": before_image, "capture": image}


def automatic_review(request, check):
    """Retain and return the confirmed local pixel verdict, or False if unreadable."""
    if check.get("source") != "local_reference_pixels" or check.get("confirmed") is not True:
        return False
    review = {key: request[key] for key in ("run_id", "frame", "epoch")}
    review.update(verdict="pass", source="local_reference_pixels", pixel_check=check,
                  note=f"Fresh paused {check['expected']} pixels match the reviewed native reference.")
    write_json(Path(request["review_path"]), review)
    return review


class ContestRuntime:
    """One connection/client/lifecycle; callable sections own only gameplay actions."""

    def __init__(self, run_id, work, settings, started, *, controller=None):
        self.run_id, self.work, self.settings, self.started = run_id, Path(work), settings, started
        self.total_deadline = started + settings["total_timeout_seconds"]
        self.controller = controller or TimedController(ControllerConfig(rom=ROM))
        self.controller.live_deadline = self.total_deadline
        self.emulator = DeSmuME(self.controller)
        self.native_saves_enabled = settings["save_policy"]["enabled"]
        self.emulator.states = TimedSlots(self.controller, saves_enabled=self.native_saves_enabled,
            loads_enabled=False)
        self.client = CheckpointClient(self.emulator, self.work, run_id)
        self.owned = self.attached = self.finalized = False
        self.originals, self.accepted_slots = {}, {}
        self.boundary, self.position = None, None
        self.target, self.party, self.overworld = None, None, None
        self.unavailable_move_slots = None
        self.bag_context = {"opened_once": False, "cursor_position": 1}
        self.active_work, self.stage, self.section_visit = self.work, None, None
        self.stage_started, self.stage_deadline = started, self.total_deadline
        self.rendering_before, self.speed_phase = None, None
        self.lifecycle = {"lua_attachments": 0, "checkpoint_engines": 0,
                          "speed_transitions": [], "terminal_cleanups": 0}
        self.on_progress = None

    def confirm_ownership(self):
        owners = set(self.controller.emulator_process_ids()) | set(self.controller.emulator_window_process_ids())
        if owners:
            session = self.controller.session()
            if owners != {session["pid"]} or Path(session["rom"]).resolve() != self.controller.ROM.resolve():
                raise RuntimeError("The master requires the sole controlled Platinum emulator.")
            self.owned = True
            self._one_lua_window(session)
            return session
        return None

    def _one_lua_window(self, session):
        dialogs = [item["handle"] for item in self.controller.windows(session["pid"])
                   if self.controller.USER32.GetDlgItem(item["handle"], 310)]
        if len(dialogs) > 1:
            raise RuntimeError("The integrated master requires one Lua script window.")
        return dialogs

    def prepare_fresh_ownership(self, session):
        """Stop the inactive prior script at setup so its scratch handles are closed."""
        if session is None:
            return
        self.controller.set_pause(True)
        dialog = session.get("lua_window", 0)
        if self.controller.USER32.IsWindow(dialog):
            stop = self.controller.USER32.GetDlgItem(dialog, 312)
            if stop and self.controller.USER32.IsWindowEnabled(stop):
                self.controller.send(dialog, 0x111, 312, stop)
                while self.controller.USER32.IsWindowEnabled(stop):
                    self.check()
                    time.sleep(0.01)
                self.lifecycle["prior_lua_stopped_at_setup"] = True

    def check(self):
        return self.controller.remaining()

    def record(self, phase, **details):
        if self.on_progress is not None:
            self.on_progress(phase, stage=self.stage, section_visit=self.section_visit, **details)

    def json(self, name, value):
        path = self.active_work / name
        write_json(path, value)
        return path

    def lua(self, name, value):
        path = self.active_work / name
        path.write_text("return " + lua_value(value) + "\n", encoding="utf-8", newline="\n")
        return path

    def validate_startup(self):
        game = identify_rom(self.controller.ROM)
        profile = self.settings.get("timing_profile")
        if profile is None:
            profile = self.settings["timing_profile"] = load_profile(game["code"])
        if profile["game_code"] != game["code"]:
            raise ValueError("The timing profile must match the selected ROM game code.")
        battery = self.controller.EMULATOR / "Battery" / f"{self.controller.ROM.stem}.dsv"
        if not battery.is_file() or not battery.stat().st_size:
            raise ValueError(f"The game save is missing or empty. Place the matching .dsv save at: {battery}")
        self.settings["game"] = game["code"]
        self.settings["game_language_tested"] = game["tested"]
        return game, battery

    def setup(self, *, fresh_start=False):
        self.check()
        game, battery = self.validate_startup()
        existing = None if fresh_start else self.confirm_ownership()
        if existing is None:
            if fresh_start:
                self.controller.launch(self.controller.ROM, check_existing=False)
            else:
                self.controller.launch(self.controller.ROM)
            self.owned = True
        self.controller.live_session = self.controller.session()
        self.controller.set_pause(True)
        (self.controller.EMULATOR / "StateSlots").mkdir(exist_ok=True)
        self.emulator.stop_movie()
        shutil.copy2(battery, self.work / "prepared-battery.dsv")
        self.lifecycle["speed_setup"] = self.emulator.restore_normal_speed(frame_limit=False)
        self._record_speed("run", self.lifecycle["speed_setup"])
        self.rendering_before = {
            "automatic": bool(self.emulator.menu_state(221) & 0x8),
            "skip_menu": next((item for item in range(191, 201) if self.emulator.menu_state(item) & 0x8), None),
        }
        window = self.controller.session()["window"]
        if self.rendering_before["automatic"]:
            self.controller.send(window, 0x111, 221)
        if not self.emulator.menu_state(191) & 0x8:
            self.controller.send(window, 0x111, 191)
        if self.emulator.menu_state(221) & 0x8 or not self.emulator.menu_state(191) & 0x8:
            raise RuntimeError("Every-frame rendering was not confirmed at master setup.")
        attachment = Path(__file__).resolve().with_name("super_contest.lua")
        mailbox = self.controller.config.mailbox_dir
        if mailbox != ROOT / "tmp":
            wrapper = self.work / "attach.lua"
            wrapper.write_text("CONTEST_MAILBOX_DIR = " + lua_value(mailbox.as_posix())
                + "\ndofile(" + lua_value(attachment.as_posix()) + ")\n", encoding="utf-8", newline="\n")
            attachment = wrapper
        attach(self.controller, attachment)
        self.attached = True
        self.lifecycle["lua_attachments"] += 1
        session = self.controller.session()
        self.controller.live_session = session
        if self._one_lua_window(session) != [session["lua_window"]]:
            raise RuntimeError("The persistent bridge is not the sole Lua window.")
        self.lifecycle["lua_window"] = session["lua_window"]
        if existing is not None:
            self.emulator.command("reset", timeout=None, deadline=self.total_deadline)
            self.controller.state_changed("reset")
        self.position = self.client.command("init-run", self.run_id, self.work.as_posix(),
            self.settings["debug"],
            self.lua("timing-profile.lua", self.settings["timing_profile"]).as_posix())
        self.lifecycle["checkpoint_engines"] += 1
        self.controller.USER32.SetForegroundWindow(window)
        self.check()

    def set_speed(self, phase):
        if phase not in {"run", "dressing"}:
            raise ValueError("Unknown master speed phase.")
        if phase == self.speed_phase:
            return
        self.check()
        limited = phase == "dressing" and self.settings["dressing"]["frame_limit"]
        self._record_speed(phase, self.emulator.set_frame_limit(limited))

    def _record_speed(self, phase, changed):
        self.speed_phase = phase
        event = {"policy_phase": phase, "speed_percent": 100, "fast_forward": False, **changed}
        self.lifecycle["speed_transitions"].append(event)
        self.record("speed_phase", **event)

    def begin_visit(self, stage, visit):
        self.controller.live_deadline = self.total_deadline
        self.check()
        self.stage, self.section_visit = stage, visit
        self.stage_started = time.monotonic()
        self.stage_deadline = min(self.stage_started + self.settings["stage_timeout_seconds"], self.total_deadline)
        self.controller.live_deadline = self.stage_deadline
        self.active_work = self.work / "visits" / f"s{stage}-{visit:03d}"
        self.active_work.mkdir(parents=True, exist_ok=False)
        return self.active_work

    def start_section(self, plan, **context):
        source = self.boundary["checkpoint"] if self.boundary else None
        source_context = checkpoint_identity(source) if source else None
        config = {**plan, "stage": self.stage, "run_id": self.run_id, "section_visit": self.section_visit,
            "work": self.active_work.as_posix(), "source_frame": source["frame"] if source else self.position["frame"],
            "source_context": source_context, "target": self.target, **context}
        self.json("runner-setup.json", {**config, "shared_host_deadline": self.stage_deadline})
        handoff = self.client.command("begin-section", self.lua("section-config.lua", config).as_posix())
        if handoff["handoff"]["emulator_frames"] != 0 or handoff["frame"] != config["source_frame"]:
            raise RuntimeError("A normal section transition advanced emulator frames.")
        self.json("source-handoff.json", handoff)
        self.check()
        return config, handoff

    def action(self, action, *arguments, expected=None, name=None):
        self.check()
        result = self.emulator.command(action, *arguments, timeout=None, deadline=self.stage_deadline)
        if name:
            self.json(name, result)
        if expected is not None and result.get("stop_reason") != expected:
            raise RuntimeError(f"The {action} section did not reach its configured endpoint.")
        return result

    def command(self, action, *arguments):
        return self.client.command(action, *arguments)

    def protect_slot(self, slot):
        if slot not in self.originals:
            native = self.emulator.states.path(slot)
            previous = self.work / "slot-backups" / f"previous-slot-{slot}.ds{slot % 10}" if native.exists() else None
            if previous is not None:
                previous.parent.mkdir(exist_ok=True)
                shutil.copy2(native, previous)
            self.originals[slot] = previous

    def acknowledge(self, response, *, slot):
        saving = response["checkpoint"].get("save_required")
        if not isinstance(saving, bool):
            raise RuntimeError("The contest checkpoint must declare its save policy.")
        if saving and not self.native_saves_enabled:
            raise RuntimeError("Quick saves are disabled for this run.")
        previous = None
        if saving:
            StateSlots.validate_slot(slot)
            self.protect_slot(slot)
            previous = self.accepted_slots.get(slot) or self.originals[slot]
        accepted = self.client.acknowledge(response, slot=slot if saving else None,
                                           previous_state=str(previous) if previous else None)
        saved = accepted["host_savestate"]
        if saved is not None:
            self.accepted_slots[slot] = Path(saved["retained_path"])
        self.boundary = {"checkpoint": accepted["checkpoint"], "save_state": saved, "receipt": accepted["host_receipt"]}
        return accepted

    def review(self, capture, expected, *, filename):
        image = self.active_work / filename
        self.controller.screenshot(image)
        request = {"run_id": self.run_id, "frame": capture["frame"], "epoch": capture["epoch"],
            "stage": self.stage, "section_visit": self.section_visit, "image": str(image),
            "review_path": str(self.active_work / "review.json"), "expected": expected,
            "automatic_check": capture["observation"]["automatic_check"]}
        self.json("review-request.json", request)
        review = automatic_review(request, request["automatic_check"])
        if not review:
            self.record("endpoint_unreadable", review_request=str(self.active_work / "review-request.json"), image=str(image))
            name = "loaded_overworld_screenshot" if self.stage == 1 else "dress_up_entry_screenshot"
            raise RuntimeError(CHECK_MESSAGES[name])
        self.record("endpoint_review", review_request=str(self.active_work / "review-request.json"), image=str(image))
        self.check()
        return review, image

    def finalize(self, *, error=None, emulator_closed=False):
        if self.finalized:
            raise RuntimeError("Terminal native cleanup may run only once.")
        self.finalized = True
        self.controller.live_deadline = None
        self.controller.live_session = None
        temporary_slot = self.settings["save_policy"]["temporary_slot"]
        result = {"errors": []}
        if emulator_closed:
            result.update(emulator_closed=True, emulator_paused=None, inputs_released=None)

        def attempt(name, action):
            try:
                result[name] = action()
                return True
            except (Exception, SystemExit, KeyboardInterrupt) as cleanup_error:
                result["errors"].append(abort_message(cleanup_error, stage=self.stage))
                return False

        if self.owned:
            self.lifecycle["terminal_cleanups"] += 1
        if self.owned and not emulator_closed:
            attempt("pause_finish", lambda: self.controller.set_pause(True))
            if self.attached:
                if error:
                    attempt("lua_failure", lambda: self.client.command("stage-failed", self.stage or 1, self.section_visit or 0))

                    def stopped_evidence():
                        slot = temporary_slot if self.settings["debug"] == 1 else None
                        if slot is not None:
                            self.protect_slot(slot)
                        retained = self.client.stop(error, slot=slot)
                        self.controller.screenshot(self.active_work / "failure.png")
                        return retained

                    attempt("stopped_evidence", stopped_evidence)
                attempt("lua_finish", lambda: self.client.command("terminal"))
            attempt("movie_finish", self.emulator.stop_movie)
            attempt("speed_finish", self.emulator.restore_normal_speed)
            if self.rendering_before is not None:
                def restore_rendering():
                    window = self.controller.session()["window"]
                    item = self.rendering_before["skip_menu"]
                    if item is not None and not self.emulator.menu_state(item) & 0x8:
                        self.controller.send(window, 0x111, item)
                    if bool(self.emulator.menu_state(221) & 0x8) != self.rendering_before["automatic"]:
                        self.controller.send(window, 0x111, 221)
                    return True

                attempt("rendering_restored", restore_rendering)

            def confirm_endpoint():
                final = self.controller.status() if self.attached else {}
                result.update(frame=final.get("frame"), emulator_paused=self.controller.paused(self.controller.session()),
                    inputs_released=not self.attached or final.get("buttons") == "" and not final.get("stylus_touch", False))
                if not result["emulator_paused"] or not result["inputs_released"]:
                    raise RuntimeError("The master did not finish paused with released buttons and stylus.")
                return True

            attempt("endpoint_confirmed", confirm_endpoint)
            if self.attached and result["errors"] and not result.get("stopped_evidence"):
                if "lua_failure" not in result:
                    attempt("lua_failure", lambda: self.client.command("stage-failed", self.stage or 1, self.section_visit or 0))

                def retain_cleanup_failure():
                    slot = temporary_slot if self.settings["debug"] == 1 else None
                    if slot is not None:
                        self.protect_slot(slot)
                    self.controller.set_pause(True)
                    retained = self.client.stop("; ".join(result["errors"]), slot=slot)
                    self.controller.screenshot(self.active_work / "failure.png")
                    return retained

                attempt("cleanup_failure_evidence", retain_cleanup_failure)
        for slot, original in self.originals.items():
            try:
                source = self.accepted_slots.get(slot) if slot != temporary_slot else original
                source = source or original
                native = self.emulator.states.path(slot, data={"rom": str(self.controller.ROM)}) if emulator_closed else self.emulator.states.path(slot)
                if source is not None:
                    if not native.is_file() or not filecmp.cmp(source, native, shallow=False):
                        shutil.copy2(source, native)
                else:
                    native.unlink(missing_ok=True)
                if slot == temporary_slot:
                    result[f"temporary_slot_{slot}_restored"] = True
            except (Exception, SystemExit) as cleanup_error:
                result["errors"].append(abort_message(cleanup_error, stage=self.stage))
        result["lifecycle"] = self.lifecycle
        return result
