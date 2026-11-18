"""Six native dance responses from a prepared source, with optional speed calibration."""

import argparse
from contextlib import ExitStack
import filecmp
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from contest.dance_master import attach, lua_value, settle_native_overlay
from contest.dance_timings import DEFAULT_OFFSET_FRAMES, calibrated_timing, canonical_rank, fixed_cue_spacing, timing_for
from contest.pt_dance_read import gd_image, load_image
from contest.rank_speed_probe import ReviewNeeded, capture_result, cue_positions, fit_speed, pointer_position, response_plan, unpause_window
from contest.runtime import ROM, TimedController, TimedSlots
from contest.stage4_dress import FRAME_RATE, stacked_gd
from host.checkpoints import CheckpointClient, write_json
from host.controller import ControllerConfig
from host.emulator import DeSmuME
from host.paths import ROOT
from host.savestates import StateSlots
from contest.inputs import whole
from contest.timing_profiles import load_profile, section, setting


BRIDGE = "pt-rank-dance"
_DANCE = section(None, "stage5_dancing")
INPUT_HOLD_FRAMES = _DANCE["response_hold_frames"]
FEEDBACK_RELEASE_FRAMES = _DANCE["feedback_release_frames"]
FIRST_FEEDBACK_DELAY_FRAMES = INPUT_HOLD_FRAMES + FEEDBACK_RELEASE_FRAMES - 1


def settings(*, rank, input_slot, host_timeout, pointer_speed=None, calibrate_speed=False,
             category="Cool", timing_profile=None):
    timing_profile = load_profile() if timing_profile is None else timing_profile
    values = section(timing_profile, "stage5_dancing")
    rank = canonical_rank(rank)
    profile = timing_for(rank, category=category, timing_profile=timing_profile)
    StateSlots.validate_slot(input_slot)
    if input_slot == 10:
        raise ValueError("The prepared source must differ from temporary slot 10.")
    if not math.isfinite(host_timeout) or host_timeout <= 0:
        raise ValueError("Supply a positive host-second deadline.")
    if pointer_speed is None and not calibrate_speed:
        pointer_speed = profile["pixels_per_frame"]
    if pointer_speed is not None and (not math.isfinite(pointer_speed) or pointer_speed <= 0):
        raise ValueError("The pointer speed must be positive.")
    return {"game": timing_profile["game_code"], "timing_profile_source": timing_profile["source"],
        "category": category, "rank": rank, "rank_source": "user_prepared_native_slot",
        "source_slot": input_slot, "temporary_slot": 10, "dances": 6,
        "cues_per_dance": profile["cues_per_dance"], "rank_timing": profile,
        "required_response_inputs": 6 * profile["cues_per_dance"], "pointer_pixels_per_emulator_frame": pointer_speed,
        "tolerant_inputs": True, "scheduled_feedback_recovery": False, "observation_policy": "diagnostic",
        "calibrate_speed": calibrate_speed,
        "render_host_seconds": section(timing_profile, "diagnostics")["rank_state_render_host_seconds"],
        "host_timeout_seconds": host_timeout, "attempts": 1, "speed_percent": 100, "frame_limit": True,
        "hold_frames": values["response_hold_frames"], "offset_frames": values["input_offset_frames"],
        "feedback_release_frames": values["feedback_release_frames"],
        "native_osd_settle_host_seconds": values["native_overlay_settle_host_seconds"],
        "demo_sample_interval_frames": values["demo_sample_interval_frames"],
        "leader_switch_gap_frames": profile["extra_switch_frames"],
        "anchor_policy": "fresh initial pointer capture; fresh midpoint pointer and the rank-specific cue count for each response",
        "unreadable_frame_policy": "retain the original frame number and unreadable marker; derive positions and speed from readable samples using full frame differences",
        "midpoint_policy": "rank-specific fresh cue pairs; matching sides pass and one readable side wins silently; acknowledged checkpoint before inputs",
        "finish": "six counted responses with every opportunity executed or skipped; non-Excellent feedback is recorded as Miss"}


