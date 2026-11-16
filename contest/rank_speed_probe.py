"""Measure two prepared contest ranks, with optional Dance 1 response trials."""

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

from contest.dance_master import DEFAULT_OFFSET_FRAMES, attach, lua_value, settle_native_overlay
from contest.dance_timings import timing_for
from contest.pt_dance_read import (
    CUE_SIDE_OFFSET, LEFT_SPRITES, RIGHT_SPRITES, _match, gd_image, layout_for, load_image,
    merge_buttons, read_bar, read_pointer, read_progress,
)
from contest.runtime import ROM, ROOT, TimedController, TimedSlots
from contest.stage4_dress import stacked_gd
from host.checkpoints import CheckpointClient, write_json
from host.controller import ControllerConfig
from host.emulator import DeSmuME
from host.savestates import StateSlots
from contest.dance_timings import fixed_timing
from contest.timing_profiles import load_profile, section, setting


HERE = Path(__file__).resolve().parent
TEMPORARY_SLOT = 10
BRIDGE = "pt-contest-rank-speed"


class ReviewNeeded(RuntimeError):
    """Fresh image evidence needs review at the current paused endpoint."""


def pointer_position(image):
    bar, pointer = read_bar(image), read_pointer(image)
    progress = read_progress(bar, pointer)
    bounds = progress["position_bounds"]
    if bounds is None or bounds[0] != bounds[1] or progress["status"] == "conflict":
        raise ReviewNeeded("The fresh pointer/strip position is unreadable or conflicting.")
    return {"x": bounds[0], "phase": progress["phase"],
        "source": progress.get("position_source"), "bar": bar, "pointer": pointer}


def cue_positions(image, side, dance=1):
    """Use the existing exact sprites at actual centers for the selected dance."""
    sprites = LEFT_SPRITES if side == "left" else RIGHT_SPRITES
    y = 168 if side == "left" else layout_for(dance)["right_marker_y"] - 8
    centers = range(8, 128) if side == "left" else range(128, 249)
    px, result = image.load(), []
    for x in centers:
        candidates = []
        for button, sprite in sprites.items():
            matched = _match(px, x - 8, y, sprite)
            if matched:
                candidates.append({"x": x, "button": button, "source_side": side, **matched})
        if len(candidates) == 1:
            result.extend(candidates)
    return result


def merge_cues(left, right, *, expected_cues, dance, side_offset_pixels=CUE_SIDE_OFFSET, fixed_spacing=False):
    """Pair cues at fixed spacing or resolve visible pairs; fill missing views."""
    offsets = {side_offset_pixels}
    if not fixed_spacing:
        offsets.update(b["x"] - a["x"] for a in left for b in right
            if a["button"] == b["button"] and 0 < b["x"] - a["x"] < 256)

    def pair_at(offset):
        sides = {"left": {cue["x"]: cue for cue in left},
                 "right": {cue["x"] - offset: cue for cue in right}}
        kept, discarded = {}, []
        for side, other in (("left", "right"), ("right", "left")):
            kept[side] = []
            for x, cue in sides[side].items():
                if len(sides[side]) > expected_cues and x not in sides[other]:
                    discarded.append({"side": side, **cue, "reason": "unpaired_excess_cue"})
                else:
                    kept[side].append({"demo_x": x, "status": "readable", "candidates": [cue]})
        value = merge_buttons(kept["left"], kept["right"], "right", "error")
        value.update(side_offset_pixels=offset, discarded_cues=discarded,
            paired_cues=sum(cue["cross_checked"] for cue in value["accepted"]),
            paired_positions=len(set(sides["left"]) & set(sides["right"])))
        value["positions_valid"] = all(8 <= cue["demo_x"] < 128 and 128 <= cue["demo_x"] + offset <= 248
            for cue in value["accepted"])
        return value

    candidates = [pair_at(offset) for offset in sorted(offsets)]
    complete = [value for value in candidates if not value["conflicts"] and value["positions_valid"]
                and len(value["accepted"]) == expected_cues]
    if complete:
        strongest = max(value["paired_cues"] for value in complete)
        best = [value for value in complete if value["paired_cues"] == strongest]
        hinted = next((value for value in best if value["side_offset_pixels"] == side_offset_pixels), None)
        if len(best) == 1 and (strongest >= 2 or hinted is not None):
            paired = best[0]
        elif strongest <= 1 and hinted is not None:
            paired = hinted
        else:
            raise ReviewNeeded(f"Dance {dance}: readable cues do not establish one supported mirror spacing.")
    else:
        paired = max(candidates, key=lambda value: (value["paired_positions"], value["paired_cues"],
            value["side_offset_pixels"] == side_offset_pixels))
    if paired["conflicts"]:
        x = paired["conflicts"][0]["demo_x"]
        raise ReviewNeeded(f"Dance {dance}: readable cue sides disagree at native demo x={x}.")
    if len(paired["accepted"]) != expected_cues:
        raise ReviewNeeded(f"Dance {dance}: expected {expected_cues} readable cue pairs; found {len(paired['accepted'])}.")
    if not paired["positions_valid"]:
        raise ReviewNeeded(f"Dance {dance}: resolved cue positions are outside the demo/response strips.")
    offset = paired["side_offset_pixels"]
    for cue in paired["accepted"]:
        cue["response_x"] = cue["demo_x"] + offset
        cue["position_source"] = "native_right_marker" if "right" in cue["evidence"] else "mirrored_readable_left_cue"
    paired.update(left=left, right=right, side_offset_hint_pixels=side_offset_pixels,
        side_offset_source="fresh_paired_cue_centers" if not fixed_spacing and paired["paired_cues"] >= 2 else "configured_cue_spacing")
    return paired


