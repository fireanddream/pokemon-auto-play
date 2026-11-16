"""Continue CPUJ dancing from Stage 4 and retain a reviewed Acting-entry checkpoint."""

from __future__ import annotations

import argparse
import ast
import filecmp
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contest.dance_master import (
    attach, check as check_dance, dance_schedule, lua_value, response_dances,
)
from contest.runtime import ROM, TimedController, TimedSlots, automatic_review, setup_speed, finish_speed
from contest.pt_dance_read import gd_image
from contest.stage3_enter import accepted_source
from contest.stage4_dress import FRAME_RATE
from host.checkpoints import CheckpointClient, CheckpointStopped, write_json
from host.controller import ControllerConfig
from host.emulator import DeSmuME
from host.paths import ROOT, project_path
from contest.inputs import whole
from contest.dance_timings import fixed_timing
from contest.timing_profiles import load_profile, section, setting


HERE = Path(__file__).resolve().parent
INPUT_SLOT, OUTPUT_SLOT = 10, 5
TRANSITION_SECONDS = section(None, "stage5_dancing")["acting_transition_b_mash_game_seconds"]


class Stage5Controller(TimedController):
    def remaining(self):
        if self.live_deadline is not None and time.monotonic() >= self.live_deadline:
            raise TimeoutError("timeout")
        return super().remaining()


def dance_source(path, *, timing_profile=None):
    """Restore the accepted Stage 4 state with its paired dynamic-entry anchor."""
    handoff_path = project_path(path)
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    if handoff.get("source_stage") != 4 or handoff.get("dance") != 1:
        raise ValueError("Stage 5 requires Stage 4's Dance 1 handoff.")
    target = handoff.get("target") or {"category": "Cool", "rank": "Master"}
    motion = fixed_timing(target["category"], target["rank"], timing_profile=timing_profile)
    if (handoff.get("demo_frames") != motion["visible_half_frames"]
            or handoff.get("response_frames") != motion["response_frames"]):
        raise ValueError("The handoff must use its selected category/rank's main-runner dance halves.")
    receipt_path, source, previous = accepted_source(handoff["source_checkpoint"])
    if previous.get("checkpoint") != "stage-4-dress":
        raise ValueError("The dance source must be an accepted Stage 4 checkpoint.")
    if (handoff.get("source_frame") != previous["frame"] or handoff.get("source_epoch") != previous["epoch"]
            or handoff.get("run_id") != previous["run_id"] or project_path(handoff["source_state"]) != source):
        raise ValueError("The Stage 4 handoff does not match its retained checkpoint.")
    source_result = json.loads(project_path(handoff["runner_result"]).read_text(encoding="utf-8"))
    if (source_result.get("outcome") != "pass" or source_result.get("stage4_accepted") is not True
            or project_path(source_result["dance_handoff"]) != handoff_path):
        raise ValueError("The source Stage 4 run must have a passing terminal result.")
    landing = source_result["landing"]
    screen = previous.get("snapshot", {}).get("screen", {})
    if (landing.get("frame") != previous["frame"] or landing.get("epoch") != previous["epoch"]
            or screen.get("fresh_visual_binding") is not True):
        raise ValueError("The Stage 4 source image must belong to its accepted paused checkpoint.")
    image = project_path(landing["path"])
    gd_image(image.read_bytes())
    handoff["source_image"] = {"path": str(image), "frame": previous["frame"],
        "origin_epoch": previous["epoch"], "origin_run_id": previous["run_id"],
        "source": "accepted_stage4_checkpoint_framebuffer"}
    anchor = handoff.get("timing_anchor")
    if not isinstance(anchor, dict) or anchor.get("demo_start_frame") is None:
        raise ValueError("The Stage 4 handoff needs a readable Dance 1 progress anchor.")
    return handoff_path, handoff, receipt_path, source, previous


