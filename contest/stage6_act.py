"""Run frame-counted CPUJ Acting, retry slot 5 on ribbon misses, and finish paused."""

from __future__ import annotations

import argparse
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

from contest.dance_master import attach, lua_value
from contest.runtime import ROM, TimedController, TimedSlots, setup_speed, finish_speed
from contest.stage3_enter import accepted_source
from contest.stage4_dress import FRAME_RATE, stacked_gd
from host.checkpoints import CheckpointClient, CheckpointStopped, write_json
from host.controller import ControllerConfig
from host.emulator import DeSmuME
from host.paths import ROOT, project_path
from host.savestates import StateSlots
from contest.inputs import whole
from contest.timing_profiles import load_profile, section, setting


HERE = Path(__file__).resolve().parent
SOURCE_SLOT, OUTPUT_SLOT, TEMPORARY_SLOT = 5, 6, 10
_ACTING = section(None, "stage6_acting")
ENTRY_RENDER_FRAMES = _ACTING["handoff_render_frames"]
RETRY_RENDER_HOST_SECONDS = _ACTING["loaded_state_render_host_seconds"]
ROUND_SECONDS = _ACTING["rounds_1_to_3_b_mash_game_seconds"]
FINAL_SECONDS = _ACTING["final_round_and_results_b_mash_game_seconds"]


class Stage6Controller(TimedController):
    def remaining(self):
        if self.live_deadline is not None and time.monotonic() >= self.live_deadline:
            raise TimeoutError("timeout")
        return super().remaining()