def select_cues(left, right, *, expected_cues, side_offset_pixels):
    """Select readable edge cues for main/debug responses, including partial reads."""
    if expected_cues not in (3, 4):
        raise ValueError("Dancing requires three or four cue opportunities.")
    offset, discarded = side_offset_pixels, []
    sides = {"left": {}, "right": {}}
    for side, readings in (("left", left), ("right", right)):
        for reading in readings:
            x = reading["x"] - (offset if side == "right" else 0)
            if not (8 <= x < 128 and 128 <= x + offset <= 248):
                discarded.append({"side": side, **reading, "reason": "outside_cue_strips"})
            else:
                sides[side][x] = reading
    a, b = sides["left"], sides["right"]
    paired = sorted(x for x in a.keys() & b.keys() if a[x]["button"] == b[x]["button"])
    conflicts = [{"demo_x": x, "readings": {"left": a[x], "right": b[x]}}
                 for x in sorted(a.keys() & b.keys()) if a[x]["button"] != b[x]["button"]]
    prefix = expected_cues - 2
    excess = len(a) > expected_cues or len(b) > expected_cues
    incomplete = len(a) < expected_cues and len(b) < expected_cues
    if excess or incomplete:
        positions = paired if len(paired) <= expected_cues else paired[:prefix] + paired[-2:]
        selections = [(x, "left" if index < prefix else "right") for index, x in enumerate(positions)]
        policy = "edge_pairs" if len(paired) > expected_cues else "confirmed_pairs_only"
    elif len(a) == len(b) == expected_cues:
        selections = [(x, "left") for x in sorted(a)[:prefix]] + [(x, "right") for x in sorted(b)[-2:]]
        policy = "demo_prefix_response_suffix"
    else:
        complete = a if len(a) == expected_cues else b
        selections = [(x, "left" if index < prefix else "right") for index, x in enumerate(sorted(complete))]
        policy = "complete_side_positions"
    accepted = {}
    for x, preferred in selections:
        readings = {side: values[x] for side, values in sides.items() if x in values}
        selected = preferred if preferred in readings else next(iter(readings))
        button = readings[selected]["button"]
        evidence = {side: reading for side, reading in readings.items() if reading["button"] == button}
        accepted[x] = {"demo_x": x, "response_x": x + offset, "button": button,
            "source_side": selected, "fallback": selected != preferred, "cross_checked": len(evidence) == 2,
            "evidence": evidence, "conflict_resolved": len(readings) == 2 and len(evidence) == 1,
            "position_source": "native_right_marker" if "right" in evidence else "mirrored_readable_left_cue"}
    for side, readings in sides.items():
        for x, reading in readings.items():
            if x not in accepted or side not in accepted[x]["evidence"]:
                discarded.append({"side": side, **reading, "reason":
                    "conflicting_side_ignored" if x in accepted else "unselected_cue"})
    cues = [accepted[x] for x in sorted(accepted)]
    return {"accepted": cues, "conflicts": conflicts, "discarded_cues": discarded,
        "left": left, "right": right, "selection_policy": policy,
        "expected_cues": expected_cues, "unresolved_cues": expected_cues - len(cues),
        "paired_cues": sum(cue["cross_checked"] for cue in cues), "positions_valid": True,
        "side_offset_pixels": offset, "side_offset_hint_pixels": offset,
        "side_offset_source": "configured_cue_spacing"}


def fit_speed(points):
    if len(points) < 2 or any(points[index][0] <= points[index - 1][0] for index in range(1, len(points))):
        raise ValueError("Speed fitting requires increasing emulator frames.")
    mean_frame = sum(frame for frame, _ in points) / len(points)
    mean_x = sum(x for _, x in points) / len(points)
    denominator = sum((frame - mean_frame) ** 2 for frame, _ in points)
    speed = sum((frame - mean_frame) * (x - mean_x) for frame, x in points) / denominator
    if not math.isfinite(speed) or speed <= 0:
        raise ReviewNeeded("The captured pointer trajectory has no positive speed.")
    return {"pixels_per_emulator_frame": speed, "frames_per_pixel": 1 / speed,
        "sample_count": len(points), "estimated_demo_half_frames": 120 / speed,
        "maximum_pixel_residual": max(abs(x - mean_x - speed * (frame - mean_frame)) for frame, x in points),
        "source": "counted_frame_callback_positions_linear_fit"}