def stage5_plan(*, rank, source_frame, host_timeout, hold_frames=None, offset_frames=None,
                  transition_seconds=None, osd_settle_seconds=None, entry_pointer_x=None,
                 feedback_policy="first_button_only", pointer_calibration=None,
                   observation_policy="diagnostic", first_feedback_delay_frames=None,
                      fixed_timing=None, first_feedback_policy="warn_and_continue", midpoint_diagnostics=False,
                     timing_profile=None, category="Cool"):
    timing_profile = load_profile() if timing_profile is None else timing_profile
    values = section(timing_profile, "stage5_dancing")
    hold_frames = setting(hold_frames, values, "response_hold_frames")
    offset_frames = setting(offset_frames, values, "input_offset_frames")
    transition_seconds = setting(transition_seconds, values, "acting_transition_b_mash_game_seconds")
    osd_settle_seconds = setting(osd_settle_seconds, values, "native_overlay_settle_host_seconds")
    debug_inputs = feedback_policy == "record_all_buttons"
    fixed_schedule = debug_inputs or (feedback_policy == "first_button_only" and fixed_timing is not None
        and observation_policy == "scheduled")
    if not isinstance(midpoint_diagnostics, bool) or midpoint_diagnostics and not debug_inputs:
        raise ValueError("Off-center midpoint diagnostics belong to the fixed-metric input debug runner.")
    if debug_inputs and fixed_timing is None:
        raise ValueError("Recording-only feedback requires the debug runner's fixed contest metrics.")
    profile = dict(fixed_timing) if fixed_timing is not None else (
        calibrated_timing(rank, pointer_calibration, category=category, timing_profile=timing_profile) if pointer_calibration is not None
        else timing_for(rank, category=category, timing_profile=timing_profile))
    if fixed_timing is None and pointer_calibration is None:
        profile["timing_source"] = "shared_main_runner_metrics"
    if fixed_timing is not None:
        native = timing_for(rank, category=profile["category"], timing_profile=timing_profile)
        native["cue_side_offset_pixels"] = fixed_cue_spacing(profile["category"], rank, timing_profile=timing_profile)
        if pointer_calibration is not None or any(profile[key] != native[key]
                for key in ("rank", "cues_per_dance", "cue_side_offset_pixels")):
            raise ValueError("Fixed contest timing must preserve the selected rank's cue contract.")
        speed = profile["pixels_per_frame"]
        if isinstance(speed, bool) or not math.isfinite(speed) or speed <= 0:
            raise ValueError("Fixed contest pointer speed must be positive and finite.")
        for field in ("dance_gap_frames", "leader_gap_frames"):
            whole(profile[field], 0, field)
        if (profile["cycle_frames"] != math.floor(240 / speed + profile["dance_gap_frames"])
                or profile["response_frames"] != math.floor(120 / speed)
                or profile["extra_switch_frames"] != profile["leader_gap_frames"] - profile["dance_gap_frames"]
                or profile["extra_switch_frames"] < 0):
            raise ValueError("Fixed contest cycles must be derived from the normalized measured gaps.")
    whole(source_frame, 0, "Accepted dance-entry frame")
    whole(hold_frames, 1, "Dance held frames")
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise ValueError("The dance offset must be a whole emulator-frame count.")
    for value in (host_timeout, transition_seconds):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError("The host deadline and transition duration must be positive seconds.")
    if isinstance(osd_settle_seconds, bool) or not math.isfinite(osd_settle_seconds) or osd_settle_seconds < 0:
        raise ValueError("Overlay settling must be nonnegative host seconds.")
    if feedback_policy not in ("first_button_only", "all_buttons", "record_all_buttons"):
        raise ValueError("Select first_button_only, all_buttons, or record_all_buttons feedback checking.")
    if first_feedback_policy != "warn_and_continue":
        raise ValueError("Dance feedback must warn and continue after a miss.")
    if observation_policy not in ("scheduled", "diagnostic"):
        raise ValueError("Select scheduled or diagnostic dance observations.")
    if first_feedback_delay_frames is None:
        first_feedback_delay_frames = hold_frames + values["feedback_release_frames"] - 1
    whole(first_feedback_delay_frames, 1, "First-feedback capture delay")
    first_feedback_delay_frames = max(hold_frames, first_feedback_delay_frames)
    if pointer_calibration is not None:
        pointer_calibration = {**pointer_calibration, "pixels_per_frame": profile["pixels_per_frame"],
            "frames_per_pixel": profile["frames_per_pixel"]}
        endpoint = pointer_calibration["end"]
        if endpoint["frame"] != source_frame or entry_pointer_x not in (None, endpoint["pointer_x"]):
            raise ValueError("The dance entry must match the accepted pointer calibration endpoint.")
        entry_pointer_x = endpoint["pointer_x"]
    if debug_inputs and observation_policy != "scheduled":
        raise ValueError("Fixed debug inputs use scheduled midpoint and per-input feedback captures.")
    if observation_policy == "scheduled" and not debug_inputs and (
            pointer_calibration is None and fixed_timing is None or feedback_policy != "first_button_only"):
        raise ValueError("Scheduled dancing requires fixed metrics or per-contest calibration and first-button feedback.")
    midpoint_timing = {"source": "visual_midpoint_fallback", "entry_frame": source_frame,
        "entry_pointer_x": entry_pointer_x, "frames": None}
    if fixed_schedule:
        midpoint_timing.update(capture_target_x=127, accepted_pointer_pixels=[128, 129], rounding="floor")
    if entry_pointer_x is not None:
        whole(entry_pointer_x, 8, "Accepted Dance 1 pointer position")
        if entry_pointer_x >= 128:
            raise ValueError("The accepted Stage 4 pointer must be within Dance 1's demonstration.")
        travel = ((127 if fixed_schedule else 128) - entry_pointer_x) / profile["pixels_per_frame"]
        first = source_frame + (max(1, math.floor(travel)) if fixed_schedule else math.floor(travel + 0.5))
        midpoint_timing.update(source="accepted_stage4_pointer_and_fixed_metrics" if fixed_timing is not None
            else "accepted_stage4_pointer_calibration" if pointer_calibration is not None
            else "accepted_stage4_pointer_and_rank_timing", frames=[
            first + index * profile["cycle_frames"] + (index // 2) * profile["extra_switch_frames"]
            for index in range(6)])
    return {"stage": 5, "timing_profile_source": timing_profile["source"],
        "demo_sample_interval_frames": values["demo_sample_interval_frames"],
        "calibration_window_frames": section(timing_profile, "diagnostics")["pointer_calibration_frames"],
        "rank": profile["rank"], "rank_source": "accepted_contest_target",
        "rank_timing": profile, "source_frame": source_frame, "debug_inputs": debug_inputs,
        "tolerant_inputs": True, "scheduled_feedback_recovery": fixed_schedule,
        "midpoint_diagnostics": midpoint_diagnostics,
        "fixed_metrics": fixed_timing is not None,
        "pointer_calibration": pointer_calibration, "observation_policy": observation_policy,
        "first_feedback_delay_frames": first_feedback_delay_frames,
        "feedback_release_frames": first_feedback_delay_frames - hold_frames + 1,
        "host_timeout_seconds": host_timeout, "refresh_frames": 0, "calibrate_speed": False,
        "pointer_pixels_per_emulator_frame": profile["pixels_per_frame"], "hold_frames": hold_frames,
        "offset_frames": offset_frames, "leader_switch_gap_frames": profile["extra_switch_frames"],
        "midpoint_timing": midpoint_timing,
        "native_osd_settle_host_seconds": osd_settle_seconds, "feedback_policy": feedback_policy,
        "first_feedback_policy": first_feedback_policy,
        "cues_per_dance": profile["cues_per_dance"],
        "required_response_inputs": 6 * profile["cues_per_dance"],
        "required_excellent_judgments": 0,
        "transition": {"game_seconds": transition_seconds, "mash_frames": max(1, round(transition_seconds * FRAME_RATE)),
            "mash_hold_frames": values["transition_mash_hold_frames"], "mash_release_frames": values["transition_mash_release_frames"]}}


def read_demo_timing(samples, read_frame):
    if not samples:
        raise ReviewNeeded("The demonstration has no recorded frame observations.")
    observations, points = [], []
    for index, sample in enumerate(samples):
        if sample["epoch"] != samples[0]["epoch"] or index and sample["frame"] <= samples[index - 1]["frame"]:
            raise ReviewNeeded("Demo observations changed epoch or frame order.")
        item = {"frame": sample["frame"], "epoch": sample["epoch"], "position_readable": False,
            "source": sample["source"], "readability_reason": sample.get("readability_reason")}
        if sample.get("readable") is not False:
            try:
                position = pointer_position(gd_image(read_frame(sample)))
            except ReviewNeeded as error:
                item["readability_reason"] = str(error)
            else:
                item.update(position_readable=True, pointer_x=position["x"], position_source=position["source"])
                if sample["source"] == "frame_callback":
                    points.append((sample["frame"], position["x"]))
        observations.append(item)
    speed = None
    if len(points) >= 2:
        try:
            speed = fit_speed(points)
        except ReviewNeeded as error:
            speed = {"sample_count": len(points), "linear_fit_error": str(error)}
        slopes = [(right[1] - left[1]) / (right[0] - left[0])
            for index, left in enumerate(points) for right in points[index + 1:]]
        speed["median_pairwise_pixels_per_frame"] = statistics.median(slopes)
        speed["calibration_method"] = "median_pairwise_slope_in_original_emulator_frame_coordinates"
    return {"first_frame": samples[0]["frame"], "last_frame": samples[-1]["frame"],
        "elapsed_emulator_frames": samples[-1]["frame"] - samples[0]["frame"],
        "unreadable_frames": [sample["frame"] for sample in samples if sample.get("readable") is False],
        "observations": observations, "observed_speed": speed,
        "timing_policy": "original emulator-frame coordinates include every unreadable interval"}


def feedback_schedule(planned, *, delay_frames, rank_timing, tolerant_inputs=False):
    """Give each press its full released wait, including a cue at the strip's end."""
    endpoint = planned["response_end_frame"]
    gap = rank_timing["leader_gap_frames" if planned["dance"] % 2 == 0 else "dance_gap_frames"]
    next_demo = endpoint + gap
    if tolerant_inputs:
        kept, cues = [], []
        for press, cue in zip(planned["presses"], planned["cues"]["accepted"]):
            reason = "input_outside_response" if press["frame"] + planned["hold_frames"] >= endpoint else \
                "overlapping_feedback_wait" if kept and kept[-1]["frame"] + delay_frames >= press["frame"] else \
                "feedback_wait_crosses_next_demo" if press["frame"] + delay_frames > endpoint \
                    and press["frame"] + delay_frames >= next_demo else None
            if reason:
                planned["skipped_inputs"].append({**press, "reason": reason})
            else:
                kept.append(press)
                cues.append(cue)
        planned["presses"], planned["cues"]["accepted"] = kept, cues
        planned["skipped_input_count"] = planned["expected_inputs"] - len(kept)
        planned["pixel_points"] = [point for cue in cues for point in cue["pixel_points"]]
    frames = [press["frame"] + delay_frames for press in planned["presses"]]
    if not frames:
        return {"feedback_frames": [], "feedback_deadline_frames": [], "next_demo_frame": next_demo}
    deadlines = [min(endpoint, following["frame"] - 1) for following in planned["presses"][1:]] \
        + [max(endpoint, frames[-1])]
    if any(frame >= following["frame"] for frame, following in zip(frames, planned["presses"][1:])):
        raise ReviewNeeded("Each scheduled feedback capture must precede the following response input.")
    if any(frame > deadline for frame, deadline in zip(frames, deadlines)):
        raise ReviewNeeded("Each feedback recovery window must fit before the next input or full released wait.")
    if deadlines[-1] > endpoint and deadlines[-1] >= next_demo:
        raise ReviewNeeded("The final input's full released wait must finish before the next demonstration.")
    return {"feedback_frames": frames, "feedback_deadline_frames": deadlines, "next_demo_frame": next_demo}


def skipped_response(midpoint, dance, options, reason):
    """Wait out an unreadable response from its counted midpoint without inputs."""
    profile = options["rank_timing"]
    anchor = (midpoint.get("midpoint_timing") or {}).get("expected_frame") or midpoint["frame"]
    endpoint = max(midpoint["frame"] + 1,
        anchor + math.ceil(1 / profile["pixels_per_frame"]) + profile["response_frames"])
    required = options["cues_per_dance"]
    return {"rank": options["rank"], "dance": dance, "frame": midpoint["frame"], "epoch": midpoint["epoch"],
        "pointer_x": None, "speed": profile["pixels_per_frame"], "hold_frames": options["hold_frames"],
        "offset_frames": options["offset_frames"], "player_center_x": 204 - 80 * ((dance - 1) // 2),
        "presses": [], "pixel_points": [], "response_end_frame": endpoint, "predicted_end_frame": endpoint,
        "expected_inputs": required, "skipped_input_count": required,
        "skipped_inputs": [{"reason": reason, "count": required}], "skip_reason": reason,
        "fallback_midpoint_frame": anchor, "source": "counted_midpoint_input_free_skip",
        "cues": {"accepted": [], "conflicts": [], "unresolved_cues": required,
            "side_offset_pixels": profile["cue_side_offset_pixels"], "side_offset_source": "configured_cue_spacing"}}


def midpoint_diagnostic(captures, adjustments, speed):
    """Keep line-window and first-pixel errors separate; frame errors are fixed-speed estimates."""
    def measurement(captured):
        x = captured["position"]["x"]
        if x is None:
            return {"frame": captured["frame"], "epoch": captured["epoch"], "pointer_x": None,
                "phase": "unknown", "status": "unreadable", "readable": False,
                "first_pixel_error_pixels": None, "estimated_first_pixel_error_frames": None,
                "line_error_pixels": None, "estimated_line_error_frames": None,
                "on_line": False, "on_first_pixel": False, "image": captured["png"],
                "raw_image": captured["image"]["path"], "direct_read_frames": captured["image"]["direct_read_frames"]}
        line_error = x - 128 if x < 128 else x - 129 if x > 129 else 0
        return {"frame": captured["frame"], "epoch": captured["epoch"], "pointer_x": x, "readable": True,
            "phase": captured["position"]["phase"], "status": "early" if x < 128 else "on_line" if x <= 129 else "late",
            "first_pixel_error_pixels": x - 128, "estimated_first_pixel_error_frames": (x - 128) / speed,
            "line_error_pixels": line_error, "estimated_line_error_frames": line_error / speed,
            "on_line": 128 <= x <= 129, "on_first_pixel": x == 128,
            "image": captured["png"], "raw_image": captured["image"]["path"],
            "direct_read_frames": captured["image"]["direct_read_frames"]}

    pre, post = measurement(captures[0]), measurement(captures[-1])
    accepted = [item for item in adjustments if item.get("accepted") is not False]
    rejected = len(adjustments) - len(accepted)
    attempted = bool(adjustments)
    outcome = "unreadable" if not post["readable"] else ("fixed" if post["on_line"] else "still_early" if post["pointer_x"] < 128 else "overshot") if attempted \
        else "not_needed" if pre["on_line"] else "late_uncorrected"
    if rejected:
        outcome = "rejected"
    return {"schema": 1, "accepted_pointer_pixels": [128, 129], "first_line_pixel": 128,
        "pixels_per_emulator_frame": speed, "frame_error_source": "estimate_from_fixed_pointer_speed",
        "pre": pre, "post": post, "post_capture_reused": len(captures) == 1,
        "correction_attempted": attempted, "correction_count": len(accepted), "rejected_corrections": rejected,
        "correction_frames": sum(item["next_frame"] - item["frame"] for item in accepted),
        "correction_outcome": outcome, "visible_progress_pixels": post["pointer_x"] - pre["pointer_x"]
            if pre["readable"] and post["readable"] else None,
        "fixed_to_line": not pre["on_line"] and post["on_line"],
        "fixed_to_first_pixel": not pre["on_first_pixel"] and post["on_first_pixel"],
        "post_off_line": not post["on_line"], "post_off_first_pixel": not post["on_first_pixel"]}


def checked_dance_result(result):
    if result.get("error") or result.get("recovery_required"):
        raise ReviewNeeded(result.get("error") or result.get("warning") or "The dance needs input-free continuation.")
    return result


def prepare_response(emulator, client, folder, dance, options, *, record):
    controller = emulator.controller
    adjustments, captures = [], []
    debug_inputs = options.get("debug_inputs") is True
    tolerant_inputs = True
    scheduled_feedback = options.get("scheduled_feedback_recovery") is True
    diagnostics = options.get("midpoint_diagnostics") is True
    skip_reason = None
    if diagnostics and not (debug_inputs and options.get("fixed_metrics") and options.get("observation_policy") == "scheduled"):
        raise ValueError("Off-center continuation requires fixed-metric scheduled input debugging.")
    while True:
        observed = emulator.command("observe-demo", timeout=None, deadline=controller.live_deadline)
        if observed.get("error") or observed.get("stop_reason") != "demo_midpoint":
            raise ReviewNeeded(observed.get("error") or "The demonstration did not reach its midpoint.")
        name = f"midpoint-{len(captures) + 1:02d}" if tolerant_inputs else "midpoint"
        captured = checked_dance_result(client.command("capture", name))
        midpoint = capture_result(captured, allow_unreadable=True) if tolerant_inputs else capture_result(captured)
        timing = captured.get("observation", {}).get("midpoint_timing")
        if isinstance(timing, dict):
            midpoint["midpoint_timing"] = timing
        if tolerant_inputs:
            x = midpoint["position"]["x"]
            midpoint["midpoint_status"] = "unreadable" if x is None else "early" if x < 128 else "on_line" if x <= 129 else "late"
            captures.append(dict(midpoint))
            write_json(folder / "midpoint-captures.json", {"accepted_pointer_pixels": [128, 129], "captures": captures})
            shutil.copy2(midpoint["image"]["path"], folder / "midpoint.gd")
            shutil.copy2(midpoint["png"], folder / "midpoint.png")
            record("midpoint_capture", dance=dance, frame=midpoint["frame"], pointer_x=x,
                midpoint_status=midpoint["midpoint_status"], image=midpoint["png"], direct_read_frames=0)
        write_json(folder / "midpoint.json", midpoint)
        if tolerant_inputs and midpoint["position"]["x"] is None:
            skip_reason = midpoint["position"]["reason"]
            break
        if midpoint["position"]["phase"] == "response":
            break
        if not (options.get("pointer_calibration") or options.get("fixed_metrics") or options.get("prepared_midpoint_anchor")) or midpoint["position"]["phase"] != "demo":
            if tolerant_inputs:
                skip_reason = "The fresh midpoint does not establish a usable demo or response position."
                break
            raise ReviewNeeded(f"Dance {dance}: the paused midpoint capture is still in the demonstration "
                f"at pointer x={midpoint['position']['x']}.")
        # Pixel-derived speeds are quantized. A readable early stop supplies a
        # new counted target; no running-frame image polling is needed.
        x = midpoint["position"]["x"]
        if adjustments:
            previous = adjustments[-1]
            if x < previous["pointer_x"]:
                skip_reason = "The counted midpoint adjustment moved backward."
                break
            if x == previous["pointer_x"]:
                if midpoint["epoch"] != previous["epoch"] or midpoint["frame"] < previous["next_frame"]:
                    skip_reason = "The held display lacks same-epoch completed counted midpoint travel."
                    break
                # Display holds do not move absolute press deadlines. Keep the
                # verified pre-correction origin and read cues from this capture.
                anchor = {key: previous[key] for key in ("frame", "epoch", "pointer_x")}
                anchor["source"] = "fresh_paused_midpoint_before_counted_correction"
                midpoint["counted_midpoint_anchor"] = anchor
                captures[-1]["counted_midpoint_anchor"] = anchor
                write_json(folder / "midpoint-captures.json", {"accepted_pointer_pixels": [128, 129], "captures": captures})
                write_json(folder / "midpoint.json", midpoint)
                record("midpoint_display_held", dance=dance, frame=midpoint["frame"], pointer_x=x,
                    advanced_emulator_frames=midpoint["frame"] - anchor["frame"], counted_midpoint_anchor=anchor)
                break
        if diagnostics and adjustments:
            break
        frames = math.ceil((128 - x) / options["pointer_pixels_per_emulator_frame"])
        adjustment = {"frame": midpoint["frame"], "epoch": midpoint["epoch"], "pointer_x": x,
            "next_frame": midpoint["frame"] + frames, "path": midpoint["image"]["path"],
            "retained_image": str(folder / f"midpoint-adjustment-{len(adjustments) + 1:02d}.gd")}
        shutil.copy2(adjustment["path"], adjustment["retained_image"])
        if tolerant_inputs:
            shutil.copy2(midpoint["png"], Path(adjustment["retained_image"]).with_suffix(".png"))
        adjustments.append(adjustment)
        write_json(folder / "midpoint-adjustments.json", {"adjustments": adjustments})
        path = folder / "midpoint-adjustment.lua"
        path.write_text("return " + lua_value(adjustment) + "\n", encoding="utf-8", newline="\n")
        adjusted = client.command("retime-midpoint", path.as_posix())
        if tolerant_inputs and adjusted.get("recovery_required"):
            adjustment.update(accepted=False, rejection_reason=adjusted["warning"])
            write_json(folder / "midpoint-adjustments.json", {"adjustments": adjustments})
            skip_reason = adjusted["warning"]
            record("midpoint_adjustment_rejected", dance=dance, frame=midpoint["frame"],
                pointer_x=x, reason=skip_reason, additional_emulator_frames=0)
            break
        checked_dance_result(adjusted)
        record("midpoint_adjusted", dance=dance, frame=midpoint["frame"], pointer_x=x,
             additional_emulator_frames=frames, next_frame=adjustment["next_frame"])
    if diagnostics:
        diagnostic = midpoint_diagnostic(captures, adjustments, options["pointer_pixels_per_emulator_frame"])
        midpoint["diagnostic"] = diagnostic
        write_json(folder / "midpoint-diagnostic.json", {"dance": dance, **diagnostic})
        write_json(folder / "midpoint.json", midpoint)
        record("midpoint_diagnostic", dance=dance, **diagnostic)
    image = load_image(Path(midpoint["image"]["path"]))
    cues = {side: cue_positions(image, side, dance) for side in ("left", "right")}
    write_json(folder / "midpoint-cues.json", {"frame": midpoint["frame"], "epoch": midpoint["epoch"],
        "expected_cues": options["cues_per_dance"], "cues": cues, "source": "fresh_paused_native_pixels"})
    record("midpoint_cues", dance=dance, frame=midpoint["frame"], pointer_x=midpoint["position"]["x"],
        left=len(cues["left"]), right=len(cues["right"]))
    with ExitStack() as stack:
        samples, read_frame = [], None
        if options.get("observation_policy") == "scheduled":
            reading = {"source": "scheduled_paused_midpoint", "sampled_frames": [midpoint["frame"]],
                "unreadable_frames": [midpoint["frame"]] if midpoint["position"]["x"] is None else [],
                "observed_speed": None, "continuous_demo_observation": False}
        else:
            samples = [json.loads(line) for line in (folder / "demo-frames.jsonl").read_text(encoding="utf-8").splitlines()]
            stream = stack.enter_context((folder / "demo-frames.bin").open("rb"))

            def read_frame(sample):
                controller.remaining()
                stream.seek(sample["capture_offset"])
                return stream.read(sample["capture_bytes"])

            reading = read_demo_timing(samples, read_frame)
            if dance == 1 and options["calibrate_speed"]:
                fitted = reading["observed_speed"]
                speed = fitted["median_pairwise_pixels_per_frame"] if fitted else None
                if speed is None or not math.isfinite(speed) or speed <= 0:
                    raise ReviewNeeded("The readable first-demo positions do not establish a positive pointer speed.")
                options["pointer_pixels_per_emulator_frame"] = speed
                record("speed_calibrated", dance=dance, pixels_per_frame=speed, readable_samples=fitted["sample_count"])
        reading["scheduled_speed"] = options["pointer_pixels_per_emulator_frame"]
        write_json(folder / "demo-reading.json", reading)
        try:
            if skip_reason:
                raise ReviewNeeded(skip_reason)
            planned = response_plan(midpoint, image, samples, read_frame, rank=options["rank"], expected_cues=options["cues_per_dance"],
                speed=options["pointer_pixels_per_emulator_frame"], hold_frames=options["hold_frames"],
                offset_frames=options["offset_frames"], dance=dance,
                side_offset_pixels=options["rank_timing"]["cue_side_offset_pixels"] if options.get("fixed_metrics")
                    else options.get("resolved_cue_side_offset_pixels"),
                allow_demo_midpoint=diagnostics, fixed_spacing=options.get("fixed_metrics") is True, tolerant_inputs=True,
                counted_midpoint_anchor=midpoint.get("counted_midpoint_anchor"))
        except ReviewNeeded as error:
            if not tolerant_inputs:
                raise
            planned = skipped_response(midpoint, dance, options, str(error))
    options["resolved_cue_side_offset_pixels"] = planned["cues"]["side_offset_pixels"]
    if options.get("rank_timing") and not planned.get("skip_reason"):
        origin = planned.get("counted_midpoint_anchor") or {"frame": midpoint["frame"], "pointer_x": midpoint["position"]["x"]}
        planned["response_end_frame"] = math.floor(origin["frame"] + (128 - origin["pointer_x"]) / planned["speed"]
            + options["rank_timing"]["response_frames"] + 0.5)
        if tolerant_inputs:
            planned["response_end_frame"] = max(midpoint["frame"] + 1, planned["response_end_frame"])
    if scheduled_feedback:
        planned.update(feedback_schedule(planned, delay_frames=options["first_feedback_delay_frames"],
            rank_timing=options["rank_timing"], tolerant_inputs=True))
        if not debug_inputs and dance == 1:
            planned["first_feedback_frame"] = next((frame for press, frame in zip(planned["presses"], planned["feedback_frames"])
                if press["input_index"] == 1), None)
    elif options.get("observation_policy") == "scheduled" and dance == 1:
        planned["first_feedback_frame"] = planned["presses"][0]["frame"] + options["first_feedback_delay_frames"] if planned["presses"] else None
        if len(planned["presses"]) > 1 and planned["first_feedback_frame"] >= planned["presses"][1]["frame"]:
            raise ReviewNeeded("The scheduled first-feedback capture must precede the second response input.")
    record("response_selected", dance=dance, expected_inputs=options["cues_per_dance"],
        planned_inputs=len(planned["presses"]), skipped_inputs=planned["skipped_input_count"],
        conflicts=len(planned["cues"]["conflicts"]), reason=planned.get("skip_reason"))
    write_json(folder / "response-plan.json", planned)
    path = folder / "response-plan.lua"
    path.write_text("return " + lua_value(planned) + "\n", encoding="utf-8", newline="\n")
    checked_dance_result(client.command("plan", path.as_posix()))
    return midpoint, planned, reading


def stage5_responses(runtime, plan):
    """Use the master's existing client, Lua callbacks, deadlines, and slot ownership."""
    responses, last = [], None
    previous_saved = runtime.boundary["save_state"] if runtime.boundary else None
    initialized = runtime.command("init")
    if initialized.get("recovery_required"):
        runtime.record("dance_warning", reason=initialized["warning"])
    for dance in range(1, 7):
        folder = runtime.active_work / f"dance-{dance:02d}"
        folder.mkdir()

        def acknowledge(name, frame, epoch):
            path = folder / f"{name}-context.lua"
            path.write_text("return " + lua_value({"frame": frame, "epoch": epoch}) + "\n", encoding="utf-8", newline="\n")
            return checked_dance_result(runtime.acknowledge(
                checked_dance_result(runtime.command("visit", name, path.as_posix())),
                slot=runtime.settings["save_policy"]["temporary_slot"] if plan["observation_policy"] == "diagnostic" else None))

        try:
            gap = plan["leader_switch_gap_frames"] if dance in (3, 5) else 0
            checked_dance_result(runtime.command("select-dance", dance, folder.as_posix(), gap))
            if gap:
                checked_dance_result(runtime.action("wait-gap", expected="leader_switch_gap_complete"))
            settle_saved_overlay(runtime, folder, dance, previous_saved, "demo", plan["native_osd_settle_host_seconds"])
            midpoint, planned, reading = prepare_response(runtime.emulator, runtime, folder, dance, plan, record=runtime.record)
            started = acknowledge("rank-dance-start", midpoint["frame"], midpoint["epoch"])
            settle_saved_overlay(runtime, folder, dance, started["host_savestate"], "response", plan["native_osd_settle_host_seconds"])
            responded = checked_dance_result(runtime.action("mirror"))
            if dance == 1 and not plan.get("debug_inputs"):
                if responded.get("stop_reason") != "offset_feedback_ready":
                    raise ReviewNeeded("The first-button feedback checkpoint was not reached.")
                acknowledge("rank-dance-offset", responded["frame"], midpoint["epoch"])
                responded = checked_dance_result(runtime.action("mirror-rest", expected="dance_response_complete"))
            elif responded.get("stop_reason") != "dance_response_complete":
                raise ReviewNeeded("The rank-selected response did not reach its counted endpoint.")
        except Exception as error:
            if isinstance(error, TimeoutError):
                raise
            reason = (str(error) or type(error).__name__).replace("|", "/").replace("\r", " ").replace("\n", " ")
            captured = checked_dance_result(runtime.command("capture", "response-recovery"))
            observation = captured["observation"]
            midpoint = {"frame": captured["frame"], "epoch": captured["epoch"], "image": observation["image"],
                "position": {"x": None, "phase": "unknown", "reason": "analysis_skipped_after_dance_error"},
                "midpoint_timing": observation["midpoint_timing"], "recovery_reason": str(error)}
            skipped = skipped_response(midpoint, dance, plan, reason)
            skipped.update(feedback_schedule(skipped, delay_frames=plan["first_feedback_delay_frames"],
                rank_timing=plan["rank_timing"]))
            path = folder / "response-skip.lua"
            path.write_text("return " + lua_value(skipped) + "\n", encoding="utf-8", newline="\n")
            recovered = checked_dance_result(runtime.command("recover-response", dance, path.as_posix(), reason))
            observation = recovered["observation"]
            planned = recovered["response_plan"]
            reading = {"source": "counted_response_recovery", "unreadable_frames": [], "analysis_error": str(error)}
            write_json(folder / "response-recovery.json", {"midpoint": midpoint, "plan": planned})
            runtime.record("dance_warning", dance=dance, frame=recovered["frame"], reason=str(error),
                continuation="counted_input_free_response")
            if not observation["trial"]["response_started"]:
                acknowledge("rank-dance-start", recovered["frame"], recovered["epoch"])
            if dance == 1 and not plan.get("debug_inputs") and not observation["trial"]["first_feedback_verified"]:
                acknowledge("rank-dance-offset", recovered["frame"], recovered["epoch"])
            responded = checked_dance_result(runtime.action("mirror", expected="dance_response_complete"))
        last = acknowledge("rank-dance-finish", responded["frame"], midpoint["epoch"])
        previous_saved = last["host_savestate"]
        trial = last["checkpoint"]["snapshot"]["trial"]
        value = {"dance": dance, "rank": plan["rank"], "midpoint_frame": midpoint["frame"],
            "midpoint_pointer_x": midpoint["position"]["x"],
            "midpoint_timing": midpoint.get("midpoint_timing"),
             "end_frame": responded["frame"], "presses": planned["presses"], "judgments": trial["judgments"],
             "excellent_count": trial["excellent_count"], "miss_count": trial["miss_count"],
            "unreadable_demo_frames": len(reading["unreadable_frames"]),
            "observation_policy": plan["observation_policy"],
            "checkpoint_receipt": last["host_receipt"],
            "retained_state": last["host_savestate"]["retained_path"] if last["host_savestate"] else None}
        if plan.get("tolerant_inputs"):
            value.update(presses=trial["presses"], expected_inputs=plan["cues_per_dance"],
                input_count=trial["inputs"], skipped_input_count=trial["skipped_input_count"],
                skipped_inputs=trial["skipped_inputs"], conflict_count=trial["conflict_count"],
                selection=planned["cues"])
        if plan.get("midpoint_diagnostics"):
            value["midpoint_diagnostic"] = {"dance": dance,
                **midpoint.get("diagnostic", {"recovery_reason": midpoint.get("recovery_reason")})}
        if planned.get("counted_midpoint_anchor"):
            value["counted_midpoint_anchor"] = planned["counted_midpoint_anchor"]
        responses.append(value)
        write_json(folder / "dance-result.json", value)
        if plan.get("tolerant_inputs") or plan["feedback_policy"] == "all_buttons":
            for path in folder.glob("judgment-*.gd"):
                stacked_gd(path).save(path.with_suffix(".png"))
        runtime.json("dance-progress.json", {"completed_dances": dance, "rank_timing": plan["rank_timing"], "trial": trial})
        runtime.record("dance_complete" if plan.get("tolerant_inputs") else "dance_pass", dance=dance,
            feedback_policy=plan["feedback_policy"], fresh_excellent_judgments=trial["excellent_count"],
            total_excellent=trial["total_excellent_count"], total_misses=trial["total_miss_count"],
            skipped_inputs=trial.get("total_skipped_inputs", 0))
    return responses, last


def settle_saved_overlay(runtime, folder, dance, saved, section_name, seconds):
    """Retain a fresh same-frame capture after a native save's paused host wait."""
    if saved is None or not seconds:
        return None
    try:
        result = settle_native_overlay(runtime.controller,
            lambda name: checked_dance_result(runtime.command("capture", name)), seconds, name=section_name)
    except RuntimeError as error:
        raise ReviewNeeded(str(error)) from error
    result.update(dance=dance, native_save=saved)
    write_json(folder / f"overlay-{section_name}.json", result)
    runtime.record("dance_overlay_settled", **result)
    return result


def stage5_handoff(runtime, plan):
    for action, expected, name in (("lead-dances", "lead_dances_complete", "lead-dances-result.json"),
            ("acting-transition", "B_transition_complete", "transition-result.json")):
        try:
            checked_dance_result(runtime.action(action, expected=expected, name=name))
        except Exception as error:
            if plan.get("first_feedback_policy") != "warn_and_continue" or isinstance(error, TimeoutError):
                raise
            reason = (str(error) or type(error).__name__).replace("|", "/").replace("\r", " ").replace("\n", " ")
            checked_dance_result(runtime.command("warn", reason))
            runtime.record("dance_warning", action=action, reason=reason, continuation="counted_handoff")
            checked_dance_result(runtime.action(action, expected=expected, name=name))


def run(run_name, **arguments):
    options = settings(**arguments)
    source_slot = options["source_slot"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a fresh plain run name.")
    parent = ROOT / "tmp/contest"
    if not parent.parent.is_dir():
        raise ValueError("The project-local scratch directory must exist.")
    parent.mkdir(exist_ok=True)
    work = parent / run_name
    work.mkdir(exist_ok=False); (work / "checkpoints").mkdir()
    started = time.monotonic()
    controller = TimedController(ControllerConfig(rom=ROM))
    controller.timeouts_enabled = False
    controller.live_deadline = started + options["host_timeout_seconds"]
    emulator = DeSmuME(controller); emulator.states = TimedSlots(controller)
    client = CheckpointClient(emulator, work, run_name)
    result = {"outcome": "fail", "run_id": run_name, "host_pid": os.getpid(), "work_dir": str(work),
        "settings": options, "dances": [], "cleanup_errors": [], "completed_dances": 0,
        "total_inputs": 0, "total_judgments": 0, "total_excellent": 0, "total_misses": 0, "total_skipped_inputs": 0}
    write_json(work / "runner-setup.json", {"run_id": run_name, "host_pid": os.getpid(),
        "work_dir": str(work), "settings": options})
    owned = attached = slot10_touched = False
    rendering_before = original10 = source_copy = None
    current_folder, current_dance = work, 0

    def record(phase, **facts):
        event = {"phase": phase, "dance": current_dance,
            "host_elapsed_seconds": round(time.monotonic() - started, 3), **facts}
        write_json(work / "runner-progress.json", event)
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    def lua_file(name, value):
        path = current_folder / name
        path.write_text("return " + lua_value(value) + "\n", encoding="utf-8", newline="\n")
        return path

    def settle(name):
        evidence = settle_native_overlay(controller, lambda name: checked_dance_result(client.command("capture", name)),
            options["native_osd_settle_host_seconds"], name=name)
        if evidence is not None:
            write_json(current_folder / f"overlay-{name}.json", evidence)

    def acknowledge(name, frame, epoch):
        nonlocal slot10_touched
        context = lua_file(f"{name}-context.lua", {"frame": frame, "epoch": epoch})
        pending = client.command("visit", name, context.as_posix())
        slot10_touched = True
        return client.acknowledge(pending, slot=10)

    def summary(observation):
        trial = observation.get("trial", {})
        for field in ("completed_dances", "total_inputs", "total_judgments", "total_excellent"):
            if field in trial:
                result[field] = trial[field]
        if "total_miss_count" in trial:
            result["total_misses"] = trial["total_miss_count"]
        if "total_skipped_inputs" in trial:
            result["total_skipped_inputs"] = trial["total_skipped_inputs"]

    try:
        native = emulator.states.path(source_slot, data={"rom": str(ROM)})
        StateSlots.validate_header(native, source_slot)
        source_copy = work / f"prepared-slot-{source_slot}.ds{source_slot % 10}"; shutil.copy2(native, source_copy)
        owners = set(controller.emulator_process_ids()) | set(controller.emulator_window_process_ids())
        if owners:
            session = controller.session()
            if owners != {session["pid"]} or Path(session["rom"]).resolve() != ROM.resolve():
                raise RuntimeError("The attempt requires the sole controlled Platinum emulator.")
            dialogs = [item for item in controller.windows(session["pid"])
                if controller.USER32.GetDlgItem(item["handle"], 310)]
            if len(dialogs) > 1:
                raise RuntimeError("The attempt requires one Lua script window.")
        else:
            controller.launch(ROM)
        owned = True
        controller.set_pause(True); emulator.stop_movie()
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
        native10 = emulator.states.path(10)
        if native10.exists():
            original10 = work / "previous-slot-10.ds0"; shutil.copy2(native10, original10)
        attach(controller, HERE / "rank_dance.lua", bridge=BRIDGE)
        attached = True
        client.command("init-run", run_name, work.as_posix(), options["rank"],
            lua_file("timing-settings.lua", options).as_posix())
        loaded = emulator.load_state(source_slot, refresh=False)
        source = client.command("capture", "loaded-source")
        write_json(work / "source-load.json", {**loaded, "frame": source["frame"], "epoch": source["epoch"],
            "rank": options["rank"], "rank_source": "user_prepared_native_slot", "retained_state": str(source_copy)})
        controller.USER32.SetForegroundWindow(window)
        rendered = unpause_window(controller, options["render_host_seconds"])
        settle("source")
        initial = capture_result(client.command("capture", "initial-position"), allow_unreadable=True)
        if initial["epoch"] != source["epoch"] or initial["frame"] <= source["frame"]:
            raise ReviewNeeded("The prepared render changed epoch or did not advance emulator frames.")
        if initial["position"]["phase"] != "demo":
            reason = "The prepared entry is unreadable or already in its response; using the counted dance budget."
            checked_dance_result(client.command("warn", reason))
            record("dance_warning", reason=reason)
        result["initial_position"] = initial
        write_json(work / "render-preparation.json", {**rendered, "source_frame": source["frame"],
            "emulator_frames": initial["frame"] - source["frame"], "capture": initial})
        anchor_speed = options["pointer_pixels_per_emulator_frame"] or options["rank_timing"]["pixels_per_frame"]
        options["pointer_pixels_per_emulator_frame"] = anchor_speed
        x = initial["position"]["x"]
        predicted_midpoint = initial["frame"] + (max(1, math.ceil((128 - x) / anchor_speed))
            if x is not None else options["rank_timing"]["response_frames"])
        anchor = {"frame": initial["frame"], "epoch": initial["epoch"], "pointer_x": initial["position"]["x"],
            "speed": anchor_speed, "first_midpoint_frame": predicted_midpoint}
        checked_dance_result(client.command("anchor-midpoints", lua_file("midpoint-anchor.lua", anchor).as_posix()))
        options["prepared_midpoint_anchor"] = True
        record("initial_position", frame=initial["frame"], pointer_x=initial["position"]["x"],
            predicted_first_midpoint_frame=predicted_midpoint)

        for current_dance in range(1, 7):
            controller.remaining()
            current_folder = work / f"dance-{current_dance:02d}"
            current_folder.mkdir()
            gap = options["leader_switch_gap_frames"] if current_dance in (3, 5) else 0
            try:
                selected = checked_dance_result(client.command("select-dance", current_dance, current_folder.as_posix(), gap))
                if gap:
                    waited = emulator.command("wait-gap", timeout=None, deadline=controller.live_deadline)
                    if waited.get("error") or waited.get("stop_reason") != "leader_switch_gap_complete":
                        raise ReviewNeeded(waited.get("error") or "The leader-switch gap needs counted continuation.")
                    write_json(current_folder / "leader-switch.json", {"start_frame": selected["frame"],
                        "end_frame": waited["frame"], "emulator_frames": waited["frame"] - selected["frame"], "inputs": "none"})
                record("observe_demo")
                settle("demo")
                midpoint, planned, reading = prepare_response(emulator, client, current_folder, current_dance, options, record=record)
                acknowledge("start", midpoint["frame"], midpoint["epoch"])
                settle("response")
                record("respond", presses=planned["presses"])
                responded = emulator.command("mirror", timeout=None, deadline=controller.live_deadline)
                if responded.get("error") or responded.get("stop_reason") != "dance_response_complete":
                    raise ReviewNeeded(responded.get("error") or "The response needs counted input-free continuation.")
            except Exception as error:
                if isinstance(error, TimeoutError):
                    raise
                reason = (str(error) or type(error).__name__).replace("|", "/").replace("\r", " ").replace("\n", " ")
                captured = checked_dance_result(client.command("capture", "response-recovery"))
                midpoint = {"frame": captured["frame"], "epoch": captured["epoch"], "image": captured["observation"]["image"],
                    "position": {"x": None, "phase": "unknown"}, "midpoint_timing": captured["observation"].get("midpoint_timing")}
                skipped = skipped_response(midpoint, current_dance, options, reason)
                recovered = checked_dance_result(client.command("recover-response", current_dance,
                    lua_file("response-skip.lua", skipped).as_posix(), reason))
                planned = recovered["response_plan"]
                reading = {"unreadable_frames": [], "observed_speed": None, "analysis_error": reason}
                if not recovered["observation"]["trial"]["response_started"]:
                    acknowledge("start", recovered["frame"], recovered["epoch"])
                record("dance_warning", reason=reason, continuation="counted_input_free_response")
                settle("recovery")
                responded = emulator.command("mirror", timeout=None, deadline=controller.live_deadline)
            summary(responded.get("observation", {}))
            completed = acknowledge("finish", responded["frame"], midpoint["epoch"])
            trial = completed["checkpoint"]["snapshot"]["trial"]
            value = {"dance": current_dance, "outcome": "pass", "midpoint_frame": midpoint["frame"],
                "end_frame": responded["frame"], "presses": planned["presses"], "judgments": trial["judgments"],
                "excellent_count": trial["excellent_count"], "miss_count": trial["miss_count"],
                "input_count": trial["inputs"], "skipped_input_count": trial["skipped_input_count"],
                "unreadable_demo_frames": len(reading["unreadable_frames"]), "observed_demo_speed": reading["observed_speed"],
                "retained_state": completed["host_savestate"]["retained_path"]}
            result["dances"].append(value)
            result["completed_dances"] = current_dance
            write_json(current_folder / "dance-result.json", value)
            for path in current_folder.glob("judgment-*.gd"):
                stacked_gd(path).save(path.with_suffix(".png"))
            record("dance_pass", judgments=len(trial["judgments"]), completed_dances=current_dance)
        if result["total_inputs"] + result["total_skipped_inputs"] != options["required_response_inputs"]:
            record("dance_warning", reason="The six-response input accounting needs review.")
        result["outcome"] = "pass"
        result["finish_state"] = result["dances"][-1]["retained_state"]
    except (Exception, SystemExit) as error:
        result["outcome"] = "review" if isinstance(error, ReviewNeeded) else "timeout" if isinstance(error, TimeoutError) else "fail"
        result["error"], result["stopped_dance"] = str(error) or type(error).__name__, current_dance
        if owned:
            controller.live_deadline = None
            try:
                controller.set_pause(True)
                if attached:
                    fresh = client.command("capture", "failure")
                    image = fresh["observation"]["image"]
                    path = Path(image["path"]); stacked_gd(path).save(path.with_suffix(".png"))
                    result["failure_capture"] = {**image, "png": str(path.with_suffix(".png"))}
                    summary(fresh["observation"])
                    write_json(current_folder / "review-request.json", {"run_id": run_name, "frame": fresh["frame"],
                        "epoch": fresh["epoch"], "image": str(path.with_suffix(".png")), "reason": result["error"],
                        "source": "fresh_paused_native_pixels", "verdict": "pending"})
                    slot10_touched = True
                    result["stopped_evidence"] = client.stop(result["error"], slot=10)
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
                    native10 = emulator.states.path(10)
                    if original10 is None:
                        native10.unlink(missing_ok=True)
                    else:
                        shutil.copy2(original10, native10)
                        if not filecmp.cmp(original10, native10, shallow=False):
                            raise RuntimeError("Temporary slot 10 differs from its original file.")
                result["temporary_slot_10_restored"] = True
                if rendering_before is not None:
                    window = controller.session()["window"]
                    if bool(emulator.menu_state(221) & 0x8) != rendering_before["automatic"]:
                        controller.send(window, 0x111, 221)
                    previous = rendering_before["skip_menu"]
                    if previous is not None and not emulator.menu_state(previous) & 0x8:
                        controller.send(window, 0x111, previous)
                status = emulator.status()
                result["emulator_paused"] = controller.paused(controller.session())
                result["inputs_released"] = status.get("buttons") == "" and status.get("stylus_touch") is False
                result["frame"] = status.get("frame")
                if not result["emulator_paused"] or not result["inputs_released"]:
                    raise RuntimeError("The paused, released-input endpoint was not confirmed.")
            except (Exception, SystemExit) as error:
                result["cleanup_errors"].append(f"Native restoration: {error}")
        if source_copy is not None:
            result["source_slot_preserved"] = filecmp.cmp(source_copy,
                emulator.states.path(source_slot, data={"rom": str(ROM)}), shallow=False)
            if not result["source_slot_preserved"]:
                result["cleanup_errors"].append("The prepared source file changed.")
        for path in current_folder.glob("judgment-*.gd"):
            stacked_gd(path).save(path.with_suffix(".png"))
        result["total_host_seconds"] = time.monotonic() - started
        if result["cleanup_errors"] and result["outcome"] == "pass":
            result["outcome"] = "fail"
        write_json(work / "runner-result.json", result)
    return result


def check(*, lua_dll=None):
    from contest.checks import check_module

    check_module(__file__, ("rank_dance.lua",), "Rank-dance Python/Lua syntax OK", lua_dll=lua_dll)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--check", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--rank", default="Master")
    parser.add_argument("--category", choices=("Cool", "Beauty", "Cute", "Smart", "Tough"), default="Cool",
                        help="Category of the prepared source; uses the shared main-runner metrics.")
    parser.add_argument("--input-slot", type=int, default=1)
    parser.add_argument("--host-timeout", type=float, default=300)
    parser.add_argument("--pointer-speed", type=float)
    parser.add_argument("--calibrate-speed", action="store_true")
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
    options = {name: getattr(args, name) for name in ("rank", "category", "input_slot", "host_timeout", "pointer_speed", "calibrate_speed")}
    if not args.execute:
        print(json.dumps(settings(**options), indent=2)); return 0
    result = run(args.run_name, **options)
    keys = ("outcome", "work_dir", "completed_dances", "total_inputs", "total_judgments", "total_excellent", "total_misses", "total_skipped_inputs",
        "stopped_dance", "error", "emulator_paused", "inputs_released", "source_slot_preserved",
        "temporary_slot_10_restored", "cleanup_errors", "total_host_seconds")
    print(json.dumps({key: result.get(key) for key in keys}, indent=2))
    return 0 if result["outcome"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
