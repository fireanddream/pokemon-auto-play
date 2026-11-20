"""Fresh dance pixels, fixed-spacing cue selection, and counted response plans."""

import math
from pathlib import Path

from contest.pt_dance_read import (
    LEFT_SPRITES, RIGHT_SPRITES, _match, layout_for, native_top,
    read_bar, read_pointer, read_progress, stacked_gd,
)


class ReviewNeeded(RuntimeError):
    """The current paused dance evidence needs input-free continuation."""


def pointer_position(image):
    bar, pointer = read_bar(image), read_pointer(image)
    progress = read_progress(bar, pointer)
    bounds = progress["position_bounds"]
    if bounds is None or bounds[0] != bounds[1] or progress["status"] == "conflict":
        raise ReviewNeeded("The fresh pointer/strip position is unreadable or conflicting.")
    return {"x": bounds[0], "phase": progress["phase"],
        "source": progress.get("position_source"), "bar": bar, "pointer": pointer}


def cue_positions(image, side, dance=1):
    """Match button templates at their native centers for the selected dance."""
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


def select_cues(left, right, *, expected_cues, side_offset_pixels):
    """Keep the demo prefix/response suffix, or agreeing pairs from partial views."""
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


def response_plan(midpoint, image, *, rank, expected_cues, speed, hold_frames, offset_frames,
                  side_offset_pixels, dance=1, counted_midpoint_anchor=None, cue_readings=None):
    """Reuse the cue scan of this same fresh image when the caller already has it."""
    fresh = cue_readings if cue_readings is not None else {
        side: cue_positions(image, side, dance) for side in ("left", "right")}
    paired = select_cues(fresh["left"], fresh["right"], expected_cues=expected_cues,
        side_offset_pixels=side_offset_pixels)
    position = midpoint["position"]
    timing_frame, timing_x = midpoint["frame"], position["x"]
    if counted_midpoint_anchor is not None:
        anchor = counted_midpoint_anchor
        if (not isinstance(anchor, dict)
                or any(isinstance(anchor.get(key), bool) or not isinstance(anchor.get(key), int)
                    for key in ("frame", "epoch", "pointer_x"))
                or anchor.get("source") != "fresh_paused_midpoint_before_counted_correction"
                or anchor["frame"] < 0 or anchor["epoch"] != midpoint["epoch"]
                or not 8 <= anchor["pointer_x"] < 128 or anchor["pointer_x"] != position["x"]
                or position["phase"] != "demo"
                or midpoint["frame"] < anchor["frame"] + math.ceil((128 - anchor["pointer_x"]) / speed)):
            raise ReviewNeeded("The held display needs its same-epoch completed counted midpoint anchor.")
        timing_frame, timing_x = anchor["frame"], anchor["pointer_x"]
    if position["phase"] != "response" and counted_midpoint_anchor is None:
        raise ReviewNeeded("The fresh capture has not reached the response midpoint.")
    presses, points, accepted, skipped = [], [], [], []
    px = image.load()
    predicted_end = timing_frame + math.floor((248 - timing_x) / speed + 0.5)
    for input_index, cue in enumerate(paired["accepted"], 1):
        due = timing_frame + math.floor((cue["response_x"] - timing_x) / speed + 0.5) + offset_frames
        reason = "input_already_due" if due <= midpoint["frame"] else "input_outside_response" \
            if due + hold_frames >= predicted_end else "overlapping_input" \
            if presses and presses[-1]["frame"] + hold_frames >= due else None
        if reason:
            skipped.append({"input_index": input_index, "button": cue["button"], "response_x": cue["response_x"],
                "frame": due, "reason": reason})
            continue
        presses.append({"button": cue["button"], "demo_x": cue["demo_x"], "response_x": cue["response_x"],
            "frame": due, "input_index": input_index})
        cue_points = []
        for side, reading in cue["evidence"].items():
            sprites = LEFT_SPRITES if side == "left" else RIGHT_SPRITES
            marker_y = 168 if side == "left" else layout_for(dance)["right_marker_y"] - 8
            for dx, dy, _ in sprites[cue["button"]].points:
                x, y = reading["x"] - 8 + dx, marker_y + dy
                cue_points.append([x, y, *px[x, y]])
        points.extend(cue_points)
        cue["pixel_points"] = cue_points
        accepted.append(cue)
    paired["accepted"] = accepted
    for index, press in enumerate(presses):
        following = presses[index + 1]["frame"] if index + 1 < len(presses) else predicted_end
        if press["frame"] <= midpoint["frame"] or press["frame"] + hold_frames >= following:
            raise ReviewNeeded("The fixed speed/offset does not fit the observed response markers.")
    result = {"rank": rank, "dance": dance, "frame": midpoint["frame"], "epoch": midpoint["epoch"],
        "pointer_x": position["x"], "speed": speed, "hold_frames": hold_frames,
        "offset_frames": offset_frames, "player_center_x": 204 - 80 * ((dance - 1) // 2), "presses": presses,
        "predicted_end_frame": predicted_end, "response_end_frame": predicted_end, "pixel_points": points,
        "cues": paired, "source": "fresh_native_cues_with_per_button_fallback",
        "expected_inputs": expected_cues, "skipped_input_count": expected_cues - len(presses), "skipped_inputs": skipped}
    if counted_midpoint_anchor is not None:
        result.update(counted_midpoint_anchor=dict(counted_midpoint_anchor),
            source="fresh_native_cues_with_counted_midpoint_anchor")
    return result


def capture_result(result, *, allow_unreadable=False):
    image = result["observation"]["image"]
    path = Path(image["path"])
    if (result.get("emulator_paused") is not True or image["capture_callback"] != "paused_lua_menu"
            or result["observation"].get("released") is not True or image.get("source") != "fresh_zero_frame_framebuffer"
            or image["direct_read_frames"] != 0 or image["frame"] != result["frame"] or image["epoch"] != result["epoch"]):
        raise RuntimeError("The fresh capture frame/epoch does not match its paused packet.")
    framebuffer = stacked_gd(path)
    framebuffer.save(path.with_suffix(".png"))
    try:
        position = pointer_position(native_top(framebuffer))
    except ReviewNeeded as error:
        if not allow_unreadable:
            raise
        position = {"x": None, "phase": "unknown", "source": "unreadable_native_pixels", "reason": str(error)}
    value = {"frame": result["frame"], "epoch": result["epoch"], "image": image,
        "png": str(path.with_suffix(".png")), "position": position}
    if isinstance(result["observation"].get("midpoint_timing"), dict):
        value["midpoint_timing"] = result["observation"]["midpoint_timing"]
    return value