def measurement_speed(start, end, samples):
    frames, pixels = end["frame"] - start["frame"], end["position"]["x"] - start["position"]["x"]
    if start["epoch"] != end["epoch"] or frames <= 0 or pixels <= 0:
        raise ReviewNeeded("The measurement changed source epoch or lacked forward pointer/frame progress.")
    if start["position"]["phase"] != "demo" or end["position"]["phase"] != "demo":
        raise ReviewNeeded("The timed measurement crossed the demonstration midpoint.")
    if any(sample["epoch"] != start["epoch"] for sample in samples):
        raise ReviewNeeded("The timed trajectory contains a state change.")
    callback_points = [(sample["frame"], sample["bar"]["last_x"]) for sample in samples
        if sample["source"] == "frame_callback" and sample["bar"]["found"]]
    endpoint_speed = pixels / frames
    fit = fit_speed(callback_points) if len(callback_points) >= 2 else {
        "pixels_per_emulator_frame": endpoint_speed, "frames_per_pixel": 1 / endpoint_speed,
        "sample_count": 2, "estimated_demo_half_frames": 120 / endpoint_speed,
        "source": "fresh_paused_endpoint_displacement"}
    return {"emulator_frames": frames, "pointer_pixels": pixels,
        "endpoint_pixels_per_emulator_frame": endpoint_speed,
        "endpoint_frames_per_pixel": frames / pixels, "fit": fit,
        "trial_speed": fit["pixels_per_emulator_frame"],
        "estimated_full_sweep_frames": 240 / fit["pixels_per_emulator_frame"],
        "unreadable_frames": [sample["frame"] for sample in samples if not sample["bar"]["found"]],
        "source": "fresh_paused_endpoints_and_counted_input_free_trajectory"}