def dancing_plan(*, host_timeout, timing_anchor=None, source_frame=None, hold_frames=None,
                  offset_frames=None, transition_seconds=None,
                 uncapped=False, osd_settle_seconds=None, timing_profile=None, category="Cool", rank="Master"):
    profile = load_profile() if timing_profile is None else timing_profile
    values = section(profile, "stage5_dancing")
    motion = fixed_timing(category, rank, timing_profile=profile)
    half_frames = motion["response_frames"]
    visible_half = motion["visible_half_frames"]
    hold_frames = setting(hold_frames, values, "response_hold_frames")
    offset_frames = setting(offset_frames, values, "input_offset_frames")
    transition_seconds = setting(transition_seconds, values, "acting_transition_b_mash_game_seconds")
    osd_settle_seconds = setting(osd_settle_seconds, values, "native_overlay_settle_host_seconds")
    for value, label in ((host_timeout, "host timeout"), (transition_seconds, "B-mashing duration")):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"The {label} must be positive seconds.")
    if isinstance(osd_settle_seconds, bool) or not math.isfinite(osd_settle_seconds) or osd_settle_seconds < 0:
        raise ValueError("Native overlay settling must be nonnegative host seconds.")
    whole(hold_frames, 1, "Dance held frames")
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise ValueError("The input offset must be a whole emulator-frame count.")
    if not isinstance(uncapped, bool):
        raise ValueError("uncapped must be a boolean.")
    demo_start = 0
    if timing_anchor is not None:
        demo_start = whole(timing_anchor["demo_start_frame"], 0, "Dance 1 demo start")
        bounds = timing_anchor.get("demo_start_frame_range")
        if (not isinstance(bounds, list) or len(bounds) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in bounds)
                or not bounds[0] <= demo_start <= bounds[1]):
            raise ValueError("Retain the Stage 4 demo-start estimate and its measured frame range.")
        if (timing_anchor.get("response_start_frame") != demo_start + visible_half
                or timing_anchor.get("response_start_frame_range") != [value + visible_half for value in bounds]):
            raise ValueError("The handoff response timing must use the selected main-runner half offset.")
        whole(source_frame, 0, "Source frame")
        if not demo_start <= source_frame < demo_start + visible_half:
            raise ValueError("Stage 5 starts somewhere within Dance 1's demonstration.")
    elif source_frame is not None:
        raise ValueError("A source frame requires its paired Stage 4 timing anchor.")
    dances = dance_schedule(demo_start_frame=demo_start, count=8, timing_profile=profile, category=category, rank=rank)
    if timing_anchor is not None:
        for dance in dances:
            for field in ("demo_start_frame", "boundary_frame", "end_frame"):
                offset = dance[field] - demo_start
                dance[field + "_range"] = [value + offset for value in timing_anchor["demo_start_frame_range"]]
    mash_frames = round(transition_seconds * FRAME_RATE)
    if mash_frames < 1:
        raise ValueError("The B-mashing duration must cover at least one emulator frame.")
    end_frame = dances[-1]["end_frame"] + mash_frames
    return {"stage": 5, "timing_profile_source": profile["source"], "host_timeout_seconds": host_timeout, "attempts": 1,
        "source_frame": source_frame, "refresh_frames": 0, "timing_anchor": timing_anchor,
        "frame_coordinates": "absolute_emulator_frames" if timing_anchor is not None else "relative_to_dance_1_demo_start",
        "dynamic_demo_entry": True, "uncapped": uncapped, "speed_percent": 100, "frame_limit": not uncapped,
        "native_osd_settle_host_seconds": osd_settle_seconds,
        "hold_frames": hold_frames, "offset_frames": offset_frames,
        "feedback_release_frames": values["feedback_release_frames"], "cues_per_dance": motion["cues_per_dance"],
        "timing": {"same_pokemon_spacing_frames": motion["cycle_frames"], "extra_switch_frames": motion["extra_switch_frames"],
            "demo_frames": half_frames, "response_frames": half_frames, "demo_sample_frames": values["demo_sample_interval_frames"],
            "frame_rate": FRAME_RATE, "anchor_exact": timing_anchor.get("exact", False) if timing_anchor else False},
        "dances": dances, "required_response_inputs": 6 * motion["cues_per_dance"], "required_excellent_judgments": 0,
        "feedback_policy": "first_button_only", "first_feedback_policy": "warn_and_continue", "tolerant_inputs": True,
        "lead_dances": {"first_dance": 7, "last_dance": 8, "input_policy": "none",
            "start_frame": dances[5]["end_frame"], "end_frame": dances[7]["end_frame"],
            "duration_frames": dances[7]["end_frame"] - dances[5]["end_frame"],
            "leader_switch_frames": dances[6]["demo_start_frame"] - dances[5]["end_frame"]},
        "transition": {"button": "B", "game_seconds": transition_seconds, "mash_frames": mash_frames,
            "mash_hold_frames": values["transition_mash_hold_frames"], "mash_release_frames": values["transition_mash_release_frames"],
            "frame_conversion": "nearest frame at 67027965 / 1120380 frames per second"},
        "end_frame": end_frame, "total_emulator_frames": end_frame - source_frame if source_frame is not None else None,
        "checkpoint_slot": OUTPUT_SLOT, "temporary_slot": INPUT_SLOT,
        "acceptance": f"All {6 * motion['cues_per_dance']} opportunities are executed or skipped; input-free Dances 7 and 8 and the Acting transition complete.",
        "failure_policy": "Warn on feedback or cue failures and continue to the counted endpoint; a skipped input is not a feedback Miss.",
        "review_source": "visual_review", "memory_proven_phase": False}


