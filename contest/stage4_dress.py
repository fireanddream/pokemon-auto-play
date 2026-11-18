"""Dress CPUJ contest accessories and locate the early Dance 1 handoff."""

from __future__ import annotations

import argparse
import filecmp
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import struct
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contest.dance_master import lua_value
from contest.dance_timings import calibrated_timing, fixed_timing, pointer_calibration
from contest.pt_dance_read import native_top, read_pointer, read_progress
from contest.runtime import ROM, TimedController, TimedSlots, setup_speed, finish_speed
from contest.stage3_enter import accepted_source
from host.checkpoints import CheckpointClient, CheckpointStopped, write_json
from host.controller import ControllerConfig
from host.emulator import DeSmuME
from host.paths import ROOT, project_path
from contest.inputs import whole
from contest.timing_profiles import FRAME_RATE, load_profile, section, setting


HERE = Path(__file__).resolve().parent
INPUT_SLOT, OUTPUT_SLOT = 10, 4
_DRESS = section(None, "stage4_dress_up")
TRANSITION_SECONDS = _DRESS["dance_transition_a_mash_game_seconds"]
POLL_SECONDS = _DRESS["dance_entry_poll_game_seconds"]
AUTO_SECONDS = _DRESS["accessory_budget_game_seconds"]
DRESS_GRID = 8
PICKUP_FRAMES = _DRESS["accessory_pickup_hold_frames"]
DRAG_FRAMES = _DRESS["accessory_drag_frames"]
DRAG_RELEASE_FRAMES = _DRESS["accessory_release_frames"]
PAGE_WAIT_FRAMES = _DRESS["page_render_wait_frames"]
ENTRY_RENDER_FRAMES = _DRESS["loaded_entry_render_frames"]
INITIAL_PAGE_TAPS = (0, 7)
PAGE_FLIP_LIMIT = 14


class Stage4Controller(TimedController):
    def remaining(self):
        if self.live_deadline is not None and time.monotonic() >= self.live_deadline:
            raise TimeoutError("timeout")
        return super().remaining()


