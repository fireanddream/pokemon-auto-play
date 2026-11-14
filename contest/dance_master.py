"""Six-dance CPUJ master: measured timing, live pixels, inputs, and feedback."""

from __future__ import annotations

import argparse
import ast
import filecmp
import json
from pathlib import Path
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from contest.runtime import ROM, TimedController, TimedSlots
from host.avi import AviRecording
from host.checkpoints import CheckpointClient, write_json
from host.controller import ControllerConfig
from host.emulator import DeSmuME
from contest.pt_dance_read import DemoAccumulator, ProgressTracker, analyze, gd_image, layout_for


SOURCE_FRAME = 9791
FIRST_HANDOFF_OFFSET = 193
DANCE_SPACING = 432
POKEMON_SWITCH_EXTRA = 213
HALF_FRAMES = 216
DEMO_SAMPLE_FRAMES = 6
DEFAULT_OFFSET_FRAMES = -6


def dance_schedule(*, demo_start_frame=None, count=6):
    """Use the same frame offsets for a prepared or live-entry timing anchor."""
    if isinstance(count, bool) or not isinstance(count, int) or count not in (6, 8):
        raise ValueError("Schedule six responses or all eight dances.")
    if demo_start_frame is None:
        demo_start_frame = SOURCE_FRAME + FIRST_HANDOFF_OFFSET - HALF_FRAMES
    if isinstance(demo_start_frame, bool) or not isinstance(demo_start_frame, int):
        raise ValueError("The demonstration start must be a whole emulator frame.")
    dances = []
    for index in range(count):
        boundary = demo_start_frame + HALF_FRAMES + DANCE_SPACING * index + POKEMON_SWITCH_EXTRA * (index // 2)
        dance = {"index": index + 1, "pokemon": index // 2 + 1,
            "demo_start_frame": boundary - HALF_FRAMES, "boundary_frame": boundary,
            "end_frame": boundary + HALF_FRAMES, "input_policy": "mirror" if index < 6 else "none"}
        if index < 6:
            dance.update(player_center_x=204 - 80 * (index // 2),
                         layout=layout_for(index + 1), sample_frames=DEMO_SAMPLE_FRAMES)
        dances.append(dance)
    return dances


def read_demonstration(dance, samples, read_frame, *, hold_frames=2, offset_frames=DEFAULT_OFFSET_FRAMES, remaining=None):
    """Use counted live captures, remembering each demo's cues through occlusion."""
    start_range = dance.get("demo_start_frame_range")
    if start_range is None:
        start_range = [dance["demo_start_frame"], dance["demo_start_frame"]]
    if (not isinstance(start_range, (list, tuple)) or len(start_range) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in start_range)
            or not start_range[0] <= dance["demo_start_frame"] <= start_range[1]):
        raise ValueError("The demo-start interval must contain its nominal emulator frame.")
    accumulator, tracker = DemoAccumulator(dance["index"]), ProgressTracker(dance["index"])
    previous, first, complete_at, tracked = None, None, None, None
    evidence_frames, visual_frames, count = {}, [], 0
    for sample in samples:
        if remaining is not None:
            remaining()
        frame = sample["frame"]
        if not dance["demo_start_frame"] <= frame <= dance["boundary_frame"]:
            raise ValueError("A demonstration capture is outside its configured dance")
        if previous is not None and frame <= previous:
            raise ValueError("Demonstration capture frames must strictly increase")
        reading = analyze(gd_image(read_frame(sample)), dance["index"])
        tracked = tracker.update(reading, advanced_frames=frame - previous if previous is not None else 0)
        phase_range = [frame - start_range[1], frame - start_range[0]]
        bounds = tracked["frame_range"]
        if bounds is not None and max(bounds[0], phase_range[0]) > min(bounds[1], phase_range[1]):
            raise ValueError(f"Dance {dance['index']} at frame {frame}: pixel progress {bounds} "
                             f"disagrees with the configured demo interval {phase_range}")
        if tracked["visually_confirmed"]:
            visual_frames.append(frame)
        pattern = accumulator.add(reading)
        if reading["progress"]["phase"] == "demo":
            for cue in reading["buttons"]["accepted"]:
                evidence_frames[cue["demo_x"]] = frame
        if pattern["complete"] and complete_at is None:
            complete_at = frame
        first = frame if first is None else first
        previous, count = frame, count + 1
    pattern = accumulator.result()
    if previous != dance["boundary_frame"] or not visual_frames or not pattern["complete"]:
        raise ValueError(f"Dance {dance['index']} needs four observed cues and a complete, visually anchored capture")
    expected_range = [dance["boundary_frame"] - start_range[1], dance["boundary_frame"] - start_range[0]]
    bounds = tracked["frame_range"]
    if bounds is None or max(bounds[0], expected_range[0]) > min(bounds[1], expected_range[1]):
        raise ValueError("The counted visual progress does not reach the configured response boundary")
    presses = [{"button": cue["button"], "demo_x": cue["demo_x"], "response_x": cue["demo_x"] + 120,
        "frame": dance["boundary_frame"] + round((cue["demo_x"] - 8) * 9 / 5) + offset_frames,
        "observed_frame": evidence_frames[cue["demo_x"]]} for cue in pattern["buttons"]]
    for index, press in enumerate(presses):
        next_frame = presses[index + 1]["frame"] if index + 1 < len(presses) else dance["end_frame"]
        if press["frame"] <= dance["boundary_frame"] or press["frame"] + hold_frames >= next_frame:
            raise ValueError("The requested hold/offset does not fit the four observed cue positions")
    return {"dance": dance["index"], "boundary_frame": dance["boundary_frame"],
        "end_frame": dance["end_frame"], "presses": presses, "hold_frames": hold_frames,
        "offset_frames": offset_frames, "visual_evidence": {
            "source": "pt_dance_read_pixels_with_per_dance_accumulator_and_counted_progress",
            "frame": previous, "complete": True, "conflict_policy": "error",
            "timeline_confirmed": True, "expected_frame_range": expected_range,
            "tracked_progress": tracked, "buttons": pattern["buttons"],
            "cue_frames": [{"demo_x": x, "frame": frame} for x, frame in sorted(evidence_frames.items())],
            "first_frame": first, "sample_count": count, "complete_at": complete_at,
            "last_visual_frame": visual_frames[-1]}}


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


def attach(controller, attachment, *, bridge="pt-dance-master"):
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
        if ready and status.get("bridge") == bridge and status.get("checkpoint_protocol") == 1:
            break
        time.sleep(0.01)
    controller.set_pause(True)


def check():
    from tests.support.lua51 import Lua51, literal

    ast.parse(Path(__file__).read_text(encoding="utf-8"))
    lua = Lua51()
    try:
        statements = "; ".join(f"assert(loadfile({literal((HERE / name).as_posix())}))" for name in
            ("dance_master.lua", "dance_pixels.lua", "feedback-templates.lua"))
        print(lua.run(statements + "; return 'Dance master and pixel-reader syntax OK'"))
    finally:
        lua.close()


def settle_native_osd(controller, seconds):
    """Let native messages and the slot indicator expire without running game frames."""
    if seconds:
        controller.set_pause(True)
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            time.sleep(min(controller.remaining(), max(0, until - time.monotonic())))


def response_dances(emulator, client, dances, *, checkpoint_slot, finish_slot,
                    hold_frames=2, offset_frames=DEFAULT_OFFSET_FRAMES, osd_settle_seconds=0,
                    terminal_section="success", first_feedback_only=False, on_progress=None):
    """Run the existing six observed responses, yielding acknowledged results."""
    controller = emulator.controller

    def progress(phase, dance):
        if on_progress is not None:
            on_progress(phase, dance["index"])

    for dance in dances[:6]:
        folder = Path(dance["work"])
        progress("observe-demo", dance)
        settle_native_osd(controller, osd_settle_seconds)
        client.command("begin-dance", dance["index"])
        observed = emulator.command("observe-demo", timeout=None, deadline=controller.live_deadline)
        if observed.get("stop_reason") != "demo_complete" or observed["frame"] != dance["boundary_frame"]:
            raise RuntimeError("The demonstration did not reach its configured response boundary")
        progress("read-demo", dance)
        samples = [json.loads(line) for line in (folder / "demo-frames.jsonl").read_text(encoding="utf-8").splitlines()]
        with (folder / "demo-frames.bin").open("rb") as stream:
            def read_frame(sample):
                stream.seek(sample["capture_offset"])
                return stream.read(sample["capture_bytes"])
            plan = read_demonstration(dance, samples, read_frame, hold_frames=hold_frames,
                                     offset_frames=offset_frames, remaining=controller.remaining)
        write_json(folder / "resolved-plan.json", plan)
        write_json(folder / "config.json", {**dance, **plan})
        (folder / "plan.lua").write_text("return " + lua_value(plan) + "\n", encoding="utf-8")
        client.command("plan")
        progress("start-checkpoint", dance)
        entry = client.acknowledge(client.command("visit", "start"), slot=checkpoint_slot)
        controller.screenshot(folder / "start.png")
        progress("mirror", dance)
        settle_native_osd(controller, osd_settle_seconds)
        client.command("prime")
        mirrored = emulator.command("mirror", timeout=None, deadline=controller.live_deadline)
        if first_feedback_only and dance["index"] == 1:
            if mirrored.get("stop_reason") != "offset_feedback_ready":
                raise RuntimeError("The first-button feedback checkpoint was not reached.")
            progress("offset-checkpoint", dance)
            offset = client.acknowledge(client.command("visit", "offset-check"), slot=checkpoint_slot)
            controller.screenshot(folder / "offset-check.png")
            if offset["next_section"] != "mirror-rest":
                raise RuntimeError("The offset checkpoint did not authorize the remaining response inputs.")
            progress("mirror-rest", dance)
            mirrored = emulator.command("mirror-rest", timeout=None, deadline=controller.live_deadline)
        if mirrored.get("stop_reason") != "mirror_complete":
            raise RuntimeError("The response did not reach its configured endpoint")
        progress("finish-checkpoint", dance)
        finish = client.acknowledge(client.command("visit", "finish"), slot=finish_slot)
        trial = finish["checkpoint"]["snapshot"]["trial"]
        controller.screenshot(folder / "finish.png")
        wanted = terminal_section if dance["index"] == 6 else "next-dance"
        if finish["next_section"] != wanted:
            raise RuntimeError("The acknowledged checkpoint did not authorize the expected next section")
        controller.remaining()
        yield {"response": {"dance": dance["index"], "pokemon": dance["pokemon"],
                   "start_frame": entry["frame"], "end_frame": finish["frame"],
                   "player_position": dance["layout"]["player_position"], "presses": plan["presses"],
                   "judgments": trial["judgments"], "retained_finish": finish["host_savestate"]["retained_path"]},
               "trial": trial, "finish": finish}


def execute(args):
    work = ROOT / "tmp" / "contest" / args.run_name
    work.mkdir(parents=True)
    (work / "checkpoints").mkdir()
    dances = dance_schedule()
    for dance in dances:
        folder = work / f"dance-{dance['index']:02d}"
        folder.mkdir()
        dance["work"] = folder.as_posix()
        dance["presses"] = []
    config = {"run_id": args.run_name, "work": work.as_posix(), "mode": "full",
        "source_frame": SOURCE_FRAME, "dances": dances, "hold_frames": args.hold_frames,
        "offset_frames": args.offset_frames, "presses": [], "end_frame": dances[-1]["end_frame"]}
    write_json(work / "config.json", config)
    attachment = work / "attach.lua"
    attachment.write_text(
        f"assert(loadfile({lua_value((HERE / 'dance_master.lua').as_posix())}))({lua_value(config)})\n", encoding="utf-8")
    controller = TimedController(ControllerConfig(rom=ROM))
    emulator = DeSmuME(controller)
    emulator.states = TimedSlots(controller)
    recording = AviRecording(emulator, args.avi) if args.avi is not None else None
    client = CheckpointClient(emulator, work, args.run_name)
    result = {"status": "preparing", "stage": "ownership", "mode": "full", "work_dir": str(work),
        "input_slot": args.input_slot, "checkpoint_slot": args.checkpoint_slot, "finish_slot": args.finish_slot,
        "host_timeout_seconds": args.host_timeout, "speed_percent": 100,
        "frame_limit": not args.uncapped, "avi_path": str(args.avi) if recording else None, "dances": [],
        "native_osd_settle_host_seconds": args.osd_settle_seconds,
        "hold_emulator_frames": args.hold_frames, "offset_emulator_frames": args.offset_frames,
        "timing": {"source_frame": SOURCE_FRAME, "first_handoff_offset_frames": FIRST_HANDOFF_OFFSET,
            "same_pokemon_spacing_frames": DANCE_SPACING, "extra_switch_frames": POKEMON_SWITCH_EXTRA,
            "response_frames": HALF_FRAMES, "demo_sample_frames": DEMO_SAMPLE_FRAMES},
        "acceptance": "Six completed responses with four fresh Excellent judgments each."}
    owned, attached, rendering_before = False, False, None
    originals = {}
    source = emulator.states.path(args.input_slot, data={"rom": str(ROM)})
    saved_source = work / "source-slot-5.ds5"
    started = time.monotonic()
    controller.live_deadline = started + args.host_timeout
    controller.timeouts_enabled = False
    try:
        emulator.states.validate_header(source, args.input_slot)
        timing_source = HERE / "states/source-slot-5.ds5"
        if not filecmp.cmp(source, timing_source, shallow=False):
            raise RuntimeError("Slot 5 differs from the prepared state used for the hard-coded dance timing")
        shutil.copy2(source, saved_source)
        owners = set(controller.emulator_process_ids()) | set(controller.emulator_window_process_ids())
        if owners:
            session = controller.session()
            if owners != {session["pid"]} or Path(session["rom"]).resolve() != ROM.resolve():
                raise RuntimeError("The controlled emulator ownership does not match the full dance run")
        else:
            controller.launch(ROM)
        owned = True
        for slot in {args.checkpoint_slot, args.finish_slot}:
            destination = emulator.states.path(slot)
            backup = work / f"original-slot-{slot}.ds{slot % 10}" if destination.exists() else None
            if backup is not None:
                shutil.copy2(destination, backup)
            originals[slot] = (destination, backup)
        controller.set_pause(True)
        emulator.stop_movie()
        rendering_before = {"automatic": bool(emulator.menu_state(221) & 0x8),
            "skip_menu": next((item for item in range(191, 201) if emulator.menu_state(item) & 0x8), None)}
        window = controller.session()["window"]
        if rendering_before["automatic"]:
            controller.send(window, 0x111, 221)
        if not emulator.menu_state(191) & 0x8:
            controller.send(window, 0x111, 191)
        if emulator.menu_state(221) & 0x8 or not emulator.menu_state(191) & 0x8:
            raise RuntimeError("Every-frame rendering was not confirmed")
        result["speed_setup"] = emulator.restore_normal_speed()
        result["stage"] = "attach"
        attach(controller, attachment)
        attached = True
        result["stage"] = "load"
        emulator.load_state(args.input_slot, refresh=False)
        result["source_frame"] = client.command("inspect")["frame"]
        if result["source_frame"] != SOURCE_FRAME:
            raise RuntimeError("The loaded slot 5 frame does not match the prepared timing origin")
        if args.uncapped:
            result["speed_setup"].update(emulator.set_frame_limit(False))
        controller.USER32.SetForegroundWindow(window)
        if recording is not None:
            result["stage"] = "record-avi"
            client.command("arm-avi-refresh")
            result["avi_start"] = recording.start(deadline=controller.live_deadline)
            result["avi_start"].update(source_frame=result["source_frame"],
                source_emulator_paused=True, source="fresh_paused_checkpoint_before_native_avi_dialog")
        result["stage"] = "refresh"
        if recording is None:
            refresh = emulator.command("advance", 2, deadline=controller.live_deadline)
        else:
            controller.USER32.SetForegroundWindow(window)
            resume_requested = False
            while True:
                controller.remaining()
                refresh = controller.status()
                if refresh.get("last_action") == "avi-refresh" and refresh.get("command_done") and refresh["emulator_paused"]:
                    if refresh.get("error"):
                        raise RuntimeError(refresh["error"])
                    break
                if refresh["emulator_paused"] and not resume_requested:
                    frame = client.command("inspect")["frame"]
                    if not SOURCE_FRAME <= frame < SOURCE_FRAME + 2:
                        raise RuntimeError("AVI setup paused outside the armed display-refresh interval")
                    # A pending native/Lua pause can stop AVI's automatic resume early.
                    # Continue the already armed refresh once; Lua owns its exact endpoint.
                    controller.post_menu(window, 113)
                    result["avi_start"]["resume_frame"] = frame
                    resume_requested = True
                time.sleep(0.01)
        result["refresh_command_start_frame"] = refresh["command_start_frame"]
        result["refresh_emulator_frames"] = refresh["frame"] - SOURCE_FRAME
        if refresh["frame"] != SOURCE_FRAME + 2:
            raise RuntimeError("The two-frame display refresh changed the timing origin")
        client.command("init")
        controller.USER32.SetForegroundWindow(window)
        result["setup_host_seconds"] = time.monotonic() - started
        def on_progress(phase, index):
            result["stage"], result["active_dance"] = phase, index
            write_json(work / "runner-progress.json", result)

        for completed in response_dances(emulator, client, dances, checkpoint_slot=args.checkpoint_slot,
                finish_slot=args.finish_slot, hold_frames=args.hold_frames, offset_frames=args.offset_frames,
                osd_settle_seconds=args.osd_settle_seconds, on_progress=on_progress):
            trial, finish = completed["trial"], completed["finish"]
            result["dances"].append(completed["response"])
            result.update(completed_dances=len(result["dances"]), input_count=trial["total_input_count"],
                excellent_count=trial["total_excellent_count"], end_frame=finish["frame"],
                retained_finish=finish["host_savestate"]["retained_path"])
            write_json(work / "runner-progress.json", result)
        controller.screenshot(work / "finish.png")
        result["status"], result["stage"] = "success", "paused-success"
    except BaseException as error:
        result["status"], result["error"] = "stopped", str(error) or type(error).__name__
    finally:
        result["live_host_seconds"] = time.monotonic() - started
        controller.live_deadline, controller.timeouts_enabled = None, True
        errors = []
        if owned:
            if recording is not None and recording.requested:
                try:
                    recording.cancel_setup()
                except BaseException as error:
                    errors.append(str(error) or type(error).__name__)
            try:
                controller.set_pause(True)
                if attached and result["status"] != "success":
                    result["stopped_evidence"] = client.stop(result.get("error", "Stopped"), slot=args.finish_slot)
                    controller.screenshot(work / "stopped.png")
            except BaseException as error:
                errors.append(str(error) or type(error).__name__)
            try:
                emulator.stop_movie()
                result["speed_finish"] = emulator.restore_normal_speed()
                result["emulator_paused"] = controller.paused(controller.session())
                if attached:
                    result["inputs_released"] = controller.status().get("buttons") == ""
            except BaseException as error:
                errors.append(str(error) or type(error).__name__)
            if recording is not None and recording.requested:
                try:
                    result["avi_finish"] = recording.stop()
                    result["avi_finish"]["frame"] = controller.status()["frame"]
                except BaseException as error:
                    errors.append(str(error) or type(error).__name__)
        if rendering_before is not None:
            try:
                item = rendering_before["skip_menu"]
                if item is not None and not emulator.menu_state(item) & 0x8:
                    controller.send(window, 0x111, item)
                if bool(emulator.menu_state(221) & 0x8) != rendering_before["automatic"]:
                    controller.send(window, 0x111, 221)
                result["rendering_restored"] = True
            except BaseException as error:
                errors.append(str(error) or type(error).__name__)
        for slot, (destination, backup) in originals.items():
            try:
                if backup is not None:
                    shutil.copy2(backup, destination)
                else:
                    destination.unlink(missing_ok=True)
            except BaseException as error:
                errors.append(f"Slot {slot} restoration: {str(error) or type(error).__name__}")
        result["temporary_slots_restored"] = not errors and bool(originals)
        if saved_source.exists():
            result["source_slot_5_preserved"] = filecmp.cmp(source, saved_source, shallow=False)
            if not result["source_slot_5_preserved"]:
                errors.append("The prepared source slot changed during the run")
        if errors:
            result["cleanup_errors"] = errors
        result["total_host_seconds"] = time.monotonic() - started
        write_json(work / "runner-result.json", result)
    print(json.dumps({key: result[key] for key in ("status", "stage", "completed_dances", "input_count",
        "excellent_count", "end_frame", "emulator_paused", "live_host_seconds", "work_dir", "avi_path", "error", "cleanup_errors")
        if key in result}, indent=2))
    return 0 if result["status"] == "success" and not result.get("cleanup_errors") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--plan-only", action="store_true")
    parser.add_argument("--run-name", default="dance-full-next")
    parser.add_argument("--input-slot", type=int, default=5)
    parser.add_argument("--checkpoint-slot", type=int, default=10)
    parser.add_argument("--finish-slot", type=int, default=10)
    parser.add_argument("--hold-frames", type=int, default=2)
    parser.add_argument("--offset-frames", type=int, default=DEFAULT_OFFSET_FRAMES)
    parser.add_argument("--host-timeout", type=float)
    parser.add_argument("--uncapped", action="store_true", help="Disable the frame limiter during the run.")
    parser.add_argument("--avi", type=Path, default=None,
        help="Optional fresh AVI output from paused slot 5; recording is off by default.")
    parser.add_argument("--osd-settle-seconds", type=float, default=0,
        help="Paused host seconds before each section, allowing native status overlays to expire.")
    args = parser.parse_args()
    if args.check:
        check()
        return 0
    if args.plan_only:
        print(json.dumps({"source_slot": 5, "source_frame": SOURCE_FRAME, "speed_percent": 100,
            "frame_limit": not args.uncapped, "avi_path": str(args.avi) if args.avi is not None else None,
            "native_osd_settle_host_seconds": args.osd_settle_seconds,
            "host_timeout_seconds": args.host_timeout, "hold_frames": args.hold_frames,
            "offset_frames": args.offset_frames, "dances": dance_schedule()}, indent=2))
        return 0
    slots = {args.input_slot, args.checkpoint_slot, args.finish_slot}
    if args.input_slot != 5 or args.input_slot in {args.checkpoint_slot, args.finish_slot} or not slots.issubset(set(range(5, 11))):
        parser.error("The full run uses prepared slot 5 and separate temporary checkpoint storage within 6 through 10.")
    if args.hold_frames <= 0 or args.host_timeout is None or args.host_timeout <= 0 or Path(args.run_name).name != args.run_name or args.run_name in {".", ".."}:
        parser.error("Use positive timing values and a plain fresh run directory name.")
    if args.osd_settle_seconds < 0:
        parser.error("Native overlay settling must be nonnegative host seconds.")
    if args.avi is not None:
        args.avi = (ROOT / args.avi).resolve()
        if (not args.avi.is_relative_to(ROOT) or args.avi.suffix.lower() != ".avi"
                or not args.avi.parent.is_dir() or args.avi.exists()):
            parser.error("Use a fresh project-local .avi path with an existing parent directory.")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