def await_acting_review(controller, request):
    path = Path(request["review_path"])
    while not path.is_file():
        controller.remaining()
        if not controller.paused(controller.session()):
            controller.set_pause(True)
            raise RuntimeError("Native pause was lost during Acting-entry screenshot review.")
        time.sleep(min(0.2, controller.remaining()))
    controller.remaining()
    review = json.loads(path.read_text(encoding="utf-8"))
    if any(review.get(key) != request[key] for key in ("run_id", "frame", "epoch")):
        raise ValueError("The review belongs to a different paused boundary.")
    if review.get("verdict") not in {"pass", "fail"} or review.get("source") not in {"visual_review", "local_reference_pixels"}:
        raise ValueError("Supply a pass/fail verdict with its actual visual source.")
    note = review.get("note")
    if not isinstance(note, str) or not note.strip() or any(char in note for char in "|\r\n"):
        raise ValueError("The review requires a single-line visual explanation.")
    return review


def run(*, run_name, dance_handoff, deadline=None, auto_review=False, keep_uncapped=False, **options):
    started = time.monotonic()
    if options.get("timing_profile") is None:
        options["timing_profile"] = load_profile()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    handoff_path, handoff, receipt_path, source, previous = dance_source(dance_handoff, timing_profile=options["timing_profile"])
    target = handoff.get("target") or {"category": "Cool", "rank": "Master"}
    plan = dancing_plan(timing_anchor=handoff["timing_anchor"], source_frame=previous["frame"],
        category=target["category"], rank=target["rank"], **options)
    if deadline is not None and (isinstance(deadline, bool) or not math.isfinite(deadline)):
        raise ValueError("The shared deadline must be an absolute monotonic host time.")
    work = ROOT / "tmp" / "contest" / run_name
    work.mkdir(parents=True, exist_ok=False)
    (work / "checkpoints").mkdir()
    shutil.copy2(handoff_path, work / "source-handoff.json")
    shutil.copy2(receipt_path, work / "source-checkpoint.json")
    shutil.copy2(source, work / "source-state.ds0")
    shutil.copy2(project_path(handoff["source_image"]["path"]), work / "source-stage4.gd")
    source_context = {key: previous[key] for key in ("checkpoint", "run_id", "visit", "frame", "epoch")}
    for dance in plan["dances"][:6]:
        folder = work / f"dance-{dance['index']:02d}"
        folder.mkdir()
        dance.update(work=folder.as_posix(), presses=[])
    config = {**plan, "run_id": run_name, "work": work.as_posix(), "mode": "stage5",
        "source_image": {**handoff["source_image"], "path": (work / "source-stage4.gd").as_posix()},
        "source_context": source_context, "target": previous.get("snapshot", {}).get("target")}
    write_json(work / "runner-setup.json", {**config, "host_pid": os.getpid(),
        "source_handoff": str(handoff_path), "source_checkpoint": str(receipt_path), "source_state": str(source),
        "shared_host_deadline": deadline})
    attachment = work / "attach.lua"
    attachment.write_text(
        f"assert(loadfile({lua_value((HERE / 'dance_master.lua').as_posix())}))({lua_value(config)})\n", encoding="utf-8", newline="\n")
    result = {"outcome": "fail", "stage": 5, "run_id": run_name, "work_dir": str(work),
        "stage5_accepted": False, "source_context": source_context, "source_kind": "accepted_stage4_checkpoint",
        "host_timeout_seconds": plan["host_timeout_seconds"], "timing_anchor": plan["timing_anchor"],
        "timing": plan["timing"], "frame_limit": plan["frame_limit"], "dances": [],
        "feedback_policy": plan["feedback_policy"]}
    controller = Stage5Controller(ControllerConfig(rom=ROM))
    emulator = DeSmuME(controller)
    emulator.states = TimedSlots(controller)
    client = CheckpointClient(emulator, work, run_name)
    backups, touched = {}, set()
    owned = attached = False
    rendering_before = None
    controller.live_deadline = started + plan["host_timeout_seconds"]
    if deadline is not None:
        controller.live_deadline = min(controller.live_deadline, deadline)
    controller.timeouts_enabled = False

    def record(phase, **details):
        event = {"phase": phase, "host_elapsed_seconds": round(time.monotonic() - started, 3), **details}
        write_json(work / "runner-status.json", event)
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    try:
        controller.remaining()
        record("setup", source_frame=previous["frame"], dynamic_demo_entry=True)
        owners = set(controller.emulator_process_ids()) | set(controller.emulator_window_process_ids())
        if owners:
            session = controller.session()
            if owners != {session["pid"]} or project_path(session["rom"]) != ROM.resolve():
                raise RuntimeError("Stage 5 requires the sole controlled Platinum emulator.")
        else:
            controller.launch(ROM)
        owned = True
        controller.set_pause(True)
        emulator.stop_movie()
        for slot in (INPUT_SLOT, OUTPUT_SLOT):
            path = emulator.states.path(slot)
            backups[slot] = work / f"previous-slot-{slot}.ds{slot % 10}" if path.exists() else None
            if backups[slot] is not None:
                shutil.copy2(path, backups[slot])
        rendering_before = {"automatic": bool(emulator.menu_state(221) & 0x8),
            "skip_menu": next((item for item in range(191, 201) if emulator.menu_state(item) & 0x8), None)}
        window = controller.session()["window"]
        if rendering_before["automatic"]:
            controller.send(window, 0x111, 221)
        if not emulator.menu_state(191) & 0x8:
            controller.send(window, 0x111, 191)
        if emulator.menu_state(221) & 0x8 or not emulator.menu_state(191) & 0x8:
            raise RuntimeError("Every-frame rendering was not confirmed.")
        result["speed_setup"] = setup_speed(emulator, inherited_uncapped=keep_uncapped)
        attach(controller, attachment, bridge="pt-contest-stage5")
        attached = True
        touched.add(INPUT_SLOT)
        shutil.copy2(work / "source-state.ds0", emulator.states.path(INPUT_SLOT))
        emulator.load_state(INPUT_SLOT, refresh=False)
        initial = client.command("inspect")
        if initial["frame"] != previous["frame"] or not initial["observation"]["game"]["supported"]:
            raise RuntimeError("The loaded source does not match the retained CPUJ checkpoint.")
        write_json(work / "source-snapshot.json", initial)
        if plan["uncapped"]:
            result["speed_setup"].update(emulator.set_frame_limit(False))
        client.command("init")
        controller.USER32.SetForegroundWindow(window)

        def on_progress(phase, index):
            record(phase, dance=index)

        for completed in response_dances(emulator, client, plan["dances"], checkpoint_slot=INPUT_SLOT,
                finish_slot=INPUT_SLOT, hold_frames=plan["hold_frames"], offset_frames=plan["offset_frames"],
                osd_settle_seconds=plan["native_osd_settle_host_seconds"],
                terminal_section="lead-dances", first_feedback_only=True, on_progress=on_progress,
                timing_profile=options["timing_profile"]):
            result["dances"].append(completed["response"])
            trial = completed["trial"]
            result.update(completed_dances=len(result["dances"]), input_count=trial["total_input_count"],
                excellent_count=trial["total_excellent_count"], feedback_checks=trial["total_judgment_count"],
                miss_count=trial["total_miss_count"], skipped_input_count=trial["total_skipped_inputs"],
                offset_check=completed["finish"]["checkpoint"]["snapshot"]["offset_check"],
                response_end_frame=completed["finish"]["frame"])
            write_json(work / "runner-progress.json", result)
        record("lead-dances", **plan["lead_dances"])
        def handoff_action(action, expected):
            try:
                value = emulator.command(action, timeout=None, deadline=controller.live_deadline)
                if value.get("error") or value.get("stop_reason") != expected:
                    raise RuntimeError(value.get("error") or "The handoff needs counted continuation.")
            except Exception as error:
                if isinstance(error, TimeoutError):
                    raise
                reason = (str(error) or type(error).__name__).replace("|", "/").replace("\r", " ").replace("\n", " ")
                client.command("warn", reason)
                record("dance-warning", action=action, reason=reason)
                value = emulator.command(action, timeout=None, deadline=controller.live_deadline)
            return value

        lead = handoff_action("lead-dances", "lead_dances_complete")
        write_json(work / "lead-dances-result.json", lead)
        if lead["frame"] != plan["dances"][7]["end_frame"]:
            client.command("warn", "The input-free dance endpoint was overrun")
        result["lead_dances"] = lead["observation"]["lead_dances"]
        record("acting-transition", **plan["transition"])
        transitioned = handoff_action("acting-transition", "B_transition_complete")
        write_json(work / "transition-result.json", transitioned)
        if transitioned["frame"] != plan["end_frame"]:
            client.command("warn", "The Acting transition endpoint was overrun")
        result["transition"] = transitioned["observation"]["transition"]
        capture = client.command("capture-acting")
        image_path = work / "stage5.png"
        controller.screenshot(image_path)
        result.update(frame=capture["frame"], screenshot=str(image_path))
        request = {"run_id": run_name, "frame": capture["frame"], "epoch": capture["epoch"],
            "expected_screen": "acting_entry", "image_path": str(image_path),
            "gd_path": str(work / "stage5.gd"), "review_path": str(work / "review.json"),
            "source": "visual_review", "memory_proven": False}
        request["automatic_check"] = capture["observation"]["automatic_check"]
        write_json(work / "review-request.json", request)
        if auto_review:
            automatic_review(request, request["automatic_check"])
        record("review", expected_screen="acting_entry", review_request=str(work / "review-request.json"),
               screenshot=str(image_path))
        review = await_acting_review(controller, request)
        review_path = work / "review.lua"
        review_path.write_text("return " + lua_value(review) + "\n", encoding="utf-8", newline="\n")
        client.command("review-acting", review_path.as_posix())
        pending = client.command("visit", "stage-5-dance")
        touched.add(OUTPUT_SLOT)
        accepted = client.acknowledge(pending, slot=OUTPUT_SLOT,
            previous_state=str(backups[OUTPUT_SLOT]) if backups[OUTPUT_SLOT] else None)
        controller.remaining()
        checkpoint = accepted["checkpoint"]
        receipt = work / "checkpoints" / f"{checkpoint['visit']:04d}-{checkpoint['checkpoint']}.json"
        acting_handoff = {"source_stage": 5, "source_checkpoint": str(receipt),
            "source_state": accepted["host_savestate"]["retained_path"], "source_frame": capture["frame"],
            "source_epoch": capture["epoch"], "run_id": run_name, "phase": "acting_entry",
            "target": config["target"], "runner_result": str(work / "runner-result.json")}
        write_json(work / "acting-handoff.json", acting_handoff)
        result.update(outcome="pass", stage5_accepted=True, checkpoint=accepted["checkpoint"],
            save_state=accepted["host_savestate"], acting_handoff=str(work / "acting-handoff.json"))
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        result["outcome"] = "timeout" if isinstance(error, TimeoutError) or time.monotonic() >= controller.live_deadline else "fail"
        result["error"] = str(error) or type(error).__name__
        if isinstance(error, CheckpointStopped):
            result["checkpoint"] = error.record
        record("abort", outcome=result["outcome"], error=result["error"])
    finally:
        result["live_host_seconds"] = time.monotonic() - started
        controller.live_deadline, controller.timeouts_enabled = None, True
        cleanup = []
        if owned:
            try:
                controller.set_pause(True)
                if attached and result["outcome"] != "pass":
                    touched.add(OUTPUT_SLOT)
                    result["stopped_evidence"] = client.stop(result["error"], slot=OUTPUT_SLOT)
                    controller.screenshot(work / "failure.png")
                    shutil.copy2(controller.config.mailbox_dir / "screen.gd", work / "failure.gd")
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
            for name, action in (("movie_finish", emulator.stop_movie), ("speed_finish",
                    lambda: finish_speed(emulator, handoff=keep_uncapped and result["outcome"] == "pass"))):
                try:
                    result[name] = action()
                except (Exception, SystemExit) as error:
                    cleanup.append(str(error))
            try:
                final = controller.status() if attached else {}
                result["emulator_paused"] = controller.paused(controller.session())
                result["inputs_released"] = attached and final.get("buttons") == "" and final.get("stylus_touch") is False
                if not result["emulator_paused"] or (attached and not result["inputs_released"]):
                    raise RuntimeError("Stage 5 finalization did not leave paused, released inputs.")
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
        if rendering_before is not None:
            try:
                item = rendering_before["skip_menu"]
                if item is not None and not emulator.menu_state(item) & 0x8:
                    controller.send(window, 0x111, item)
                if bool(emulator.menu_state(221) & 0x8) != rendering_before["automatic"]:
                    controller.send(window, 0x111, 221)
                result["rendering_restored"] = True
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
        if cleanup:
            result["outcome"] = "fail"
            result.setdefault("error", "; ".join(cleanup))
        for slot in sorted(touched, reverse=True):
            if slot == OUTPUT_SLOT and result["outcome"] == "pass" and not cleanup:
                continue
            try:
                path = emulator.states.path(slot)
                if backups[slot] is not None:
                    shutil.copy2(backups[slot], path)
                    if not filecmp.cmp(backups[slot], path, shallow=False):
                        raise RuntimeError(f"Slot {slot} restoration did not match its backup.")
                else:
                    path.unlink(missing_ok=True)
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
        if cleanup:
            result["outcome"] = "fail"
            result.setdefault("error", "; ".join(cleanup))
        if result["outcome"] != "pass":
            result["stage5_accepted"] = False
        result["cleanup_errors"] = cleanup
        result["total_host_seconds"] = time.monotonic() - started
        write_json(work / "runner-result.json", result)
        record("complete", outcome=result["outcome"], error=result.get("error"),
               emulator_paused=result.get("emulator_paused"), checkpoint_slot=OUTPUT_SLOT)
    return result