def response_plan(midpoint, image, samples, read_frame, *, rank, expected_cues, speed, hold_frames, offset_frames,
                  dance=1, side_offset_pixels=None, allow_demo_midpoint=False, fixed_spacing=False, tolerant_inputs=False):
    side_offset = timing_for(rank)["cue_side_offset_pixels"] if side_offset_pixels is None else side_offset_pixels
    seen = {"left": {}, "right": {}}
    changed = {side: set() for side in seen}
    previous = None
    for sample in samples:
        if sample["epoch"] != midpoint["epoch"] or previous is not None and sample["frame"] <= previous:
            raise ReviewNeeded("The demo captures changed epoch or did not increase in frame order.")
        previous = sample["frame"]
        if sample.get("readable") is False:
            continue
        captured = gd_image(read_frame(sample))
        for side in seen:
            for cue in cue_positions(captured, side, dance):
                old = seen[side].get(cue["x"])
                if old and old["button"] != cue["button"]:
                    if not tolerant_inputs:
                        raise ReviewNeeded("A demonstrated cue changed at the same native position.")
                    changed[side].add(cue["x"])
                seen[side][cue["x"]] = cue
    fresh = {side: cue_positions(image, side, dance) for side in seen}
    resolved = None
    if tolerant_inputs and not fixed_spacing:
        try:
            resolved = merge_cues(fresh["left"], fresh["right"], expected_cues=expected_cues, dance=dance,
                side_offset_pixels=side_offset)
        except ReviewNeeded:
            pass
    if tolerant_inputs:
        paired = select_cues(fresh["left"], fresh["right"], expected_cues=expected_cues,
            side_offset_pixels=resolved["side_offset_pixels"] if resolved else side_offset)
        if resolved:
            paired["side_offset_source"] = resolved["side_offset_source"]
    else:
        paired = merge_cues(fresh["left"], fresh["right"], expected_cues=expected_cues, dance=dance,
            side_offset_pixels=side_offset, fixed_spacing=fixed_spacing)
    for side in seen:
        for cue in fresh[side]:
            old = seen[side].get(cue["x"])
            if old and old["button"] != cue["button"]:
                if not tolerant_inputs:
                    raise ReviewNeeded("The fresh midpoint contradicts an earlier cue.")
                changed[side].add(cue["x"])
            seen[side][cue["x"]] = cue
    if not tolerant_inputs:
        history = merge_cues(*(list(values.values()) for values in seen.values()), expected_cues=expected_cues, dance=dance,
            side_offset_pixels=paired["side_offset_pixels"], fixed_spacing=fixed_spacing)
        if [(cue["demo_x"], cue["button"]) for cue in paired["accepted"]] != \
                [(cue["demo_x"], cue["button"]) for cue in history["accepted"]]:
            raise ReviewNeeded("The fresh cue pairs contradict the demonstrated cues.")
    position = midpoint["position"]
    if position["phase"] != "response" and not (allow_demo_midpoint is True
            and position["phase"] == "demo" and 8 <= position["x"] < 128):
        raise ReviewNeeded("The fresh capture has not reached the response midpoint.")
    presses, points, accepted, skipped = [], [], [], []
    px = image.load()
    predicted_end = midpoint["frame"] + math.floor((248 - position["x"]) / speed + 0.5)
    for input_index, cue in enumerate(paired["accepted"], 1):
        due = midpoint["frame"] + math.floor((cue["response_x"] - position["x"]) / speed + 0.5) + offset_frames
        reason = "cue_changed_during_demo" if any(reading["x"] in changed[side] for side, reading in cue["evidence"].items()) \
            else "input_already_due" if due <= midpoint["frame"] else "input_outside_response" \
            if due + hold_frames >= predicted_end else "overlapping_input" \
            if presses and presses[-1]["frame"] + hold_frames >= due else None
        if tolerant_inputs and reason:
            skipped.append({"input_index": input_index, "button": cue["button"], "response_x": cue["response_x"], "frame": due, "reason": reason})
            continue
        presses.append({"button": cue["button"], "demo_x": cue["demo_x"], "response_x": cue["response_x"], "frame": due})
        if tolerant_inputs:
            presses[-1]["input_index"] = input_index
        cue_points = []
        for side, reading in cue["evidence"].items():
            sprites = LEFT_SPRITES if side == "left" else RIGHT_SPRITES
            marker_y = 168 if side == "left" else layout_for(dance)["right_marker_y"] - 8
            for dx, dy, _ in sprites[cue["button"]].points:
                x, y = reading["x"] - 8 + dx, marker_y + dy
                cue_points.append([x, y, *px[x, y]])
        points.extend(cue_points)
        if tolerant_inputs:
            cue["pixel_points"] = cue_points
        accepted.append(cue)
    paired["accepted"] = accepted
    for index, press in enumerate(presses):
        following = presses[index + 1]["frame"] if index + 1 < len(presses) else predicted_end
        if press["frame"] <= midpoint["frame"] or press["frame"] + hold_frames >= following:
            raise ReviewNeeded("The measured speed/offset does not fit the observed response markers.")
    result = {"rank": rank, "dance": dance, "frame": midpoint["frame"], "epoch": midpoint["epoch"],
        "pointer_x": position["x"], "speed": speed, "hold_frames": hold_frames,
        "offset_frames": offset_frames, "player_center_x": 204 - 80 * ((dance - 1) // 2), "presses": presses,
        "predicted_end_frame": predicted_end, "response_end_frame": predicted_end, "pixel_points": points,
        "cues": paired, "source": "fresh_native_cues_with_per_button_fallback"}
    if tolerant_inputs:
        result.update(expected_inputs=expected_cues, skipped_input_count=expected_cues - len(presses), skipped_inputs=skipped)
    return result


def skipped_trial(midpoint, source, *, speed, hold_frames, offset_frames, reason):
    """Finish an unreadable prepared response using its existing native half-sweep budget."""
    x = midpoint["position"]["x"]
    anchor = (midpoint.get("midpoint_timing") or {}).get("expected_frame")
    frames = math.floor((248 - x) / speed + 0.5) if x is not None else math.ceil(120 / speed)
    if anchor is not None and (x is None or anchor > midpoint["frame"]):
        frames = anchor - midpoint["frame"] + math.ceil(120 / speed)
    endpoint = midpoint["frame"] + max(1, frames)
    return {"rank": source["rank"], "dance": 1, "frame": midpoint["frame"], "epoch": midpoint["epoch"],
        "pointer_x": None, "speed": speed, "hold_frames": hold_frames, "offset_frames": offset_frames,
        "player_center_x": 204, "presses": [], "pixel_points": [], "predicted_end_frame": endpoint,
        "response_end_frame": endpoint, "expected_inputs": source["expected_cues"],
        "skipped_input_count": source["expected_cues"], "skipped_inputs": [{"reason": reason, "count": source["expected_cues"]}],
        "skip_reason": reason, "source": "retained_paused_capture_and_counted_input_free_response",
        "cues": {"accepted": [], "conflicts": [], "side_offset_pixels": source["rank_timing"]["cue_side_offset_pixels"]}}


def probe_plan(*, super_slot=1, hyper_slot=2, render_host_seconds=None, measurement_host_seconds=None,
        host_timeout=300, hold_frames=None, offset_frames=None, measure_only=False, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    diagnostic, dance = section(profile, "diagnostics"), section(profile, "stage5_dancing")
    render_host_seconds = setting(render_host_seconds, diagnostic, "rank_state_render_host_seconds")
    measurement_host_seconds = setting(measurement_host_seconds, diagnostic, "rank_measurement_host_seconds")
    hold_frames = setting(hold_frames, dance, "response_hold_frames")
    offset_frames = setting(offset_frames, dance, "input_offset_frames")
    gaps = fixed_timing("Cool", "Hyper", timing_profile=profile)
    for slot in (super_slot, hyper_slot):
        StateSlots.validate_slot(slot)
        if slot == TEMPORARY_SLOT:
            raise ValueError("Prepared source slots must differ from temporary checkpoint slot 10.")
    if super_slot == hyper_slot:
        raise ValueError("Prepare separate Great/Super and Ultra/Hyper slots.")
    for value in (render_host_seconds, measurement_host_seconds, host_timeout):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError("Render, measurement, and timeout values must be positive host seconds.")
    if isinstance(hold_frames, bool) or not isinstance(hold_frames, int) or hold_frames < 1:
        raise ValueError("Button holds must be positive emulator-frame counts.")
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise ValueError("The response offset must be an integer emulator-frame count.")
    return {"game": profile["game_code"], "timing_profile_source": profile["source"],
        "demo_sample_interval_frames": dance["demo_sample_interval_frames"], "sources": [
        {"rank": "Super", "japanese_rank": "Great", "slot": super_slot, "expected_cues": 4,
            "rank_timing": timing_for("Super", timing_profile=profile)},
        {"rank": "Hyper", "japanese_rank": "Ultra", "slot": hyper_slot, "expected_cues": 4,
            "rank_timing": timing_for("Hyper", timing_profile=profile)}],
        "render_host_seconds": render_host_seconds, "measurement_host_seconds": measurement_host_seconds,
        "host_timeout_per_source_seconds": host_timeout, "speed_percent": 100, "frame_limit": True,
        "fast_forward": False, "hold_frames": hold_frames, "offset_frames": offset_frames,
        "feedback_release_frames": dance["feedback_release_frames"],
        "first_feedback_delay_frames": hold_frames + dance["feedback_release_frames"] - 1,
        "native_osd_settle_host_seconds": dance["native_overlay_settle_host_seconds"],
        "measure_only": measure_only,
        "order": "measure both sources" if measure_only else "measure both sources, then reload each source for one complete Dance 1 response",
        "capture_policy": "native pause, fresh paused-Lua-menu framebuffer, zero added frames",
        "speed_policy": "endpoint pixel displacement divided by actual emulator-frame change; recorded trajectory refines the trial rate",
        "cue_policy": "80-percent button-cue matching at native centers; per-button agreement or silent one-sided fallback",
        "finish_to_next_start_gap_frames": {"same_pokemon": gaps["dance_gap_frames"], "pokemon_switch": gaps["leader_gap_frames"]},
        "temporary_checkpoint_slot": TEMPORARY_SLOT,
        "deadline_policy": "per-source absolute host deadline from its first measurement; setup belongs to the first source",
        "finish": ("both pointer-speed measurements acknowledged, or an unexpected paused stop" if measure_only
            else "both counted Dance 1 responses finish with every opportunity executed or skipped and feedback recorded as Excellent or Miss"),
        "memory_proven_phase": False}


def lua_file(path, value):
    path.write_text("return " + lua_value(value) + "\n", encoding="utf-8", newline="\n")
    return path


def unpause_window(controller, seconds):
    """Inputs stay released in Lua; host seconds and emulator frames remain distinct."""
    controller.remaining()
    session = controller.session()
    if not controller.paused(session):
        raise RuntimeError("The timed input-free window requires native pause.")
    controller.set_pause(False)
    if controller.paused(session):
        raise RuntimeError("The timed window did not reach native unpause.")
    started = time.monotonic()
    try:
        until = started + seconds
        while True:
            controller.remaining()
            if controller.paused(session):
                raise RuntimeError("Native pause interrupted the input-free host-time window.")
            left = until - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(0.05, left, controller.remaining()))
        running_seconds = time.monotonic() - started
    finally:
        controller.set_pause(True)
    if not controller.paused(session):
        raise RuntimeError("The timed window did not return to confirmed native pause.")
    return {"requested_host_seconds": seconds, "running_host_seconds": running_seconds,
        "host_seconds_to_confirmed_pause": time.monotonic() - started, "emulator_paused": True, "input_policy": "none"}


def capture(client, name):
    result = client.command("capture", name)
    return capture_result(result)


def capture_result(result, *, allow_unreadable=False):
    image = result["observation"]["image"]
    path = Path(image["path"])
    if (result.get("emulator_paused") is not True or image["capture_callback"] != "paused_lua_menu"
            or result["observation"].get("released") is not True or image.get("source") != "fresh_zero_frame_framebuffer"
            or image["direct_read_frames"] != 0 or image["frame"] != result["frame"] or image["epoch"] != result["epoch"]):
        raise RuntimeError("The fresh capture frame/epoch does not match its paused packet.")
    stacked_gd(path).save(path.with_suffix(".png"))
    try:
        position = pointer_position(gd_image(path.read_bytes()))
    except ReviewNeeded as error:
        if not allow_unreadable:
            raise
        position = {"x": None, "phase": "unknown", "source": "unreadable_native_pixels", "reason": str(error)}
    value = {"frame": result["frame"], "epoch": result["epoch"], "image": image,
        "png": str(path.with_suffix(".png")), "position": position}
    if isinstance(result["observation"].get("midpoint_timing"), dict):
        value["midpoint_timing"] = result["observation"]["midpoint_timing"]
    return value


def run(*, run_name, **options):
    settings = probe_plan(**options)
    if not run_name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Execution requires a fresh run name with letters, digits, underscores, or hyphens.")
    parent = ROOT / "tmp/contest"
    if not parent.parent.is_dir():
        raise ValueError("The project-local tmp directory must exist.")
    parent.mkdir(exist_ok=True)
    work = parent / run_name
    work.mkdir(exist_ok=False)
    (work / "checkpoints").mkdir()
    started = time.monotonic()
    controller = TimedController(ControllerConfig(rom=ROM))
    controller.timeouts_enabled = False
    first_rank = settings["sources"][0]["rank"]
    deadlines = {first_rank: started + settings["host_timeout_per_source_seconds"]}
    controller.live_deadline = deadlines[first_rank]
    emulator = DeSmuME(controller)
    emulator.states = TimedSlots(controller)
    client = CheckpointClient(emulator, work, run_name)
    result = {"outcome": "fail", "run_id": run_name, "host_pid": os.getpid(), "work_dir": str(work),
        "settings": settings, "measurements": [], "trials": [], "cleanup_errors": []}
    write_json(work / "runner-setup.json", {"run_id": run_name, "work_dir": str(work),
        "host_pid": os.getpid(), "settings": settings})
    owned = attached = slot10_touched = False
    original_slot10, rendering_before = None, None
    source_copies, current_folder = {}, work

    def record(phase, **facts):
        event = {"phase": phase, "host_elapsed_seconds": round(time.monotonic() - started, 3), **facts}
        write_json(work / "runner-progress.json", event)
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    def acknowledge(name, facts):
        nonlocal slot10_touched
        context = lua_file(current_folder / f"{name}-context.lua", facts)
        pending = client.command("visit", name, context.as_posix())
        slot10_touched = True
        return client.acknowledge(pending, slot=TEMPORARY_SLOT)

    def settle(name):
        evidence = settle_native_overlay(controller, lambda name: client.command("capture", name),
            settings["native_osd_settle_host_seconds"], name=name)
        if evidence is not None:
            write_json(current_folder / f"overlay-{name}.json", evidence)

    def load_source(source, phase):
        nonlocal current_folder
        rank = source["rank"]
        deadlines.setdefault(rank, time.monotonic() + settings["host_timeout_per_source_seconds"])
        controller.live_deadline = deadlines[rank]
        controller.remaining()
        current_folder = work / f"{rank.lower()}-{phase}"
        current_folder.mkdir(exist_ok=False)
        loaded = emulator.load_state(source["slot"], refresh=False)
        selected = client.command("select-source", rank, current_folder.as_posix(), source["expected_cues"], phase)
        write_json(current_folder / "source-load.json", {**loaded, "frame": selected["frame"], "epoch": selected["epoch"],
            "rank": rank, "rank_source": "user_prepared_native_slot", "source_copy": str(source_copies[source["slot"]]),
            "host_deadline": deadlines[rank]})
        rendered = unpause_window(controller, settings["render_host_seconds"])
        settle("source")
        start = capture_result(client.command("capture", "rendered-entry"), allow_unreadable=phase == "trial")
        if start["epoch"] != selected["epoch"] or start["frame"] <= selected["frame"]:
            raise ReviewNeeded("The render preparation changed epoch or did not advance emulator frames.")
        write_json(current_folder / "render-preparation.json", {**rendered, "start_frame": selected["frame"],
            "end_frame": start["frame"], "emulator_frames": start["frame"] - selected["frame"], "capture": start})
        return start

    try:
        for source in settings["sources"]:
            native = emulator.states.path(source["slot"], data={"rom": str(ROM)})
            StateSlots.validate_header(native, source["slot"])
            retained = work / f"prepared-slot-{source['slot']}.ds{source['slot'] % 10}"
            shutil.copy2(native, retained)
            source_copies[source["slot"]] = retained
        owners = set(controller.emulator_process_ids()) | set(controller.emulator_window_process_ids())
        if owners:
            session = controller.session()
            if owners != {session["pid"]} or Path(session["rom"]).resolve() != ROM.resolve():
                raise RuntimeError("The probe requires the sole controlled Platinum emulator.")
            dialogs = [item["handle"] for item in controller.windows(session["pid"])
                if controller.USER32.GetDlgItem(item["handle"], 310)]
            if len(dialogs) > 1:
                raise RuntimeError("The probe requires one Lua script window.")
        else:
            controller.launch(ROM)
        owned = True
        controller.set_pause(True)
        emulator.stop_movie()
        result["speed_setup"] = emulator.restore_normal_speed()
        rendering_before = {"automatic": bool(emulator.menu_state(221) & 0x8),
            "skip_menu": next((item for item in range(191, 201) if emulator.menu_state(item) & 0x8), None)}
        window = controller.session()["window"]
        if rendering_before["automatic"]:
            controller.send(window, 0x111, 221)
        if not emulator.menu_state(191) & 0x8:
            controller.send(window, 0x111, 191)
        if emulator.menu_state(221) & 0x8 or not emulator.menu_state(191) & 0x8:
            raise RuntimeError("Every-frame rendering was not confirmed.")
        native10 = emulator.states.path(TEMPORARY_SLOT)
        if native10.exists():
            original_slot10 = work / "previous-slot-10.ds0"
            shutil.copy2(native10, original_slot10)
        attach(controller, HERE / "rank_speed_probe.lua", bridge=BRIDGE)
        attached = True
        client.command("init-run", run_name, work.as_posix(),
            lua_file(work / "timing-settings.lua", settings).as_posix())
        controller.USER32.SetForegroundWindow(window)

        # Finish both speed measurements before replaying either prepared source.
        for source in settings["sources"]:
            start = load_source(source, "measure")
            acknowledge("measure-start", {"frame": start["frame"], "epoch": start["epoch"], "pointer_x": start["position"]["x"]})
            settle("measurement")
            client.command("measure-begin")
            timed = unpause_window(controller, settings["measurement_host_seconds"])
            end = capture_result(client.command("measure-end"))
            samples = [json.loads(line) for line in (current_folder / "pointer-samples.jsonl").read_text(encoding="utf-8").splitlines()]
            speed = measurement_speed(start, end, samples)
            completed = acknowledge("measure-end", {"frame": end["frame"], "epoch": end["epoch"],
                "pointer_x": end["position"]["x"], "start_frame": start["frame"], "start_x": start["position"]["x"]})
            measured = {"rank": source["rank"], "slot": source["slot"], "start": start, "end": end,
                "timed_window": timed, "speed": speed, "checkpoint_visit": completed["checkpoint"]["visit"]}
            write_json(current_folder / "measurement.json", measured)
            result["measurements"].append(measured)
            record("measured", rank=source["rank"], frames=speed["emulator_frames"], pixels=speed["pointer_pixels"],
                endpoint_speed=speed["endpoint_pixels_per_emulator_frame"], fitted_speed=speed["trial_speed"])

        speeds = {value["rank"]: value["speed"]["trial_speed"] for value in result["measurements"]}
        trial_sources = [] if settings["measure_only"] else settings["sources"]
        for source in trial_sources:
            entry = load_source(source, "trial")
            try:
                x = entry["position"]["x"]
                if entry["position"]["phase"] != "demo":
                    reason = "The prepared trial entry is unreadable or already in its response; using counted continuation."
                    client.command("warn", reason)
                    record("dance_warning", rank=source["rank"], reason=reason)
                predicted_midpoint = entry["frame"] + (max(1, math.ceil((128 - x) / speeds[source["rank"]]))
                    if x is not None else math.ceil(120 / speeds[source["rank"]]))
                entry["midpoint_timing"] = {"expected_frame": predicted_midpoint}
                observed = emulator.command("observe-demo", predicted_midpoint, timeout=None, deadline=controller.live_deadline)
                if observed.get("error") or observed.get("stop_reason") != "demo_midpoint":
                    raise ReviewNeeded(observed.get("error") or "The demonstration needs counted response continuation.")
                midpoint = capture_result(client.command("capture", "midpoint"), allow_unreadable=True)
                samples = [json.loads(line) for line in (current_folder / "demo-frames.jsonl").read_text(encoding="utf-8").splitlines()]
                with (current_folder / "demo-frames.bin").open("rb") as stream:
                    def read_frame(sample):
                        stream.seek(sample["capture_offset"])
                        return stream.read(sample["capture_bytes"])
                    planned = response_plan(midpoint, gd_image(Path(midpoint["image"]["path"]).read_bytes()), samples, read_frame,
                        rank=source["rank"], expected_cues=source["expected_cues"], speed=speeds[source["rank"]],
                        hold_frames=settings["hold_frames"], offset_frames=settings["offset_frames"],
                        side_offset_pixels=source["rank_timing"]["cue_side_offset_pixels"], tolerant_inputs=True)
                write_json(current_folder / "response-plan.json", planned)
                loaded = client.command("plan", lua_file(current_folder / "response-plan.lua", planned).as_posix())
                if loaded.get("recovery_required"):
                    raise ReviewNeeded(loaded["warning"])
                acknowledge("trial-start", {"frame": midpoint["frame"], "epoch": midpoint["epoch"]})
                settle("response")
                responded = emulator.command("mirror", timeout=None, deadline=controller.live_deadline)
                if responded.get("error") or responded.get("stop_reason") != "dance_1_response_complete":
                    raise ReviewNeeded(responded.get("error") or "The response needs counted input-free continuation.")
            except Exception as error:
                if isinstance(error, TimeoutError):
                    raise
                reason = (str(error) or type(error).__name__).replace("|", "/").replace("\r", " ").replace("\n", " ")
                midpoint = capture_result(client.command("capture", "response-recovery"), allow_unreadable=True)
                midpoint.setdefault("midpoint_timing", entry.get("midpoint_timing", {}))
                planned = skipped_trial(midpoint, source, speed=speeds[source["rank"]], hold_frames=settings["hold_frames"],
                    offset_frames=settings["offset_frames"], reason=reason)
                recovered = client.command("recover-response", lua_file(current_folder / "response-skip.lua", planned).as_posix(), reason)
                planned = recovered["response_plan"]
                write_json(current_folder / "response-recovery.json", {"capture": midpoint, "plan": planned, "reason": reason})
                if not recovered["observation"]["trial"]["response_started"]:
                    acknowledge("trial-start", {"frame": recovered["frame"], "epoch": recovered["epoch"]})
                record("dance_warning", rank=source["rank"], reason=reason, continuation="counted_input_free_response")
                settle("recovery")
                responded = emulator.command("mirror", timeout=None, deadline=controller.live_deadline)
            completed = acknowledge("trial-finish", {"frame": responded["frame"], "epoch": midpoint["epoch"],
                "expected_cues": source["expected_cues"]})
            snapshot = completed["checkpoint"]["snapshot"]
            stacked_gd(snapshot["image"]["path"]).save(Path(snapshot["image"]["path"]).with_suffix(".png"))
            for image_path in current_folder.glob("judgment-*.gd"):
                stacked_gd(image_path).save(image_path.with_suffix(".png"))
            trial = {"rank": source["rank"], "slot": source["slot"], "outcome": "pass", "entry": entry,
                "midpoint": midpoint, "end_frame": responded["frame"], "actual_response_frames": responded["frame"] - midpoint["frame"],
                "speed": speeds[source["rank"]], "presses": planned["presses"], "judgments": snapshot["trial"]["judgments"],
                "excellent_count": snapshot["trial"]["excellent_count"], "miss_count": snapshot["trial"]["miss_count"],
                "input_count": snapshot["trial"]["inputs"], "skipped_input_count": snapshot["trial"]["skipped_input_count"],
                "checkpoint_visit": completed["checkpoint"]["visit"], "source": "fresh_judgment_pixels_and_response_strip_completion"}
            write_json(current_folder / "trial-result.json", trial)
            result["trials"].append(trial)
            record("trial_complete", rank=source["rank"], judgments=len(trial["judgments"]), frame=responded["frame"],
                misses=trial["miss_count"], skipped_inputs=trial["skipped_input_count"])
        result["outcome"] = "pass"
    except (Exception, SystemExit) as error:
        result["outcome"] = "review" if isinstance(error, ReviewNeeded) else "timeout" if isinstance(error, TimeoutError) else "fail"
        result["error"] = str(error) or type(error).__name__
        if owned:
            controller.live_deadline = None
            try:
                controller.set_pause(True)
                if attached:
                    fresh = client.command("capture", "failure")
                    item = fresh["observation"]["image"]
                    png = Path(item["path"]).with_suffix(".png")
                    stacked_gd(item["path"]).save(png)
                    result["failure_capture"] = {**item, "png": str(png)}
                    if result["outcome"] == "review":
                        write_json(current_folder / "review-request.json", {"run_id": run_name, "frame": fresh["frame"],
                            "epoch": fresh["epoch"], "image": str(png), "reason": result["error"],
                            "source": "fresh_paused_native_pixels", "verdict": "pending"})
                    slot10_touched = True
                    result["stopped_evidence"] = client.stop(result["error"], slot=TEMPORARY_SLOT)
            except (Exception, SystemExit) as retained_error:
                result["cleanup_errors"].append(f"Stopped evidence: {retained_error}")
    finally:
        controller.live_deadline, controller.timeouts_enabled = None, True
        if owned:
            for name, action in (("pause_finish", lambda: controller.set_pause(True)),
                    ("lua_finish", lambda: client.command("terminal") if attached else None),
                    ("movie_finish", emulator.stop_movie), ("speed_finish", emulator.restore_normal_speed)):
                try:
                    result[name] = action()
                except (Exception, SystemExit) as error:
                    result["cleanup_errors"].append(f"{name}: {error}")
            try:
                if slot10_touched:
                    native10 = emulator.states.path(TEMPORARY_SLOT)
                    if original_slot10 is None:
                        native10.unlink(missing_ok=True)
                    else:
                        shutil.copy2(original_slot10, native10)
                        if not filecmp.cmp(original_slot10, native10, shallow=False):
                            raise RuntimeError("Temporary slot 10 restoration did not match its original file.")
                result["temporary_slot_10_restored"] = True
                if rendering_before is not None:
                    window = controller.session()["window"]
                    if bool(emulator.menu_state(221) & 0x8) != rendering_before["automatic"]:
                        controller.send(window, 0x111, 221)
                    previous = rendering_before["skip_menu"]
                    if previous is not None and not emulator.menu_state(previous) & 0x8:
                        controller.send(window, 0x111, previous)
                result["emulator_paused"] = controller.paused(controller.session())
                status = emulator.status()
                result["frame"] = status.get("frame")
                result["inputs_released"] = status.get("buttons") == "" and status.get("stylus_touch") is False
                result["endpoint_confirmed"] = result["emulator_paused"] and result["inputs_released"]
                if not result["endpoint_confirmed"]:
                    raise RuntimeError("The terminal paused, released-input endpoint was not confirmed.")
            except (Exception, SystemExit) as error:
                result["cleanup_errors"].append(f"Native restoration: {error}")
        try:
            result["prepared_sources_preserved"] = all(filecmp.cmp(retained,
                emulator.states.path(slot, data={"rom": str(ROM)}), shallow=False) for slot, retained in source_copies.items()) if source_copies else None
            if result["prepared_sources_preserved"] is False:
                result["cleanup_errors"].append("A prepared source file changed during the run.")
        except OSError as error:
            result["cleanup_errors"].append(f"Prepared source validation: {error}")
        result["total_host_seconds"] = time.monotonic() - started
        if result["cleanup_errors"] and result["outcome"] == "pass":
            result["outcome"] = "fail"
        write_json(work / "runner-result.json", result)
    return result


def check():
    from tests.support.lua51 import Lua51, literal

    ast.parse(Path(__file__).read_text(encoding="utf-8"))
    lua = Lua51()
    try:
        print(lua.run(f"assert(loadfile({literal((HERE / 'rank_speed_probe.lua').as_posix())})); return 'Rank-speed probe Python/Lua syntax OK'"))
    finally:
        lua.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--inspect-image", type=Path, help="Read an existing native PNG/GD without emulator access")
    parser.add_argument("--run-name")
    parser.add_argument("--measure-only", action="store_true", help="Finish paused after both pointer-speed measurements")
    parser.add_argument("--super-slot", type=int, default=1)
    parser.add_argument("--hyper-slot", type=int, default=2)
    parser.add_argument("--render-host-seconds", type=float)
    parser.add_argument("--measurement-host-seconds", type=float)
    parser.add_argument("--host-timeout", type=float, default=300)
    parser.add_argument("--hold-frames", type=int)
    parser.add_argument("--offset-frames", type=int)
    args = parser.parse_args(argv)
    if args.check:
        check()
        return 0
    if args.inspect_image:
        image = load_image(args.inspect_image)
        print(json.dumps({"position": pointer_position(image), "left": cue_positions(image, "left"),
            "right": cue_positions(image, "right"), "source": "offline_image_pixels"}, indent=2))
        return 0
    options = {name: getattr(args, name) for name in ("super_slot", "hyper_slot", "render_host_seconds",
        "measurement_host_seconds", "host_timeout", "hold_frames", "offset_frames", "measure_only")}
    if not args.execute:
        print(json.dumps(probe_plan(**options), indent=2))
        return 0
    result = run(run_name=args.run_name, **options)
    print(json.dumps({"outcome": result["outcome"], "work_dir": result["work_dir"],
        "measurements": [{"rank": value["rank"], "speed": value["speed"]} for value in result["measurements"]],
        "trials": [{"rank": value["rank"], "outcome": value["outcome"], "judgments": value["judgments"]} for value in result["trials"]],
        "emulator_paused": result.get("emulator_paused"), "inputs_released": result.get("inputs_released"),
        "error": result.get("error"),
        "cleanup_errors": result["cleanup_errors"]}, indent=2))
    return 0 if result["outcome"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
