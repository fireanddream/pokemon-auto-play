"""Contest deadlines, standalone helpers, and the master-owned persistent runtime."""

import filecmp
from pathlib import Path
import shutil
import time

from host.controller import Controller, ControllerConfig
from host.checkpoints import CheckpointClient, write_json
from host.emulator import DeSmuME
from host.savestates import StateSlots


ROOT = Path(__file__).resolve().parents[1]
ROM = ROOT / "desmume-0.9.13-win64/Roms/Pocket Monsters - Platinum (Japan).nds"
ACTING_RETRY_SLOT = 10
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
            raise TimeoutError("The agreed contest-stage host deadline expired.")
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
    def save(self, slot, *, expected=None, timeout=20):
        remaining = self.controller.remaining()
        limit = min(20 if timeout is None else timeout, remaining)
        return super().save(slot, expected=expected, timeout=limit)


def setup_speed(emulator, *, inherited_uncapped=False):
    if inherited_uncapped:
        return {**emulator.set_frame_limit(False), "source": "accepted_chain_speed"}
    return emulator.restore_normal_speed()


def finish_speed(emulator, *, handoff=False):
    return emulator.set_frame_limit(False) if handoff else emulator.restore_normal_speed()


def automatic_review(request, check):
    """Only readable local pixel evidence can bypass the external paused review."""
    if check.get("source") != "local_reference_pixels" or check.get("confirmed") is not True:
        return False
    review = {key: request[key] for key in ("run_id", "frame", "epoch")}
    review.update(verdict="pass", source="local_reference_pixels", pixel_check=check,
                  note=f"Fresh paused {check['expected']} pixels match the reviewed native reference.")
    write_json(Path(request["review_path"]), review)
    return True