def dress_plan(*, host_timeout,
                transition_seconds=None,
                stylus_x=230, stylus_y=173, stylus_hold_frames=None, stylus_release_frames=None,
                from_stage3=False, dress_grid=DRESS_GRID, dress_seconds=None,
                pickup_frames=None, drag_frames=None,
                drag_release_frames=None, page_wait_frames=None, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    values = section(profile, "stage4_dress_up")
    transition_seconds = setting(transition_seconds, values, "dance_transition_a_mash_game_seconds")
    stylus_hold_frames = setting(stylus_hold_frames, values, "finish_tap_hold_frames")
    stylus_release_frames = setting(stylus_release_frames, values, "finish_tap_release_frames")
    dress_seconds = setting(dress_seconds, values, "accessory_budget_game_seconds")
    pickup_frames = setting(pickup_frames, values, "accessory_pickup_hold_frames")
    drag_frames = setting(drag_frames, values, "accessory_drag_frames")
    drag_release_frames = setting(drag_release_frames, values, "accessory_release_frames")
    page_wait_frames = setting(page_wait_frames, values, "page_render_wait_frames")
    for value, label in ((host_timeout, "host timeout"), (transition_seconds, "A-mashing duration")):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"The {label} must be positive seconds.")
    for value, maximum, label in ((stylus_x, 255, "Stylus X"), (stylus_y, 191, "Stylus Y")):
        whole(value, 0, label)
        if value > maximum:
            raise ValueError(f"{label} must be a native touchscreen coordinate.")
    whole(stylus_hold_frames, 1, "Stylus held frames")
    whole(stylus_release_frames, 1, "Stylus released frames")
    if not isinstance(from_stage3, bool):
        raise ValueError("from_stage3 must be a boolean.")
    whole(dress_grid, 1, "Dress-up grid size")
    if dress_grid not in (6, 7, 8):
        raise ValueError("The accessory grid must be 6 by 6, 7 by 7, or 8 by 8.")
    if isinstance(dress_seconds, bool) or not math.isfinite(dress_seconds) or dress_seconds <= 0:
        raise ValueError("The automatic dress-up duration must be positive game seconds.")
    for value, label in ((pickup_frames, "Accessory pickup frames"), (drag_frames, "Accessory drag frames"),
                         (drag_release_frames, "Accessory release frames")):
        whole(value, 1, label)
    whole(page_wait_frames, 0, "Accessory page render frames")
    render_frames = 0 if from_stage3 else values["loaded_entry_render_frames"]
    dress_frames = round(dress_seconds * FRAME_RATE)
    initial_page_frames = values["page_tap_hold_frames"] + values["page_tap_release_frames"] + page_wait_frames
    if dress_frames < render_frames + INITIAL_PAGE_TAPS[1] * initial_page_frames + drag_release_frames:
        raise ValueError("The dressing window must cover entry rendering, initial page navigation, and the released-input tail.")
    automatic = {"enabled": True, "game_seconds": dress_seconds, "budget_frames": dress_frames,
        "frame_rate": FRAME_RATE, "speed_percent": 100, "frame_limit": True, "grid_size": dress_grid,
        "rectangle": {"left": 14, "top": 35, "right": 114, "bottom": 131},
        "background_rgb": [121, 247, 215], "background_sample": {"x": 20, "y": 43},
        "centers": [{"row": row + 1, "column": column + 1,
                     "x": math.floor(14 + (column + 0.5) * 100 / dress_grid + 0.5),
                     "y": math.floor(35 + (row + 0.5) * 96 / dress_grid + 0.5)}
                    for row in range(dress_grid) for column in range(dress_grid)],
        "destination": {"x": 192, "y": 80},
        "motion": {"pickup_frames": pickup_frames, "drag_frames": drag_frames,
                   "release_frames": drag_release_frames,
                   "cycle_frames": pickup_frames + drag_frames + drag_release_frames},
        "next_page": {"x": 66, "y": 160, "hold_frames": values["page_tap_hold_frames"], "release_frames": values["page_tap_release_frames"],
                        "wait_frames": page_wait_frames},
        "page_flip_limit": PAGE_FLIP_LIMIT,
        "initial_navigation": {"tap_range": list(INITIAL_PAGE_TAPS), "selection": "fresh_random_integer_per_entry",
            "frames_per_tap": initial_page_frames, "maximum_frames": INITIAL_PAGE_TAPS[1] * initial_page_frames,
            "ordering": "all_initial_taps_before_first_accessory_capture_or_drag", "counts_toward_budget": True,
            "counts_toward_page_flip_limit": False},
        "full_checks": {"Normal": {"limit": 5, "x": 118, "y": 110},
                        "Super": {"limit": 10, "x": 208, "y": 110},
                        "Hyper": {"limit": 15, "x": 118, "y": 136,
                                  "heart_rgb": [156, 49, 99], "pearl_rgb": [255, 115, 222]},
                        "Master": {"limit": 20, "x": 208, "y": 128}},
        "indicator_rgb": {"pearl": [8, 189, 239], "heart": [231, 0, 115]},
        "scan_policy": "one center sample per cell, once per page, from one fresh paused framebuffer",
        "deadline_policy": f"count emulator frames from Stage 3; Finish when full or after {PAGE_FLIP_LIMIT} collection page flips, otherwise at the frame-budget deadline",
        "drag_success_checks": False}
    mash_frames = round(transition_seconds * FRAME_RATE)
    if mash_frames < 1:
        raise ValueError("The A-mashing duration must cover at least one emulator frame.")
    return {"stage": 4, "timing_profile_source": profile["source"], "host_timeout_seconds": host_timeout,
            "entry_mode": "stage3_handoff" if from_stage3 else "load_stage3_checkpoint",
            "entry_render_frames": render_frames, "automatic_dressing": automatic,
            "dressing_speed_percent": 100, "accelerated_frame_limit": False,
            "timing": {"transition_game_seconds": transition_seconds, "mash_frames": mash_frames,
                       "mash_hold_frames": values["transition_mash_hold_frames"], "mash_release_frames": values["transition_mash_release_frames"],
                       "poll_game_seconds": values["dance_entry_poll_game_seconds"], "frame_rate": FRAME_RATE,
                       "frame_conversion": "nearest frame at 67027965 / 1120380 frames per second"},
            "stylus": {"x": stylus_x, "y": stylus_y, "hold_frames": stylus_hold_frames,
                       "release_frames": stylus_release_frames, "button": "Finish"},
             "dance": 1, "ideal_demo_frame_range": [0, fixed_timing("Cool", "Master", timing_profile=profile)["response_frames"] // 2],
            "dance_check": {"tip_x": 8, "tip_rows": [155, 156, 157, 158],
                            "source": "fresh_zero_frame_left_tip_pixels"},
            "checkpoint_slot": OUTPUT_SLOT, "attempts": 1,
            "acceptance": "The first paused one-second check with a green dance-progress left tip.",
            "review_source": "fresh_zero_frame_left_tip_pixels", "memory_proven_phase": False}


def dress_source(path):
    receipt_path, source, previous = accepted_source(path)
    if previous.get("checkpoint") != "stage-3-enter":
        raise ValueError("Stage 4 requires an accepted Stage 3 dress-up checkpoint.")
    screen = previous.get("snapshot", {}).get("screen", {})
    if screen.get("confirmed") is not True or screen.get("expected") != "dress_up_entry":
        raise ValueError("The Stage 3 checkpoint does not confirm dress-up entry.")
    return receipt_path, source, previous


def attach(controller, attachment):
    dialog = controller.session().get("lua_window", 0)
    if controller.USER32.IsWindow(dialog):
        stop = controller.USER32.GetDlgItem(dialog, 312)
        if stop and controller.USER32.IsWindowEnabled(stop):
            controller.send(dialog, 0x111, 312, stop)
            while controller.USER32.IsWindowEnabled(stop):
                controller.remaining()
                time.sleep(0.01)
    controller.set_pause(True)
    started = time.time_ns()
    controller.load_lua(attachment)
    dialog = controller.session()["lua_window"]
    stop = controller.USER32.GetDlgItem(dialog, 312)
    if not controller.USER32.IsWindowEnabled(stop):
        controller.send(dialog, 0x111, 311, controller.USER32.GetDlgItem(dialog, 311))
    while True:
        controller.remaining()
        try:
            fresh = controller.STATUS.stat().st_mtime_ns >= started
            status = json.loads(controller.STATUS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fresh, status = False, {}
        if fresh and status.get("bridge") == "pt-contest-stage4" and status.get("checkpoint_protocol") == 1:
            controller.set_pause(True)
            return
        if not controller.USER32.IsWindowEnabled(stop):
            raise RuntimeError("The Stage 4 Lua bridge stopped before reporting ready.")
        time.sleep(0.02)


def stacked_gd(path):
    from PIL import Image

    raw = Path(path).read_bytes()
    if len(raw) < 11:
        raise ValueError("Truncated timing screenshot.")
    magic, width, height = struct.unpack_from(">HHH", raw)
    if magic != 65534 or raw[6] != 1 or (width, height) != (256, 384) or len(raw) != 11 + width * height * 4:
        raise ValueError("Expected a native stacked-screen GD image.")
    channels = Image.frombytes("RGBA", (width, height), raw[11:]).split()
    return Image.merge("RGB", channels[1:])


def pointer_timing(image, *, rank_timing=None, timing_profile=None):
    pointer = read_pointer(native_top(image))
    progress = read_progress({"status": "unreadable", "position_bounds": None, "first_empty_x": None}, pointer,
        frames_per_pixel=(rank_timing or fixed_timing("Cool", "Master", timing_profile=timing_profile))["frames_per_pixel"])
    return {"source": "pt_dance_read_jigglypuff_pixels", "pointer": pointer, "progress": progress,
            "memory_proven": False}


def place_accessories(emulator, client, plan, work, source_frame, initial_page_taps, *, acknowledge, record):
    """Shared automatic section; its caller supplies save ownership and speed policy."""
    controller = emulator.controller
    automatic, pages = plan["automatic_dressing"], []
    summary = {key: automatic[key] for key in ("enabled", "grid_size", "game_seconds", "budget_frames", "motion", "speed_percent", "page_flip_limit")}
    pages_path = work / "dress-pages.json"
    summary["pages_manifest"] = str(pages_path)
    navigation = {"chosen_taps": initial_page_taps, "executed_taps": 0, "executed_frames": 0,
        "planned_frames": initial_page_taps * automatic["initial_navigation"]["frames_per_tap"],
        "complete": initial_page_taps == 0}
    record("initial_accessory_page", chosen_taps=initial_page_taps, emulator_frames=navigation["planned_frames"])
    if initial_page_taps:
        navigated = emulator.command("dress-initial-pages", timeout=None, deadline=controller.live_deadline)
        write_json(work / "initial-navigation-inputs.json", navigated)
        navigation = navigated["observation"]["dressing"]["initial_navigation"]
        if (navigated.get("stop_reason") != "dress_initial_pages_complete"
                or navigation["chosen_taps"] != initial_page_taps or navigation["executed_taps"] != initial_page_taps
                or navigation["executed_frames"] != navigation["planned_frames"] or not navigation["complete"]
                or navigated["command_end_frame"] != source_frame + plan["entry_render_frames"] + navigation["planned_frames"]):
            raise RuntimeError("Initial accessory navigation did not complete before the first page read.")
    write_json(work / "initial-navigation.json", navigation)
    summary["initial_navigation"] = navigation
    record("automatic_dress", game_seconds=automatic["game_seconds"], emulator_frames=automatic["budget_frames"],
           grid_size=automatic["grid_size"], motion=automatic["motion"], speed_percent=100)

    def save_pages():
        write_json(pages_path, {"grid_size": automatic["grid_size"], "background_rgb": automatic["background_rgb"],
            "initial_navigation": navigation, "checks": pages})

    while True:
        controller.remaining()
        pending = client.command("dress-page-visit")
        progress = pending["observation"]["dressing"]
        page = progress["page"]
        image = work / f"dress-page-{page['index']:03d}.png"
        controller.screenshot(image)
        item = {"page": page["index"], "frame": page["frame"], "epoch": page["epoch"], "source": page["source"],
            "image": str(image), "gd_image": page["path"], "remaining_frames": progress["remaining_frames"],
            "last_indicator": page["last_indicator"], "full": page["full"], "readable": page["readable"], "queued_items": page["items"]}
        pages.append(item)
        summary.update({key: value for key, value in progress.items() if key != "page"})
        save_pages()
        record("dress_page_check", page=page["index"], frame=page["frame"], full=page["full"],
               readable=page["readable"], queued_drags=len(page["items"]), remaining_emulator_frames=progress["remaining_frames"],
               page_flips=progress["collection_page_taps"])
        accepted = acknowledge(pending)
        branch = accepted["checkpoint"]["branch"]
        item.update(checkpoint=accepted["host_receipt"], branch=branch)
        if branch == "finish-dress-early":
            if (progress.get("complete") is not True or progress.get("full_detected") is not True
                    or progress["end_frame"] != page["frame"] or progress["end_reason"] != "accessory_limit"
                    or progress["start_frame"] != source_frame or not 0 <= progress["elapsed_frames"] <= automatic["budget_frames"]):
                raise RuntimeError("The full accessory indicator did not authorize an immediate Finish.")
            save_pages()
            record("dress_full", frame=page["frame"], limit=page["limit"], elapsed_emulator_frames=progress["elapsed_frames"],
                   elapsed_game_seconds=progress["elapsed_game_seconds"], finish="immediate")
            break
        if branch != "drag-dress-page":
            raise RuntimeError("The accessory checkpoint did not authorize the queued inputs.")
        inputs = emulator.command("dress-page-inputs", timeout=None, deadline=controller.live_deadline)
        input_path = work / f"dress-page-{page['index']:03d}-inputs.json"
        write_json(input_path, inputs)
        item.update(input_result=str(input_path), stop_reason=inputs.get("stop_reason"))
        progress = inputs["observation"]["dressing"]
        summary.update({key: value for key, value in progress.items() if key != "page"})
        save_pages()
        if inputs.get("stop_reason") == "dress_countdown_complete":
            if (progress["elapsed_frames"] != automatic["budget_frames"] or progress["start_frame"] != source_frame
                    or inputs["command_end_frame"] != source_frame + automatic["budget_frames"]
                    or progress.get("complete") is not True):
                raise RuntimeError("The dressing window did not end at its exact counted deadline.")
            record("dress_countdown_complete", frame=progress["end_frame"], emulator_frames=progress["elapsed_frames"],
                    attempted_drags=progress["attempted_drags"], completed_drag_inputs=progress["completed_drag_inputs"])
            break
        if inputs.get("stop_reason") == "dress_page_flip_limit_reached":
            if (progress.get("complete") is not True or progress.get("page_flip_limit_reached") is not True
                    or progress["end_reason"] != "page_flip_limit" or progress["start_frame"] != source_frame
                    or progress["end_frame"] != inputs["command_end_frame"]
                    or progress["end_frame"] != source_frame + progress["elapsed_frames"]
                    or not 0 <= progress["elapsed_frames"] < automatic["budget_frames"]
                    or progress["collection_page_taps"] != automatic["page_flip_limit"]
                    or progress["page_taps"] != navigation["executed_taps"] + progress["collection_page_taps"]
                    or progress["last_input"]["executed_frames"] != progress["last_input"]["planned_frames"]):
                raise RuntimeError("The collection page-flip limit did not authorize an immediate Finish.")
            record("dress_page_flip_limit", frame=progress["end_frame"], page_flips=progress["collection_page_taps"],
                   initial_page_flips=navigation["executed_taps"], elapsed_emulator_frames=progress["elapsed_frames"],
                   finish="immediate")
            break
        if inputs.get("stop_reason") != "dress_page_complete":
            raise RuntimeError("The accessory page input section did not complete.")
    touch = emulator.command("finish-dressing", timeout=None, deadline=controller.live_deadline)
    write_json(work / "stylus-result.json", touch)
    if (touch.get("stop_reason") != "finish_tap_complete" or touch["command_start_frame"] != summary["end_frame"]
            or touch["command_end_frame"] != summary["end_frame"] + plan["stylus"]["hold_frames"] + plan["stylus"]["release_frames"]):
        raise RuntimeError("The Finish tap must immediately follow the dressing stop.")
    return summary


def locate_dance_entry(emulator, client, plan, work, *, record, rank_timing=None, timing_profile=None):
    """Find the green entry and complete any configured counted pointer calibration."""
    controller = emulator.controller
    motion = rank_timing or fixed_timing("Cool", "Master", timing_profile=timing_profile)
    poll_seconds = plan["timing"]["poll_game_seconds"]
    record("transition", game_seconds=plan["timing"]["transition_game_seconds"], emulator_frames=plan["timing"]["mash_frames"])
    inputs = emulator.command("dress-transition", timeout=None, deadline=controller.live_deadline)
    write_json(work / "input-result.json", inputs)
    if inputs.get("stop_reason") != "A_mash_complete":
        raise RuntimeError("The A-mashing segment did not complete.")
    checks, wait_frames, poll = [], 0, 0
    while True:
        controller.remaining()
        poll += 1
        target_wait_frames = round(poll * poll_seconds * FRAME_RATE)
        advance = emulator.command("observe-frames", target_wait_frames - wait_frames, timeout=None, deadline=controller.live_deadline)
        if advance.get("stop_reason") != "observation_interval_complete":
            raise RuntimeError("The configured observation interval did not complete.")
        wait_frames = target_wait_frames
        checked = client.command("check-dance")
        tip = checked["observation"]["tip"]
        checks.append({"poll": poll, "frame": checked["frame"], "epoch": checked["epoch"],
            "wait_emulator_frames": wait_frames, "wait_game_seconds": wait_frames / FRAME_RATE, "tip": tip})
        write_json(work / "dance-checks.json", {"interval_game_seconds": poll_seconds, "checks": checks})
        record("dance_check", poll=poll, frame=checked["frame"], green=tip["green"])
        if tip["green"]:
            break
    capture = client.command("capture")
    reading = pointer_timing(stacked_gd(work / "stage4.gd"), rank_timing=rank_timing, timing_profile=timing_profile)
    calibration = None
    if plan.get("pointer_calibration_frames"):
        calibration_frames = plan["pointer_calibration_frames"]
        if rank_timing is None:
            raise ValueError("Per-contest calibration requires the selected category/rank metrics.")

        def sample(captured, reading, path):
            pointer = reading["pointer"]
            bounds = pointer["position_bounds"]
            if pointer["status"] != "exact" or bounds is None or bounds[0] != bounds[1]:
                raise RuntimeError("Dance 1 calibration needs a readable pointer at both paused endpoints.")
            return {"frame": captured["frame"], "epoch": captured["epoch"], "pointer_x": bounds[0],
                "path": path.as_posix(), "source": reading["source"]}

        start_path = work / "pointer-calibration-start.gd"
        shutil.copy2(work / "stage4.gd", start_path)
        start = sample(capture, reading, start_path)
        write_json(work / "pointer-calibration-start.json", start)
        start_context = work / "pointer-calibration-start.lua"
        start_context.write_text("return " + lua_value(start) + "\n", encoding="utf-8", newline="\n")
        client.command("calibration-start", start_context.as_posix())
        advance = emulator.command("calibrate-pointer", timeout=None, deadline=controller.live_deadline)
        write_json(work / "pointer-calibration-inputs.json", advance)
        if (advance.get("stop_reason") != "pointer_calibration_complete"
                or advance["command_start_frame"] != start["frame"]
                or advance["command_end_frame"] != start["frame"] + calibration_frames):
            raise RuntimeError("The input-free pointer calibration did not complete its configured frame window.")
        capture = client.command("capture")
        image = stacked_gd(work / "stage4.gd")
        reading = pointer_timing(image, rank_timing=rank_timing)
        end = sample(capture, reading, work / "stage4.gd")
        write_json(work / "pointer-calibration-end.json", end)
        calibration = pointer_calibration(start, end, calibration_frames=calibration_frames)
        rank_timing = calibrated_timing(rank_timing["rank"], calibration,
            category=rank_timing.get("category", "Cool"), timing_profile=timing_profile)
        reading = pointer_timing(image, rank_timing=rank_timing)
        write_json(work / "pointer-calibration.json", {**calibration, "rank_timing": rank_timing})
        record("pointer_calibrated", emulator_frames=calibration["emulator_frames"],
            pointer_pixels=calibration["pointer_pixels"], pixels_per_frame=calibration["pixels_per_frame"],
            response_frames=rank_timing["response_frames"], cycle_frames=rank_timing["cycle_frames"],
            fixed_timing_estimates=rank_timing["fixed_timing_estimates"])
    image_path = work / "stage4.png"
    controller.screenshot(image_path)
    result = {"frame": capture["frame"], "screenshot": str(image_path), "dance_checks": checks,
        "transition_emulator_frames": capture["observation"]["transition"]["total_frames"],
        "dance_entry_poll_frames": wait_frames,
        "no_input_wait_frames": wait_frames + (calibration["emulator_frames"] if calibration else 0)}
    half_frames = (rank_timing or motion)["visible_half_frames"]
    progress, bounds = reading["progress"], reading["progress"]["frame_range"]
    evidence = {"frame": capture["frame"], "epoch": capture["epoch"], "dance": 1,
        "source": "fresh_zero_frame_left_tip_pixels", "path": (work / "stage4.gd").as_posix(),
        "tip": capture["observation"]["tip"], "phase": progress["phase"], "frame_range": bounds,
        "pointer": reading["pointer"], "pointer_source": reading["source"], "memory_proven": False,
        "pointer_calibration": calibration}
    write_json(work / "endpoint-reading.json", {**evidence, "reading": reading})
    endpoint_path = work / "endpoint.lua"
    endpoint_path.write_text("return " + lua_value(evidence) + "\n", encoding="utf-8", newline="\n")
    anchor = {"source": reading["source"], "exact": False, "demo_start_frame": None,
        "demo_start_frame_range": None, "response_start_frame": None, "response_start_frame_range": None}
    if rank_timing:
        anchor.update(rank=rank_timing["rank"], pixels_per_frame=rank_timing["pixels_per_frame"],
            profile_source=rank_timing["profile_source"])
    if bounds is not None:
        origin = capture["observation"]["transition"]["start_frame"]
        start_range = [capture["frame"] - bounds[1], capture["frame"] - bounds[0]]
        result.update(dance_start_frame_range=start_range,
            dance_start_game_seconds_range=[(frame - origin) / FRAME_RATE for frame in start_range],
            demo_percent_range=[100 * frame / half_frames for frame in bounds],
            dance_percent_range=[100 * frame / (2 * half_frames) for frame in bounds])
        start_frame = capture["frame"] - round(progress["nominal_frame"])
        anchor.update(demo_start_frame=start_frame, demo_start_frame_range=start_range,
            response_start_frame=start_frame + half_frames, response_start_frame_range=[frame + half_frames for frame in start_range])
    result.update(landing=evidence, timing_anchor=anchor, pointer_calibration=calibration, rank_timing=rank_timing)
    record("landing", frame=capture["frame"], dance_phase=progress["phase"], demo_frame_range=bounds, poll_count=poll)
    return result, client.command("visit", endpoint_path.as_posix())


def run(*, run_name, source_checkpoint, deadline=None, keep_uncapped=False, from_stage3=False, **options):
    started = time.monotonic()
    if options.get("timing_profile") is None:
        options["timing_profile"] = load_profile()
    plan = dress_plan(from_stage3=from_stage3, **options)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    receipt_path, source, previous = dress_source(source_checkpoint)
    work = ROOT / "tmp" / "contest" / run_name
    work.mkdir(parents=True, exist_ok=False)
    (work / "checkpoints").mkdir()
    shutil.copy2(receipt_path, work / "source-checkpoint.json")
    shutil.copy2(source, work / "source-state.ds0")
    source_context = {key: previous[key] for key in ("checkpoint", "run_id", "visit", "frame", "epoch")}
    config = {"run_id": run_name, "work": work.as_posix(), "source_frame": previous["frame"],
               "source_context": source_context, "target": previous.get("snapshot", {}).get("target"),
               "initial_page_taps": random.SystemRandom().randint(*INITIAL_PAGE_TAPS), **plan}
    setup = {**config, "host_pid": os.getpid(), "source_checkpoint": str(receipt_path), "source_state": str(source),
             "source_kind": "accepted_stage3_handoff" if from_stage3 else "accepted_stage3_checkpoint"}
    write_json(work / "runner-setup.json", setup)
    attachment = work / ("handoff-config.lua" if from_stage3 else "attach.lua")
    if from_stage3:
        attachment.write_text("return " + lua_value(config) + "\n", encoding="utf-8", newline="\n")
    else:
        attachment.write_text(f"assert(loadfile({lua_value((HERE / 'stage4_dress.lua').as_posix())}))({lua_value(config)})\n",
            encoding="utf-8", newline="\n")
    result = {"outcome": "fail", "stage": 4, "run_id": run_name, "work_dir": str(work),
               "source_context": source_context, "source_kind": setup["source_kind"],
                "host_timeout_seconds": plan["host_timeout_seconds"], "initial_page_taps": config["initial_page_taps"],
               "entry_mode": plan["entry_mode"], "entry_render_frames": plan["entry_render_frames"],
               "automatic_dressing": {key: plan["automatic_dressing"][key]
                    for key in ("enabled", "grid_size", "game_seconds", "budget_frames", "motion", "speed_percent")},
               "timing": plan["timing"], "stage4_accepted": False}
    controller = Stage4Controller(ControllerConfig(rom=ROM))
    emulator = DeSmuME(controller)
    emulator.states = TimedSlots(controller)
    client = CheckpointClient(emulator, work, run_name)
    backups, touched = {}, set()
    owned = attached = False
    controller.live_deadline = started + plan["host_timeout_seconds"]
    if deadline is not None:
        controller.live_deadline = min(controller.live_deadline, deadline)

    def record(phase, **details):
        event = {"phase": phase, "host_elapsed_seconds": round(time.monotonic() - started, 3), **details}
        write_json(work / "runner-status.json", event)
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    try:
        record("setup", source_frame=previous["frame"], entry_mode=plan["entry_mode"], initial_page_taps=config["initial_page_taps"])
        owners = set(controller.emulator_process_ids()) | set(controller.emulator_window_process_ids())
        if owners:
            session = controller.session()
            if owners != {session["pid"]} or project_path(session["rom"]) != ROM.resolve():
                raise RuntimeError("Stage 4 requires the sole controlled Platinum emulator.")
            if from_stage3 and not controller.paused(session):
                raise RuntimeError("The accepted Stage 3 endpoint must still be natively paused.")
        else:
            if from_stage3:
                raise RuntimeError("A direct Stage 3 handoff requires its controlled paused emulator.")
            controller.launch(ROM)
        owned = True
        controller.set_pause(True)
        emulator.stop_movie()
        for slot in (INPUT_SLOT, OUTPUT_SLOT):
            path = emulator.states.path(slot)
            backups[slot] = work / f"previous-slot-{slot}.ds{slot % 10}" if path.exists() else None
            if backups[slot] is not None:
                shutil.copy2(path, backups[slot])
        result["speed_setup"] = setup_speed(emulator)
        if from_stage3:
            result["source_handoff"] = client.command("handoff-stage4", attachment.as_posix())
            write_json(work / "source-handoff.json", result["source_handoff"])
        else:
            attach(controller, attachment)
        attached = True
        if not from_stage3:
            touched.add(INPUT_SLOT)
            shutil.copy2(work / "source-state.ds0", emulator.states.path(INPUT_SLOT))
            emulator.load_state(INPUT_SLOT, refresh=False)
        initial = client.command("inspect")
        write_json(work / "source-snapshot.json", initial)
        if initial["frame"] != previous["frame"] or not initial["observation"]["game"]["supported"]:
            raise RuntimeError(f"The Stage 3 entry does not match the retained CPUJ checkpoint: "
                               f"expected frame {previous['frame']}; actual {initial['frame']}.")
        controller.screenshot(work / "source.png")
        client.command("init")
        if plan["entry_render_frames"]:
            record("dress_render_preparation", emulator_frames=plan["entry_render_frames"], inputs="released")
            render = emulator.command("prepare-dress-render", timeout=None, deadline=controller.live_deadline)
            if render.get("stop_reason") != "dress_render_preparation_complete":
                raise RuntimeError("The configured Stage 3 load render advance did not complete.")
            prepared = client.command("dress-render-prepared")
            write_json(work / "render-preparation.json", {"entry_mode": plan["entry_mode"],
                "emulator_frames": plan["entry_render_frames"], "command": render, "confirmation": prepared,
                "counts_toward_automatic_window": True})

        def acknowledge_page(pending):
            touched.add(INPUT_SLOT)
            return client.acknowledge(pending, slot=INPUT_SLOT,
                previous_state=str(backups[INPUT_SLOT]) if backups[INPUT_SLOT] else None)

        result["automatic_dressing"] = place_accessories(emulator, client, plan, work, previous["frame"],
            config["initial_page_taps"], acknowledge=acknowledge_page, record=record)
        result["transition_speed_setup"] = emulator.set_frame_limit(False)
        target = config["target"] or {"category": "Cool", "rank": "Master"}
        timing = fixed_timing(target["category"], target["rank"], timing_profile=options["timing_profile"])
        landing, pending = locate_dance_entry(emulator, client, plan, work, record=record,
            rank_timing=timing, timing_profile=options["timing_profile"])
        result.update(landing)
        touched.add(OUTPUT_SLOT)
        accepted = client.acknowledge(pending, slot=OUTPUT_SLOT,
                                      previous_state=str(backups[OUTPUT_SLOT]) if backups[OUTPUT_SLOT] else None)
        controller.remaining()
        checkpoint = accepted["checkpoint"]
        receipt = work / "checkpoints" / f"{checkpoint['visit']:04d}-{checkpoint['checkpoint']}.json"
        handoff = {"source_stage": 4, "source_checkpoint": str(receipt),
                   "source_state": accepted["host_savestate"]["retained_path"], "source_frame": landing["frame"],
                   "source_epoch": landing["landing"]["epoch"], "run_id": run_name, "dance": 1, "phase": landing["landing"]["phase"],
                   "target": config["target"], "runner_result": str(work / "runner-result.json"),
                    "demo_frames": timing["visible_half_frames"], "response_frames": timing["response_frames"],
                    "rank_timing": timing, "timing_anchor": landing["timing_anchor"]}
        write_json(work / "dance-handoff.json", handoff)
        result.update(outcome="pass", stage4_accepted=True, checkpoint=accepted["checkpoint"],
                      save_state=accepted["host_savestate"], dance_handoff=str(work / "dance-handoff.json"))
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        result["outcome"] = "timeout" if isinstance(error, TimeoutError) or time.monotonic() >= controller.live_deadline else "fail"
        result["error"] = str(error) or type(error).__name__
        if isinstance(error, CheckpointStopped):
            result["checkpoint"] = error.record
        record("abort", outcome=result["outcome"], error=result["error"])
    finally:
        result["live_host_seconds"] = time.monotonic() - started
        controller.live_deadline = None
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
                    raise RuntimeError("Stage 4 finalization did not leave paused, released inputs.")
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
            result["stage4_accepted"] = False
        result["cleanup_errors"] = cleanup
        result["total_host_seconds"] = time.monotonic() - started
        write_json(work / "runner-result.json", result)
        record("complete", outcome=result["outcome"], error=result.get("error"),
               emulator_paused=result.get("emulator_paused"), checkpoint_slot=OUTPUT_SLOT)
    return result


def check(*, lua_dll=None):
    from contest.checks import check_module

    check_module(__file__, ("stage4_dress.lua", "dress_pixels.lua"), "Stage 4 Python/Lua syntax OK", lua_dll=lua_dll)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--source-checkpoint")
    parser.add_argument("--host-timeout", type=float)
    parser.add_argument("--from-stage3", action="store_true", help="Use the current accepted paused Stage 3 handoff without loading.")
    parser.add_argument("--dress-grid", type=int, choices=(6, 7, 8), default=DRESS_GRID)
    parser.add_argument("--dress-seconds", type=float, help="Override profile dressing game seconds.")
    parser.add_argument("--pickup-frames", type=int)
    parser.add_argument("--drag-frames", type=int)
    parser.add_argument("--drag-release-frames", type=int)
    parser.add_argument("--page-wait-frames", type=int)
    parser.add_argument("--transition-seconds", type=float)
    parser.add_argument("--stylus-x", type=int, default=230)
    parser.add_argument("--stylus-y", type=int, default=173)
    parser.add_argument("--stylus-hold-frames", type=int)
    parser.add_argument("--stylus-release-frames", type=int)
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
    options = {key: getattr(args, key) for key in ("host_timeout", "transition_seconds",
               "stylus_x", "stylus_y", "stylus_hold_frames", "stylus_release_frames", "from_stage3",
               "dress_grid", "dress_seconds", "pickup_frames", "drag_frames", "drag_release_frames", "page_wait_frames")}
    try:
        plan = dress_plan(**options)
        if args.plan_only:
            if args.source_checkpoint:
                _, source, previous = dress_source(args.source_checkpoint)
                plan.update(source_state=str(source), source_frame=previous["frame"])
            print(json.dumps(plan, indent=2))
            return 0
        if not args.run_name or not args.source_checkpoint:
            parser.error("Execution requires --run-name and --source-checkpoint.")
        result = run(run_name=args.run_name, source_checkpoint=args.source_checkpoint, **options)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
    return 0 if result["outcome"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