def acting_plan(*, host_timeout, hold_frames=None, release_frames=None,
                 from_stage5=False, max_attempts=None, uncapped=False,
                round_seconds=None, final_seconds=None, source_slot=SOURCE_SLOT, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    values = section(profile, "stage6_acting")
    hold_frames = setting(hold_frames, values, "menu_hold_frames")
    release_frames = setting(release_frames, values, "menu_release_frames")
    round_seconds = setting(round_seconds, values, "rounds_1_to_3_b_mash_game_seconds")
    final_seconds = setting(final_seconds, values, "final_round_and_results_b_mash_game_seconds")
    if isinstance(host_timeout, bool) or not math.isfinite(host_timeout) or host_timeout <= 0:
        raise ValueError("The host timeout must be positive seconds.")
    whole(hold_frames, 1, "Menu held frames")
    whole(release_frames, 1, "Menu released frames")
    StateSlots.validate_slot(source_slot)
    if not isinstance(from_stage5, bool):
        raise ValueError("from_stage5 must be a boolean.")
    if not isinstance(uncapped, bool):
        raise ValueError("uncapped must be a boolean.")
    if max_attempts is not None:
        whole(max_attempts, 1, "Maximum Acting attempts")
    for value in (round_seconds, final_seconds):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0 or round(value * FRAME_RATE) < 1:
            raise ValueError("Acting waits must be positive game seconds covering at least one emulator frame.")
    return {"stage": 6, "timing_profile_source": profile["source"], "host_timeout_seconds": host_timeout,
        "uncapped": uncapped, "speed_percent": 100, "frame_limit": not uncapped, "fast_forward": False,
        "retry_policy": f"reload_accepted_slot_{source_slot}_and_repeat_acting",
        "retry_limit": None if max_attempts is None else max_attempts - 1, "max_attempts": max_attempts,
        "capture_policy": "judges_and_acting_order_each_round_and_available_move_types_hearts_at_entry",
        "move_policy": "exclude_empty_previous_and_unrecognized_then_most_raw_hearts_or_first_legal_fallback",
        "unrecognized_move_policy": "warn_discard_then_first_available_if_no_recognized_legal_move",
        "zero_heart_policy": "only_if_one_recognized_legal_move_remains",
        "move_category_read_policy": "once_per_attempt_from_first_round_framebuffer",
        "move_heart_read_policy": "once_per_attempt_from_first_round_framebuffer",
        "acting_order_read_policy": "each_round_from_player_panel_fill_pixels",
        "move_navigation": "remembered_row_major_2x2",
        "judge_policy": "early_four_stars_with_matching_move_else_first_fewest",
        "source_slot": source_slot, "checkpoint_slot": OUTPUT_SLOT, "temporary_slot": TEMPORARY_SLOT,
        "entry_mode": "stage5_handoff" if from_stage5 else "load_retry_state",
        "handoff_render_frames": values["handoff_render_frames"],
        "retry_render_host_seconds": values["loaded_state_render_host_seconds"],
        "entry_render_method": ("input_bridge_advance_then_fresh_paused_capture" if from_stage5
            else "host_unpause_then_native_pause_and_fresh_capture"),
        "timing": {"hold_frames": hold_frames, "release_frames": release_frames,
            "mash_hold_frames": values["mash_hold_frames"], "mash_release_frames": values["mash_release_frames"],
            "frame_rate": FRAME_RATE, "frame_conversion": "nearest emulator frame at the native frame rate"},
        "rounds": [{"round": index,
            "mash_game_seconds": seconds, "mash_frames": round(seconds * FRAME_RATE)}
            for index, seconds in enumerate((round_seconds, round_seconds, round_seconds, final_seconds), 1)],
        "success": "report_success_and_remove_owned_temporary_images_after_finalization",
        "unexpected_result": "abort_paused_with_evidence", "memory_proven_phase": False}


def acting_source(path):
    handoff_path = project_path(path)
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    if handoff.get("source_stage") != 5 or handoff.get("phase") != "acting_entry":
        raise ValueError("Stage 6 requires the accepted Stage 5 Acting-entry handoff.")
    receipt_path, source, previous = accepted_source(handoff["source_checkpoint"])
    if (previous.get("checkpoint") != "stage-5-dance"
            or any(handoff.get(key) != previous[field] for key, field in
                   (("run_id", "run_id"), ("source_frame", "frame"), ("source_epoch", "epoch")))
            or project_path(handoff["source_state"]) != source):
        raise ValueError("The Acting handoff does not match its retained Stage 5 checkpoint.")
    screen = previous["snapshot"].get("screen", {})
    if (screen.get("confirmed") is not True or screen.get("fresh_visual_binding") is not True
            or screen.get("expected") != "acting_entry"
            or any(screen.get(key) != previous[key] for key in ("frame", "epoch"))):
        raise ValueError("The Stage 5 checkpoint must have a frame-bound Acting-entry review.")
    result = json.loads(project_path(handoff["runner_result"]).read_text(encoding="utf-8"))
    if (result.get("outcome") != "pass" or result.get("stage5_accepted") is not True
            or result.get("cleanup_errors") or project_path(result["acting_handoff"]) != handoff_path
            or result.get("save_state", {}).get("slot") != SOURCE_SLOT):
        raise ValueError("The source Stage 5 run must have a passing finalized slot-5 result.")
    target = handoff.get("target")
    if (not isinstance(target, dict) or target != previous["snapshot"].get("target")
            or isinstance(target.get("ribbon_bit"), bool)
            or not isinstance(target.get("ribbon_bit"), int) or not 0 <= target["ribbon_bit"] < 20
            or target.get("party_slot") != 1):
        raise ValueError("The source must retain the same first-party contest-ribbon target.")
    return handoff_path, handoff, receipt_path, source, previous


def overworld_reference(path):
    receipt_path, _, previous = accepted_source(path)
    return overworld_reference_from_checkpoint(receipt_path, previous)


def overworld_reference_from_checkpoint(path, previous):
    """Retain reviewed hall pixels without requiring a savestate for that screen check."""
    from PIL import Image

    receipt_path = project_path(path)
    if previous.get("status") != "complete" or previous.get("proceed") is not True:
        raise ValueError("The overworld reference requires an acknowledged checkpoint.")
    screen = previous["snapshot"].get("screen", {})
    if (screen.get("confirmed") is not True or screen.get("expected") != "loaded_overworld"
            or screen.get("source") not in {"visual_review", "local_reference_pixels"}
            or any(screen.get(key) != previous[key] for key in ("frame", "epoch"))):
        raise ValueError("Use a reviewed checkpoint of the prepared contest-hall overworld.")
    image_path = project_path(screen["path"])
    if not image_path.is_file():
        points_path = receipt_path.parent.parent / "overworld-reference.json"
        retained = json.loads(points_path.read_text(encoding="utf-8"))
        reference, points = retained["reference"], retained["points"]
        if (project_path(reference["checkpoint"]) != receipt_path or project_path(reference["path"]) != image_path
                or any(reference.get(key) != previous[key] for key in ("frame", "epoch"))
                or not isinstance(points, list) or not points
                or any(not isinstance(point, list) or len(point) != 5
                    or any(isinstance(value, bool) or not isinstance(value, int) for value in point)
                    or not 0 <= point[0] < 256 or not 0 <= point[1] < 384
                    or any(not 0 <= value <= 255 for value in point[2:]) for point in points)):
            raise ValueError("The retained overworld pixels do not match this reviewed checkpoint.")
        return retained
    with Image.open(image_path) as opened:
        if opened.format != "PNG" or opened.size != (256, 384):
            raise ValueError("The overworld reference must be a native stacked PNG.")
        image = opened.convert("RGB")
    regions = ((124, 54, 131, 67), (124, 78, 131, 95))
    coordinates = [(x, y) for y in (54, 60, 66, 78, 84, 94) for x in (124, 130)]
    points = [[x, y, *image.getpixel((x, y))] for x, y in coordinates]
    return {"points": points, "regions": regions, "reference": {"path": str(image_path),
        "checkpoint": str(receipt_path), "frame": previous["frame"], "epoch": previous["epoch"],
        "source": "reviewed_prepared_contest_hall", "scope": "central_player_and_receptionist_only"}}


def retain_capture(snapshot, work, images, *, judges=False):
    item = snapshot["image"]
    path = project_path(item["path"])
    if not path.is_relative_to(work):
        raise RuntimeError("The fresh screenshot is outside this Stage 6 run.")
    images.add(path)
    image = stacked_gd(path)
    png = path.with_suffix(".png")
    image.save(png)
    images.add(png)
    result = {**item, "png": str(png)}
    if judges:
        crop = path.with_name(path.stem + "-judges.png")
        image.crop((80, 0, 176, 50)).save(crop)
        images.add(crop)
        result["judges_png"] = str(crop)
    return result


def cleanup_images(work, images):
    removed = []
    for path in sorted(images):
        if not path.resolve().is_relative_to(work.resolve()) or path.suffix.lower() not in {".png", ".gd"}:
            raise RuntimeError("Screenshot cleanup is limited to this run's owned PNG/GD files.")
        path.unlink()
        removed.append(str(path))
    receipt = {"status": "complete", "removed": removed, "retained": False,
        "reason": "ribbon_success_after_checkpoint_acknowledgement_and_finalization",
        "reference_policy": "retained logs and checkpoint image paths are resolved through this removal manifest"}
    write_json(work / "image-cleanup.json", receipt)
    return receipt


def prepare_render(emulator, client, initial, *, entry_mode, plan=None):
    """Prepare the selected entry, then confirm native pause before fresh analysis."""
    controller = emulator.controller
    values = section(None, "stage6_acting") if plan is None else plan
    render_frames = values["handoff_render_frames"]
    retry_seconds = values.get("retry_render_host_seconds", values.get("loaded_state_render_host_seconds"))
    controller.remaining()
    session = controller.session()
    if initial.get("emulator_paused") is not True or not controller.paused(session):
        raise RuntimeError("Stage 6 render preparation requires its paused source.")
    host_seconds = None
    if entry_mode == "load_retry_state":
        client.command("render-retry-begin")
        try:
            controller.set_pause(False)
            if controller.paused(session):
                raise RuntimeError("Acting retry render preparation did not reach native unpause.")
            unpaused_at = time.monotonic()
            until = unpaused_at + retry_seconds
            while True:
                controller.remaining()
                if controller.paused(session):
                    raise RuntimeError("Native pause interrupted the configured Acting retry render window.")
                left = until - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(0.05, left, controller.remaining()))
            host_seconds = time.monotonic() - unpaused_at
        finally:
            controller.set_pause(True)
        if not controller.paused(session):
            raise RuntimeError("Acting retry render preparation did not return to native pause.")
        prepared = client.command("render-prepared", host_seconds)
        if prepared["frame"] <= initial["frame"]:
            raise RuntimeError("No emulator frames advanced during the Acting retry unpause window.")
        method = "host_unpause_then_native_pause"
    elif entry_mode == "stage5_handoff":
        advanced = emulator.command("advance", render_frames, timeout=None, deadline=controller.live_deadline)
        if advanced.get("emulator_paused") is not True or not controller.paused(session):
            raise RuntimeError("The configured handoff render advance did not return to native pause.")
        prepared = client.command("render-prepared")
        if prepared["frame"] != initial["frame"] + render_frames:
            raise RuntimeError("The Acting handoff changed during its configured render preparation.")
        method = "input_bridge_advance"
    else:
        raise ValueError("Unknown Stage 6 entry mode.")
    if prepared["epoch"] != initial["epoch"]:
        raise RuntimeError("The Acting source epoch changed during render preparation.")
    return {"start_frame": initial["frame"], "end_frame": prepared["frame"],
        "epoch": prepared["epoch"], "frames": prepared["frame"] - initial["frame"],
        "input_policy": "none", "method": method, "entry_mode": entry_mode,
        "host_unpaused_seconds": host_seconds, "emulator_paused": True}