class ContestRuntime:
    """One connection/client/lifecycle; callable sections own only gameplay actions."""

    def __init__(self, run_id, work, settings, started, *, controller=None):
        self.run_id, self.work, self.settings, self.started = run_id, Path(work), settings, started
        self.total_deadline = started + settings["total_timeout_seconds"] if settings.get("host_timeouts_enabled", True) else None
        self.controller = controller or TimedController(ControllerConfig(rom=ROM))
        self.controller.live_deadline = self.total_deadline
        self.emulator = DeSmuME(self.controller)
        self.emulator.states = TimedSlots(self.controller)
        self.client = CheckpointClient(self.emulator, self.work, run_id)
        self.owned = self.attached = self.finalized = False
        self.originals, self.accepted_slots, self.retry_sources = {}, {}, {}
        self.boundary, self.loaded_slot, self.position = None, None, None
        self.target, self.party, self.overworld = None, None, None
        self.bag_context = {"opened_once": False, "cursor_position": 1}
        self.dance_handoff = None
        self.active_work, self.stage, self.section_visit = self.work, None, None
        self.stage_started, self.stage_deadline = started, self.total_deadline
        self.rendering_before, self.speed_phase = None, None
        self.artifacts = None
        self.lifecycle = {"lua_attachments": 0, "checkpoint_engines": 0, "retry_loads": [],
                          "speed_transitions": [], "terminal_cleanups": 0}
        self.on_progress = None

    def confirm_ownership(self):
        owners = set(self.controller.emulator_process_ids()) | set(self.controller.emulator_window_process_ids())
        if owners:
            session = self.controller.session()
            if owners != {session["pid"]} or Path(session["rom"]).resolve() != ROM.resolve():
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

    def register(self, path, *, kind="scratch", protected=False):
        if self.artifacts is not None:
            self.artifacts.register(path, kind=kind, protected=protected)
        return Path(path)

    def json(self, name, value):
        path = self.active_work / name
        write_json(path, value)
        return path

    def lua(self, name, value):
        from contest.dance_master import lua_value

        path = self.active_work / name
        path.write_text("return " + lua_value(value) + "\n", encoding="utf-8")
        return self.register(path, kind="generated_configuration")

    def setup(self, *, resume_measurement=False):
        from contest.dance_master import attach

        self.check()
        with ROM.open("rb") as stream:
            stream.seek(12)
            if stream.read(4) != b"CPUJ":
                raise ValueError("The integrated contest runner requires Japanese Platinum.")
        battery = self.controller.EMULATOR / "Battery" / f"{ROM.stem}.dsv"
        if not battery.is_file() or not battery.stat().st_size:
            raise ValueError("The prepared Platinum battery save is missing or empty.")
        existing = self.confirm_ownership()
        if resume_measurement and (existing is None or self.settings.get("mode") != "measurement"
                or not self.settings.get("resume_paused")):
            raise RuntimeError("Paused continuation belongs to an existing measurement emulator.")
        if existing is None:
            self.controller.launch(ROM)
            self.owned = True
        self.controller.live_session = self.controller.session()
        self.controller.set_pause(True)
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
        attach(self.controller, ROOT / "contest" / "super_contest.lua", bridge="pt-super-contest")
        self.attached = True
        self.lifecycle["lua_attachments"] += 1
        session = self.controller.session()
        self.controller.live_session = session
        if self._one_lua_window(session) != [session["lua_window"]]:
            raise RuntimeError("The persistent bridge is not the sole Lua window.")
        self.lifecycle["lua_window"] = session["lua_window"]
        if existing is not None and not resume_measurement:
            self.emulator.command("reset", timeout=None, deadline=self.total_deadline)
            self.controller.state_changed("reset")
        self.position = self.client.command("init-run", self.run_id, self.work.as_posix(),
            self.settings["true_retry"], self.settings["debug"], self.settings["dance_observation_policy"])
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
        self.stage_deadline = min(self.stage_started + self.settings["stage_timeout_seconds"], self.total_deadline) \
            if self.total_deadline is not None else None
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
        self.loaded_slot = None
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
                self.register(previous, kind="slot_backup", protected=True)
            self.originals[slot] = previous

    def acknowledge(self, response, *, slot):
        saving = response["checkpoint"].get("save_required", True)
        previous = None
        if saving:
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
        from contest.stage1_load import await_review

        image = self.register(self.active_work / filename, kind="capture")
        self.controller.screenshot(image)
        request = {"run_id": self.run_id, "frame": capture["frame"], "epoch": capture["epoch"],
            "stage": self.stage, "section_visit": self.section_visit, "image": str(image),
            "review_path": str(self.active_work / "review.json"), "expected": expected,
            "automatic_check": capture["observation"]["automatic_check"]}
        self.json("review-request.json", request)
        if not automatic_review(request, request["automatic_check"]):
            self.record("endpoint_unreadable", review_request=str(self.active_work / "review-request.json"), image=str(image))
            raise RuntimeError(f"Stage {self.stage} screen check failed; stopped paused for review: {image}")
        self.record("endpoint_review", review_request=str(self.active_work / "review-request.json"), image=str(image))
        return await_review(self.controller, request), image

    def retain_retry(self, slot):
        context = {"target": self.target, "bag_context": dict(self.bag_context), "source_party": self.party}
        source = {**self.boundary, "context": context}
        path = self.lua(f"retry-slot-{slot}.lua", context)
        self.client.command("retain-context", slot, path.as_posix())
        self.retry_sources[slot] = source
        write_json(self.work / "retry-context.json", {str(key): {
            "source_context": checkpoint_identity(value["checkpoint"]), "save_state": value["save_state"],
            "source_checkpoint": value["receipt"], "context": value["context"]}
            for key, value in self.retry_sources.items()})

    def retry(self, slot):
        if self.settings["true_retry"] != 0 or slot != ACTING_RETRY_SLOT:
            raise ValueError(f"Only true_retry = 0 may reload the accepted slot {ACTING_RETRY_SLOT}.")
        source = self.retry_sources[slot]
        checkpoint = source["checkpoint"]
        native, retained = self.emulator.states.path(slot), Path(source["save_state"]["retained_path"])
        if not native.is_file() or not filecmp.cmp(native, retained, shallow=False):
            raise RuntimeError(f"Native retry slot {slot} differs from its accepted paired checkpoint.")
        armed = self.client.command("arm-retry", slot, checkpoint["visit"])
        self.emulator.states.load(slot, refresh=False, timeout=None, checkpoint_visit=checkpoint["visit"])
        self.position = self.controller.status()
        if self.position["frame"] != checkpoint["frame"] or self.position["checkpoint_epoch"] != armed["epoch"] + 1:
            raise RuntimeError("The retry did not restore its paired frame and create a new load epoch.")
        self.boundary, self.loaded_slot = source, slot
        context = source["context"]
        self.target, self.party, self.bag_context = context["target"], context["source_party"], dict(context["bag_context"])
        event = {"slot": slot, "source_visit": checkpoint["visit"], "frame": checkpoint["frame"],
            "epoch": self.position["checkpoint_epoch"], "target": self.target}
        self.lifecycle["retry_loads"].append(event)
        self.record("retry_load", **event)
        return source

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
                result["errors"].append(str(cleanup_error) or type(cleanup_error).__name__)
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
                        self.controller.screenshot(self.register(self.active_work / "failure.png", kind="capture"))
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
                    self.controller.screenshot(self.register(self.active_work / "failure.png", kind="capture"))
                    return retained

                attempt("cleanup_failure_evidence", retain_cleanup_failure)
        for slot, original in self.originals.items():
            try:
                source = self.accepted_slots.get(slot) if slot != temporary_slot else original
                source = source or original
                native = self.emulator.states.path(slot, data={"rom": str(ROM)}) if emulator_closed else self.emulator.states.path(slot)
                if source is not None:
                    if not native.is_file() or not filecmp.cmp(source, native, shallow=False):
                        shutil.copy2(source, native)
                else:
                    native.unlink(missing_ok=True)
                if slot == temporary_slot:
                    result[f"temporary_slot_{slot}_restored"] = True
            except (Exception, SystemExit) as cleanup_error:
                result["errors"].append(str(cleanup_error))
        result["lifecycle"] = self.lifecycle
        return result