def check():
    ast.parse(Path(__file__).read_text(encoding="utf-8"))
    check_dance()
    print("Stage 5 Python syntax OK")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--dance-handoff")
    parser.add_argument("--host-timeout", type=float)
    parser.add_argument("--hold-frames", type=int)
    parser.add_argument("--offset-frames", type=int)
    parser.add_argument("--transition-seconds", type=float)
    parser.add_argument("--uncapped", action="store_true")
    parser.add_argument("--osd-settle-seconds", type=float)
    args = parser.parse_args()
    if args.check:
        check()
        return 0
    if args.host_timeout is None:
        parser.error("Supply the agreed --host-timeout in host seconds.")
    options = {key: getattr(args, key) for key in ("host_timeout", "hold_frames", "offset_frames",
               "transition_seconds", "uncapped", "osd_settle_seconds")}
    try:
        if args.plan_only:
            source = {}
            if args.dance_handoff:
                _, handoff, _, _, previous = dance_source(args.dance_handoff)
                target = handoff.get("target") or {"category": "Cool", "rank": "Master"}
                source = {"timing_anchor": handoff["timing_anchor"], "source_frame": previous["frame"],
                    "category": target["category"], "rank": target["rank"]}
            print(json.dumps(dancing_plan(**source, **options), indent=2))
            return 0
        if not args.run_name or not args.dance_handoff:
            parser.error("Execution requires --run-name and --dance-handoff.")
        result = run(run_name=args.run_name, dance_handoff=args.dance_handoff, **options)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps({key: result[key] for key in ("outcome", "stage5_accepted", "completed_dances", "input_count",
        "excellent_count", "miss_count", "skipped_input_count", "feedback_checks", "frame", "emulator_paused", "work_dir", "error",
        "cleanup_errors") if key in result}, indent=2))
    return 0 if result["outcome"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