def run(*, run_name, acting_handoff, overworld_checkpoint, deadline=None, keep_uncapped=False,
        cleanup_on_success=True, **options):
    started = time.monotonic()
    if options.get("timing_profile") is None:
        options["timing_profile"] = load_profile()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    plan = acting_plan(**options)
    handoff_path, handoff, receipt_path, source, previous = acting_source(acting_handoff)
    overworld = overworld_reference(overworld_checkpoint)
    if deadline is not None and (isinstance(deadline, bool) or not math.isfinite(deadline)):
        raise ValueError("The shared deadline must be an absolute monotonic host time.")
    work = ROOT / "tmp" / "contest" / run_name
    work.mkdir(parents=True, exist_ok=False)
    (work / "checkpoints").mkdir()
    shutil.copy2(handoff_path, work / "source-handoff.json")
    shutil.copy2(receipt_path, work / "source-checkpoint.json")
    shutil.copy2(source, work / "source-state.ds5")
    source_context = {key: previous[key] for key in ("checkpoint", "run_id", "visit", "frame", "epoch")}
    config = {**plan, "run_id": run_name, "work": work.as_posix(), "source_frame": previous["frame"],
        "source_context": source_context, "target": handoff["target"], "overworld": overworld}
    write_json(work / "runner-setup.json", {**config, "host_pid": os.getpid(),
        "source_handoff": str(handoff_path), "source_checkpoint": str(receipt_path),
        "source_state": str(source), "shared_host_deadline": deadline})
    attachment = work / "attach.lua"
    attachment.write_text(
        f"assert(loadfile({lua_value((HERE / 'stage6_act.lua').as_posix())}))({lua_value(config)})\n", encoding="utf-8", newline="\n")
    result = {"outcome": "fail", "stage": 6, "run_id": run_name, "work_dir": str(work),
        "stage6_accepted": False, "ribbon_acquired": False, "target": config["target"],
        "source_context": source_context, "host_timeout_seconds": plan["host_timeout_seconds"],
        "uncapped": plan["uncapped"], "speed_percent": plan["speed_percent"],
        "frame_limit": plan["frame_limit"], "max_attempts": plan["max_attempts"],
        "attempts": [], "completed_attempts": 0}
    controller = Stage6Controller(ControllerConfig(rom=ROM))
    emulator = DeSmuME(controller)
    emulator.states = TimedSlots(controller)
    client = CheckpointClient(emulator, work, run_name)
    controller.live_deadline = started + plan["host_timeout_seconds"]
    if deadline is not None:
        controller.live_deadline = min(controller.live_deadline, deadline)
    backups, touched, images = {}, set(), set()
    owned = attached = False
    rendering_before = None
    accepted_output = None

    def record(phase, **details):
        event = {"phase": phase, "host_elapsed_seconds": round(time.monotonic() - started, 3), **details}
        write_json(work / "runner-status.json", event)
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    try:
        controller.remaining()
        record("setup", target=config["target"], uncapped=plan["uncapped"], frame_limit=plan["frame_limit"],
               host_timeout_seconds=plan["host_timeout_seconds"])
        owners = set(controller.emulator_process_ids()) | set(controller.emulator_window_process_ids())
        if owners:
            session = controller.session()
            if owners != {session["pid"]} or project_path(session["rom"]) != ROM.resolve():
                raise RuntimeError("Stage 6 requires the sole controlled Platinum emulator.")
            if plan["entry_mode"] == "stage5_handoff" and not controller.paused(session):
                raise RuntimeError("The accepted Stage 5 endpoint must still be natively paused.")
        else:
            if plan["entry_mode"] == "stage5_handoff":
                raise RuntimeError("A direct Stage 5 handoff requires its controlled paused emulator.")
            controller.launch(ROM)
        owned = True
        controller.set_pause(True)
        emulator.stop_movie()
        source_slot = emulator.states.path(SOURCE_SLOT)
        if not source_slot.is_file() or not filecmp.cmp(source_slot, source, shallow=False):
            raise RuntimeError("Native slot 5 differs from the accepted Acting checkpoint; review its ownership first.")
        for slot in (TEMPORARY_SLOT, OUTPUT_SLOT):
            path = emulator.states.path(slot)
            backups[slot] = work / f"previous-slot-{slot}.ds{slot % 10}" if path.exists() else None
            if backups[slot] is not None:
                shutil.copy2(path, backups[slot])
        window = controller.session()["window"]
        rendering_before = {"automatic": bool(emulator.menu_state(221) & 0x8),
            "skip_menu": next((item for item in range(191, 201) if emulator.menu_state(item) & 0x8), None)}
        if rendering_before["automatic"]:
            controller.send(window, 0x111, 221)
        if not emulator.menu_state(191) & 0x8:
            controller.send(window, 0x111, 191)
        if emulator.menu_state(221) & 0x8 or not emulator.menu_state(191) & 0x8:
            raise RuntimeError("Every-frame rendering was not confirmed.")
        result["speed_setup"] = setup_speed(emulator, inherited_uncapped=keep_uncapped)
        if plan["uncapped"]:
            result["speed_setup"].update(emulator.set_frame_limit(False))
        controller.remaining()
        attach(controller, attachment, bridge="pt-contest-stage6")
        attached = True
        controller.USER32.SetForegroundWindow(window)
        while True:
            controller.remaining()
            if not filecmp.cmp(source_slot, source, shallow=False):
                raise RuntimeError("Native slot 5 changed during Acting retries.")
            index = len(result["attempts"]) + 1
            folder = work / f"attempt-{index:02d}"
            folder.mkdir()
            entry_mode = plan["entry_mode"] if index == 1 else "load_retry_state"
            record("enter-acting", attempt=index, entry_mode=entry_mode, source_slot=SOURCE_SLOT,
                   remaining_host_seconds=round(controller.remaining(), 3))
            if entry_mode == "load_retry_state":
                emulator.load_state(SOURCE_SLOT, refresh=False)
            initial = client.command("begin-attempt")
            write_json(folder / "source-snapshot.json", initial)
            attempt = {"attempt": index, "entry_mode": entry_mode,
                "source_frame": initial["frame"], "source_epoch": initial["epoch"], "rounds": []}
            result["attempts"].append(attempt)
            attempt["render_preparation"] = prepare_render(emulator, client, initial, entry_mode=entry_mode, plan=plan)
            write_json(folder / "render-preparation.json", attempt["render_preparation"])
            record("render-prepared", attempt=index, **attempt["render_preparation"])
            for number in range(1, 5):
                controller.remaining()
                pending = client.command("round-visit", number)
                snapshot = pending["checkpoint"]["snapshot"]
                capture = retain_capture(snapshot, work, images, judges=True)
                if number == 1:
                    attempt["move_categories"] = snapshot["move_categories"]
                    write_json(folder / "move-categories.json", attempt["move_categories"])
                touched.add(TEMPORARY_SLOT)
                accepted = client.acknowledge(pending, slot=TEMPORARY_SLOT,
                    previous_state=str(backups[TEMPORARY_SLOT]) if backups[TEMPORARY_SLOT] else None)
                chosen = accepted["checkpoint"]["snapshot"]["plan"]
                round_result = {"round": number, "image": capture, "stars": snapshot["stars"],
                    "acting_order": snapshot["acting_order"], "plan": chosen,
                    "checkpoint_visit": accepted["checkpoint"]["visit"]}
                attempt["rounds"].append(round_result)
                record("acting-round", attempt=index, round=number, stars=snapshot["stars"]["counts"],
                       move=chosen["move"], judge=chosen["judge"], frame=accepted["frame"],
                       move_category=chosen["move_category"], move_hearts=chosen["move_hearts"],
                       acting_position=chosen["acting_position"],
                       unrecognized_move_fallback=chosen["unrecognized_fallback"],
                       matching_category_override=chosen["matching_category_override"],
                       judge_screenshot=capture["judges_png"])
                progressed = emulator.command("combo", "inputs", *chosen["steps"],
                    timeout=None, deadline=controller.live_deadline)
                round_result["end_frame"] = progressed["frame"]
                write_json(folder / f"round-{number}.json", round_result)
            client.command("inspect")
            emulator.command("combo", "inputs", f"X:{plan['timing']['hold_frames']}:{plan['timing']['release_frames']}",
                timeout=None, deadline=controller.live_deadline)
            pending = client.command("result-visit")
            snapshot = pending["checkpoint"]["snapshot"]
            attempt["result_image"] = retain_capture(snapshot, work, images)
            touched.add(OUTPUT_SLOT)
            previous_output = accepted_output or backups[OUTPUT_SLOT]
            accepted = client.acknowledge(pending, slot=OUTPUT_SLOT,
                previous_state=str(previous_output) if previous_output else None)
            accepted_output = Path(accepted["host_savestate"]["retained_path"])
            branch = accepted["checkpoint"]["branch"]
            if branch not in {"success", "retry-acting"}:
                raise RuntimeError("The Stage 6 checkpoint returned an unknown result branch.")
            attempt.update(branch=branch, target_acquired=snapshot["target_acquired"],
                ribbon_flags=snapshot["party"]["ribbon_flags"], checkpoint=accepted["checkpoint"],
                save_state=accepted["host_savestate"])
            result.update(completed_attempts=index, checkpoint=accepted["checkpoint"],
                save_state=accepted["host_savestate"], frame=accepted["frame"])
            write_json(folder / "attempt-result.json", attempt)
            write_json(work / "runner-progress.json", result)
            record("ribbon-result", attempt=index, acquired=snapshot["target_acquired"],
                   branch=branch, checkpoint_slot=OUTPUT_SLOT, frame=accepted["frame"])
            controller.remaining()
            if branch == "success":
                result.update(outcome="pass", stage6_accepted=True, ribbon_acquired=True)
                break
            if plan["max_attempts"] is not None and index >= plan["max_attempts"]:
                result.update(outcome="miss", stage6_accepted=True, stop_reason="attempt_limit_reached")
                break
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        result["outcome"] = "timeout" if isinstance(error, TimeoutError) or time.monotonic() >= controller.live_deadline else "fail"
        result["error"] = str(error) or type(error).__name__
        if isinstance(error, CheckpointStopped):
            result["failed_checkpoint"] = error.record
        record("abort", outcome=result["outcome"], error=result["error"])
    finally:
        result["live_host_seconds"] = time.monotonic() - started
        controller.live_deadline = None
        cleanup = []
        if owned:
            try:
                controller.set_pause(True)
                if attached and result["outcome"] not in {"pass", "miss"}:
                    touched.add(OUTPUT_SLOT)
                    result["stopped_evidence"] = client.stop(result["error"], slot=OUTPUT_SLOT)
                    controller.screenshot(work / "failure.png")
                    shutil.copy2(controller.config.mailbox_dir / "screen.gd", work / "failure.gd")
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
            for name, action in (("movie_finish", emulator.stop_movie), ("speed_finish",
                    lambda: finish_speed(emulator, handoff=keep_uncapped and result["outcome"] in {"pass", "miss"}))):
                try:
                    result[name] = action()
                except (Exception, SystemExit) as error:
                    cleanup.append(str(error))
            try:
                final = controller.status() if attached else {}
                result["emulator_paused"] = controller.paused(controller.session())
                result["inputs_released"] = attached and final.get("buttons") == "" and final.get("stylus_touch") is False
                if not result["emulator_paused"] or (attached and not result["inputs_released"]):
                    raise RuntimeError("Stage 6 finalization did not leave paused, released inputs.")
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
        for slot in sorted(touched, reverse=True):
            try:
                path = emulator.states.path(slot)
                restore = accepted_output if slot == OUTPUT_SLOT and accepted_output is not None else backups.get(slot)
                if restore is not None:
                    shutil.copy2(restore, path)
                    if not filecmp.cmp(restore, path, shallow=False):
                        raise RuntimeError(f"Slot {slot} restoration did not match its retained state.")
                else:
                    path.unlink(missing_ok=True)
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
        if cleanup:
            result["outcome"] = "fail"
            result.setdefault("error", "; ".join(cleanup))
        result["cleanup_errors"] = cleanup
        write_json(work / "runner-result.json", result)
        record("finalized", outcome=result["outcome"], emulator_paused=result.get("emulator_paused"))
        if result["outcome"] == "pass" and cleanup_on_success:
            try:
                result["image_cleanup"] = cleanup_images(work, images)
                for attempt in result["attempts"]:
                    for item in attempt["rounds"]:
                        item["image"].update(retained=False, cleanup_manifest=str(work / "image-cleanup.json"))
                        write_json(work / f"attempt-{attempt['attempt']:02d}" / f"round-{item['round']}.json", item)
                    attempt["result_image"].update(retained=False, cleanup_manifest=str(work / "image-cleanup.json"))
                    write_json(work / f"attempt-{attempt['attempt']:02d}" / "attempt-result.json", attempt)
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
                result.update(outcome="fail", error=str(error))
        result["cleanup_errors"] = cleanup
        result["total_host_seconds"] = time.monotonic() - started
        write_json(work / "runner-result.json", result)
        record("complete", outcome=result["outcome"], acquired=result["ribbon_acquired"],
               completed_attempts=result["completed_attempts"], error=result.get("error"), stop_reason=result.get("stop_reason"),
               emulator_paused=result.get("emulator_paused"), checkpoint_slot=OUTPUT_SLOT)
    return result


