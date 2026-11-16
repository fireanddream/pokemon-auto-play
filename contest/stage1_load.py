"""Load CPUJ for the contest, then await one paused screenshot verdict."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.checkpoints import CheckpointClient, CheckpointStopped, write_json
from host.controller import ControllerConfig
from host.emulator import DeSmuME
from host.paths import ROOT, project_path
from contest.runtime import TimedController, TimedSlots, automatic_review, finish_speed
from contest.timing_profiles import load_profile, section, setting


HERE = Path(__file__).resolve().parent
ROM = ROOT / "desmume-0.9.13-win64/Roms/Pocket Monsters - Platinum (Japan).nds"
START_WAIT_FRAMES = section(None, "stage1_loading")["start_release_frames"]


def load_plan(*, boot_wait_frames=None, hold_frames=None, start_wait_frames=None,
              a_wait_frames=None, b_wait_frames=None, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    values = section(profile, "stage1_loading")
    boot_wait_frames = setting(boot_wait_frames, values, "boot_wait_frames")
    hold_frames = setting(hold_frames, values, "button_hold_frames")
    start_wait_frames = setting(start_wait_frames, values, "start_release_frames")
    a_wait_frames = setting(a_wait_frames, values, "load_save_a_release_frames")
    b_wait_frames = setting(b_wait_frames, values, "dismiss_prompt_b_release_frames")
    timing = {"boot_wait_frames": boot_wait_frames, "hold_frames": hold_frames,
              "start_wait_frames": start_wait_frames, "a_wait_frames": a_wait_frames,
              "b_wait_frames": b_wait_frames}
    for name, value in timing.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive whole emulator-frame count.")
    return {"stage": 1, "timing_profile_source": profile["source"], "timing": timing, "inputs": [f"wait:{boot_wait_frames}",
        f"start:{hold_frames}:{start_wait_frames}", f"start:{hold_frames}:{start_wait_frames}",
        f"A:{hold_frames}:{a_wait_frames}", f"B:{hold_frames}:{b_wait_frames}"],
        "total_emulator_frames": boot_wait_frames + 4 * hold_frames + 2 * start_wait_frames
            + a_wait_frames + b_wait_frames,
        "uncapped": True, "attempts": 1, "guards": {}, "checkpoint_slot": 1,
        "acceptance": "Loaded contest-hall overworld facing the middle receptionist.",
        "review_source": "visual_review", "memory_proven_phase": False}


def attach(controller):
    session = controller.session()
    dialog = session.get("lua_window", 0)
    if controller.USER32.IsWindow(dialog):
        stop = controller.USER32.GetDlgItem(dialog, 312)
        if stop and controller.USER32.IsWindowEnabled(stop):
            controller.send(dialog, 0x111, 312, stop)
            while controller.USER32.IsWindowEnabled(stop):
                controller.remaining()
                time.sleep(0.01)
    started = time.time_ns()
    controller.load_lua(HERE / "stage1_load.lua")
    dialog = controller.session()["lua_window"]
    stop = controller.USER32.GetDlgItem(dialog, 312)
    if not controller.USER32.IsWindowEnabled(stop):
        controller.send(dialog, 0x111, 311, controller.USER32.GetDlgItem(dialog, 311))
    while True:
        controller.remaining()
        try:
            fresh = controller.STATUS.stat().st_mtime_ns >= started
            status = json.loads(controller.STATUS.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
            fresh, status = False, {}
        if fresh and status.get("bridge") == "pt-contest-stage1" and status.get("checkpoint_protocol") == 1:
            controller.set_pause(True)
            return
        if not controller.USER32.IsWindowEnabled(stop):
            raise RuntimeError("The Stage 1 Lua bridge stopped before reporting ready; inspect its console.")
        time.sleep(0.05)


def await_review(controller, request):
    path = Path(request["review_path"])
    while not path.is_file():
        controller.remaining()
        if not controller.paused(controller.session()):
            controller.set_pause(True)
            raise RuntimeError("Native pause was lost during the screenshot review.")
        time.sleep(0.2)
    review = json.loads(path.read_text(encoding="utf-8"))
    if any(review.get(key) != request[key] for key in ("run_id", "frame", "epoch")):
        raise ValueError("The screenshot verdict belongs to a different paused boundary.")
    controller.remaining()
    if review.get("verdict") not in {"pass", "fail"} or review.get("source") not in {"visual_review", "local_reference_pixels"}:
        raise ValueError("Supply a pass/fail verdict with its actual visual source.")
    note = review.get("note")
    if not isinstance(note, str) or not note.strip() or any(char in note for char in "|\r\n"):
        raise ValueError("The screenshot verdict needs a single-line visual explanation.")
    return review


def run(*, run_name, rom=ROM, host_timeout=None, deadline=None, auto_review=False, keep_uncapped=False, **timing):
    started = time.monotonic()
    if host_timeout is not None and (isinstance(host_timeout, bool) or not isinstance(host_timeout, (int, float))
                                     or not 0 < host_timeout < float("inf")):
        raise ValueError("The Stage 1 host timeout must be positive finite seconds.")
    plan = load_plan(**timing)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    rom = project_path(rom)
    with rom.open("rb") as stream:
        stream.seek(12)
        if stream.read(4) != b"CPUJ":
            raise ValueError("Stage 1 requires Japanese Platinum (CPUJ).")
    controller = TimedController(ControllerConfig(rom=rom))
    controller.live_deadline = min(value for value in
        (started + host_timeout if host_timeout is not None else float("inf"),
         deadline if deadline is not None else float("inf")))
    if controller.live_deadline == float("inf"):
        controller.live_deadline = None
    emulator = DeSmuME(controller)
    emulator.states = TimedSlots(controller)
    battery = controller.EMULATOR / "Battery" / f"{rom.stem}.dsv"
    if not battery.is_file() or not battery.stat().st_size:
        raise ValueError("The prepared Platinum battery save is missing or empty.")
    work = ROOT / "tmp" / "contest" / run_name
    work.mkdir(parents=True, exist_ok=False)
    (work / "checkpoints").mkdir()
    setup = {**plan, "run_id": run_name, "host_pid": os.getpid(), "rom": str(rom),
             "battery_save": str(battery), "work_dir": str(work),
             "host_timeout_seconds": host_timeout, "shared_host_deadline": deadline}
    write_json(work / "runner-setup.json", setup)
    result = {"outcome": "fail", "run_id": run_name, "work_dir": str(work), "stage": 1,
              "attempts": 1, "guards": {"host_timeout_seconds": host_timeout} if host_timeout is not None else {},
              "uncapped": True}
    client = CheckpointClient(emulator, work, run_name)
    owned, attached, slot_touched = False, False, False
    original_slot = None
    slot_path = emulator.states.path(1, data={"rom": str(rom)})

    def record(phase, **details):
        event = {"phase": phase, "host_elapsed_seconds": time.monotonic() - started, **details}
        write_json(work / "runner-status.json", event)
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    with controller.without_timeouts():
        try:
            controller.remaining()
            record("setup")
            owners = set(controller.emulator_process_ids()) | set(controller.emulator_window_process_ids())
            if owners:
                session = controller.session()
                if owners != {session["pid"]} or project_path(session["rom"]) != rom:
                    raise RuntimeError("Stage 1 requires the sole controlled Platinum emulator.")
            else:
                controller.launch(rom)
            owned = True
            controller.set_pause(True)
            emulator.stop_movie()
            shutil.copy2(battery, work / "prepared-battery.dsv")
            if slot_path.exists():
                original_slot = work / "previous-slot-1.ds1"
                shutil.copy2(slot_path, original_slot)
            result["speed_setup"] = emulator.restore_normal_speed()
            result["speed_setup"].update(emulator.set_frame_limit(False))
            attach(controller)
            attached = True
            if owners:
                emulator.command("reset", timeout=None, deadline=controller.live_deadline)
                controller.state_changed("reset")
            client.command("init", run_name, (work / "checkpoint-events.jsonl").as_posix())
            record("loading", inputs=plan["inputs"], uncapped=True)
            loaded = emulator.command("combo", "inputs", *plan["inputs"], timeout=None, deadline=controller.live_deadline)
            write_json(work / "input-result.json", loaded)
            if loaded.get("stop_reason") != "combo_complete":
                raise RuntimeError("The loading input sequence did not reach its endpoint.")
            result["input_frames"] = loaded["frame"] - loaded["command_start_frame"]
            capture = client.command("capture")
            image = work / "stage1.png"
            emulator.screenshot(str(image))
            shutil.copy2(controller.config.mailbox_dir / "screen.gd", work / "stage1.gd")
            request = {"run_id": run_name, "frame": capture["frame"], "epoch": capture["epoch"],
                       "image": str(image), "review_path": str(work / "review.json"),
                       "expected": plan["acceptance"]}
            request["automatic_check"] = capture["observation"]["automatic_check"]
            result.update({"frame": capture["frame"], "screenshot": str(image)})
            write_json(work / "review-request.json", request)
            if auto_review:
                automatic_review(request, request["automatic_check"])
            record("awaiting_review", **request)
            review = await_review(controller, request)
            result["screen_evidence"] = review
            pending = client.command("visit", review["verdict"], review["source"], image.as_posix(),
                                     review["note"], review["frame"], review["epoch"])
            record("checkpoint", verdict=review["verdict"], frame=pending["frame"])
            slot_touched = True
            accepted = client.acknowledge(pending, slot=1,
                                         previous_state=str(original_slot) if original_slot else None)
            result.update({"outcome": "pass", "checkpoint": accepted["checkpoint"],
                            "save_state": accepted["host_savestate"]})
            controller.remaining()
        except (Exception, SystemExit, KeyboardInterrupt) as error:
            result["outcome"] = "timeout" if isinstance(error, TimeoutError) else "fail"
            result["error"] = str(error) or type(error).__name__
            controller.live_deadline = None
            if isinstance(error, CheckpointStopped):
                result["checkpoint"] = error.record
            if owned:
                try:
                    controller.set_pause(True)
                    if not result.get("checkpoint", {}).get("savestate"):
                        slot_touched = True
                        if attached:
                            result["stopped_checkpoint"] = client.stop(result["error"], slot=1)
                            emulator.screenshot(str(work / "failure.png"))
                        else:
                            saved = emulator.states.save(1, timeout=None)
                            retained = work / "checkpoints" / "setup-stop.ds1"
                            shutil.copy2(saved["path"], retained)
                            result["stopped_state"] = {**saved, "retained_path": str(retained)}
                except (Exception, SystemExit) as evidence_error:
                    result["evidence_error"] = str(evidence_error)
        finally:
            controller.live_deadline = None
            cleanup = {"errors": []}
            if owned:
                for action in (emulator.stop_movie,
                               lambda: finish_speed(emulator, handoff=keep_uncapped and result["outcome"] == "pass")):
                    try:
                        cleanup.update(action())
                    except (Exception, SystemExit, KeyboardInterrupt) as error:
                        cleanup["errors"].append(str(error) or type(error).__name__)
                try:
                    result["emulator_paused"] = controller.paused(controller.session())
                    if not result["emulator_paused"]:
                        controller.set_pause(True)
                        raise RuntimeError("Native pause was lost during finalization.")
                except (Exception, SystemExit) as error:
                    cleanup["errors"].append(str(error))
            if cleanup["errors"]:
                result["outcome"] = "fail"
                result.setdefault("error", "; ".join(cleanup["errors"]))
            if slot_touched and result["outcome"] != "pass":
                try:
                    if original_slot is not None:
                        shutil.copy2(original_slot, slot_path)
                    else:
                        slot_path.unlink(missing_ok=True)
                    cleanup["previous_slot_1_restored"] = True
                except OSError as error:
                    cleanup["errors"].append(str(error))
            result["cleanup"] = cleanup
            result["host_elapsed_seconds"] = time.monotonic() - started
            write_json(work / "runner-result.json", result)
            record("complete", outcome=result["outcome"], error=result.get("error"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--rom", default=str(ROM))
    for name in ("boot-wait-frames", "hold-frames", "start-wait-frames", "a-wait-frames", "b-wait-frames"):
        parser.add_argument(f"--{name}", type=int, help="Override the JPN timing profile (emulator frames).")
    args = parser.parse_args()
    timing = {key: value for key, value in vars(args).items() if key.endswith("_frames")}
    try:
        if args.plan_only:
            print(json.dumps(load_plan(**timing), indent=2))
            return 0
        if not args.run_name:
            parser.error("--execute requires a fresh --run-name.")
        result = run(run_name=args.run_name, rom=args.rom, **timing)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0 if result["outcome"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