def check(*, lua_dll=None):
    from contest.checks import check_module

    check_module(__file__, ("stage6_act.lua", "acting_pixels.lua", "acting_plan.lua"),
        "Stage 6 Python and Lua syntax OK", lua_dll=lua_dll)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--acting-handoff")
    parser.add_argument("--overworld-checkpoint")
    parser.add_argument("--from-stage5", action="store_true",
                        help="Start from the current paused Stage 5 checkpoint instead of loading slot 5.")
    parser.add_argument("--max-attempts", type=int,
                        help="Optional agreed total attempt limit; ribbon misses otherwise retry until the host deadline.")
    parser.add_argument("--uncapped", action="store_true",
                        help="Disable the frame limiter while preserving counted emulator-frame timings.")
    parser.add_argument("--host-timeout", type=float)
    parser.add_argument("--hold-frames", type=int, help="Override profile menu hold emulator frames.")
    parser.add_argument("--release-frames", type=int, help="Override profile menu release emulator frames.")
    parser.add_argument("--round-seconds", type=float, help="Override profile rounds 1–3 game seconds.")
    parser.add_argument("--final-seconds", type=float, help="Override profile final round/results game seconds.")
    parser.add_argument("--lua-dll", type=Path, help="Use this Lua 5.1 DLL for the input-free --check.")
    args = parser.parse_args()
    if args.check:
        try:
            check(lua_dll=args.lua_dll)
        except (OSError, ValueError, RuntimeError) as error:
            parser.error(str(error))
        return 0
    if args.lua_dll is not None:
        parser.error("--lua-dll belongs to --check.")
    if args.host_timeout is None:
        parser.error("Supply the agreed --host-timeout in host seconds.")
    options = {key: getattr(args, key) for key in
                ("host_timeout", "hold_frames", "release_frames", "from_stage5", "max_attempts", "uncapped",
                 "round_seconds", "final_seconds")}
    try:
        if args.plan_only:
            plan = acting_plan(**options)
            if args.acting_handoff:
                _, handoff, _, _, previous = acting_source(args.acting_handoff)
                plan.update(target=handoff["target"], source_frame=previous["frame"])
            if args.overworld_checkpoint:
                plan["overworld_reference"] = overworld_reference(args.overworld_checkpoint)["reference"]
            print(json.dumps(plan, indent=2))
            return 0
        if not args.run_name or not args.acting_handoff or not args.overworld_checkpoint:
            parser.error("Execution requires --run-name, --acting-handoff, and --overworld-checkpoint.")
        result = run(run_name=args.run_name, acting_handoff=args.acting_handoff,
                     overworld_checkpoint=args.overworld_checkpoint, **options)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps({key: result[key] for key in ("outcome", "ribbon_acquired", "target", "completed_attempts",
        "uncapped", "frame_limit", "emulator_paused", "inputs_released", "work_dir", "stop_reason", "error",
        "cleanup_errors") if key in result}, indent=2))
    return 0 if result["outcome"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
