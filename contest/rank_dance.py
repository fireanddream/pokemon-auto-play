"""Six main-run dance responses using accepted entry pixels and fixed regional metrics."""

import math

from contest.dance_response import ReviewNeeded, capture_result, cue_positions, response_plan
from contest.dance_timings import fixed_timing as resolve_fixed_timing
from contest.inputs import whole
from contest.pt_dance_read import load_image, stacked_gd
from contest.runtime import lua_value, settle_native_overlay
from contest.timing_profiles import FRAME_RATE, load_profile, section, setting
from host.checkpoints import write_json


def stage5_plan(*, rank, source_frame, entry_pointer_x, host_timeout, hold_frames=None, offset_frames=None,
                transition_seconds=None, osd_settle_seconds=None, fixed_timing=None,
                timing_profile=None, category="Cool"):
    timing_profile = load_profile() if timing_profile is None else timing_profile
    values = section(timing_profile, "stage5_dancing")
    hold_frames = setting(hold_frames, values, "response_hold_frames")
    offset_frames = setting(offset_frames, values, "input_offset_frames")
    transition_seconds = setting(transition_seconds, values, "acting_transition_b_mash_game_seconds")
    osd_settle_seconds = setting(osd_settle_seconds, values, "native_overlay_settle_host_seconds")
    if fixed_timing is not None:
        category = fixed_timing["category"]
    profile = resolve_fixed_timing(category, rank, timing_profile=timing_profile)
    if fixed_timing is not None and fixed_timing != profile:
        raise ValueError("The accepted dance timing must match the selected regional fixed metrics.")
    whole(source_frame, 0, "Accepted dance-entry frame")
    whole(entry_pointer_x, 8, "Accepted Dance 1 pointer position")
    if entry_pointer_x >= 128:
        raise ValueError("The accepted Stage 4 pointer must be within Dance 1's demonstration.")
    whole(hold_frames, 1, "Dance held frames")
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise ValueError("The dance offset must be a whole emulator-frame count.")
    for value in (host_timeout, transition_seconds):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError("The host deadline and transition duration must be positive seconds.")
    if isinstance(osd_settle_seconds, bool) or not math.isfinite(osd_settle_seconds) or osd_settle_seconds < 0:
        raise ValueError("Overlay settling must be nonnegative host seconds.")
    first_feedback_delay_frames = hold_frames + values["feedback_release_frames"] - 1
    whole(first_feedback_delay_frames, 1, "First-feedback capture delay")
    first_feedback_delay_frames = max(hold_frames, first_feedback_delay_frames)
    first = source_frame + max(1, math.floor((127 - entry_pointer_x) / profile["pixels_per_frame"]))
    midpoint_timing = {"source": "accepted_stage4_pointer_and_fixed_metrics", "entry_frame": source_frame,
        "entry_pointer_x": entry_pointer_x, "capture_target_x": 127, "accepted_pointer_pixels": [128, 129],
        "rounding": "floor", "frames": [
            first + index * profile["cycle_frames"] + (index // 2) * profile["extra_switch_frames"]
            for index in range(6)]}
    return {"stage": 5, "timing_profile_source": timing_profile["source"],
        "rank": profile["rank"], "rank_source": "accepted_contest_target",
        "rank_timing": profile, "source_frame": source_frame,
        "tolerant_inputs": True, "fixed_metrics": True, "observation_policy": "scheduled",
        "first_feedback_delay_frames": first_feedback_delay_frames,
        "feedback_release_frames": first_feedback_delay_frames - hold_frames + 1,
        "host_timeout_seconds": host_timeout, "refresh_frames": 0,
        "pointer_pixels_per_emulator_frame": profile["pixels_per_frame"], "hold_frames": hold_frames,
        "offset_frames": offset_frames, "leader_switch_gap_frames": profile["extra_switch_frames"],
        "midpoint_timing": midpoint_timing, "native_osd_settle_host_seconds": osd_settle_seconds,
        "feedback_policy": "first_button_only", "first_feedback_policy": "warn_and_continue",
        "cues_per_dance": profile["cues_per_dance"],
        "required_response_inputs": 6 * profile["cues_per_dance"], "required_excellent_judgments": 0,
        "transition": {"game_seconds": transition_seconds, "mash_frames": max(1, round(transition_seconds * FRAME_RATE)),
            "mash_hold_frames": values["transition_mash_hold_frames"], "mash_release_frames": values["transition_mash_release_frames"]}}


def feedback_schedule(planned, *, delay_frames, rank_timing):
    """Keep the existing released-input windows without moving response deadlines."""
    endpoint = planned["response_end_frame"]
    gap = rank_timing["leader_gap_frames" if planned["dance"] % 2 == 0 else "dance_gap_frames"]
    next_demo = endpoint + gap
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
    frames = [press["frame"] + delay_frames for press in kept]
    if not frames:
        return {"feedback_frames": [], "feedback_deadline_frames": [], "next_demo_frame": next_demo}
    deadlines = [min(endpoint, following["frame"] - 1) for following in kept[1:]] \
        + [max(endpoint, frames[-1])]
    if any(frame >= following["frame"] for frame, following in zip(frames, kept[1:])):
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


def checked_dance_result(result):
    if result.get("error") or result.get("recovery_required"):
        raise ReviewNeeded(result.get("error") or result.get("warning") or "The dance needs input-free continuation.")
    return result


def prepare_response(emulator, client, folder, dance, options, *, record):
    controller = emulator.controller
    adjustments, captures = [], []
    skip_reason = None
    while True:
        observed = emulator.command("observe-demo", timeout=None, deadline=controller.live_deadline)
        if observed.get("error") or observed.get("stop_reason") != "demo_midpoint":
            raise ReviewNeeded(observed.get("error") or "The demonstration did not reach its midpoint.")
        name = f"midpoint-{len(captures) + 1:02d}"
        captured = checked_dance_result(client.command("capture", name))
        midpoint = capture_result(captured, allow_unreadable=True)
        timing = captured.get("observation", {}).get("midpoint_timing")
        if isinstance(timing, dict):
            midpoint["midpoint_timing"] = timing
        x = midpoint["position"]["x"]
        midpoint["midpoint_status"] = "unreadable" if x is None else "early" if x < 128 else "on_line" if x <= 129 else "late"
        captures.append(dict(midpoint))
        write_json(folder / "midpoint-captures.json", {"accepted_pointer_pixels": [128, 129], "captures": captures})
        record("midpoint_capture", dance=dance, frame=midpoint["frame"], pointer_x=x,
            midpoint_status=midpoint["midpoint_status"], image=midpoint["png"], direct_read_frames=0)
        write_json(folder / "midpoint.json", midpoint)
        if x is None:
            skip_reason = midpoint["position"]["reason"]
            break
        if midpoint["position"]["phase"] == "response":
            break
        if midpoint["position"]["phase"] != "demo":
            skip_reason = "The fresh midpoint does not establish a usable demo or response position."
            break
        if adjustments:
            previous = adjustments[-1]
            if x < previous["pointer_x"]:
                skip_reason = "The counted midpoint adjustment moved backward."
                break
            if x == previous["pointer_x"]:
                if midpoint["epoch"] != previous["epoch"] or midpoint["frame"] < previous["next_frame"]:
                    skip_reason = "The held display lacks same-epoch completed counted midpoint travel."
                    break
                # Display holds retain the verified pre-correction deadlines, with fresh cues.
                anchor = {key: previous[key] for key in ("frame", "epoch", "pointer_x")}
                anchor["source"] = "fresh_paused_midpoint_before_counted_correction"
                midpoint["counted_midpoint_anchor"] = anchor
                captures[-1]["counted_midpoint_anchor"] = anchor
                write_json(folder / "midpoint-captures.json", {"accepted_pointer_pixels": [128, 129], "captures": captures})
                write_json(folder / "midpoint.json", midpoint)
                record("midpoint_display_held", dance=dance, frame=midpoint["frame"], pointer_x=x,
                    advanced_emulator_frames=midpoint["frame"] - anchor["frame"], counted_midpoint_anchor=anchor)
                break
        frames = math.ceil((128 - x) / options["pointer_pixels_per_emulator_frame"])
        adjustment = {"frame": midpoint["frame"], "epoch": midpoint["epoch"], "pointer_x": x,
            "next_frame": midpoint["frame"] + frames, "path": midpoint["image"]["path"]}
        adjustments.append(adjustment)
        write_json(folder / "midpoint-adjustments.json", {"adjustments": adjustments})
        path = folder / "midpoint-adjustment.lua"
        path.write_text("return " + lua_value(adjustment) + "\n", encoding="utf-8", newline="\n")
        adjusted = client.command("retime-midpoint", path.as_posix())
        if adjusted.get("recovery_required"):
            adjustment.update(accepted=False, rejection_reason=adjusted["warning"])
            write_json(folder / "midpoint-adjustments.json", {"adjustments": adjustments})
            skip_reason = adjusted["warning"]
            record("midpoint_adjustment_rejected", dance=dance, frame=midpoint["frame"],
                pointer_x=x, reason=skip_reason, additional_emulator_frames=0)
            break
        checked_dance_result(adjusted)
        record("midpoint_adjusted", dance=dance, frame=midpoint["frame"], pointer_x=x,
             additional_emulator_frames=frames, next_frame=adjustment["next_frame"])
    image = load_image(midpoint["image"]["path"])
    cues = {side: cue_positions(image, side, dance) for side in ("left", "right")}
    write_json(folder / "midpoint-cues.json", {"frame": midpoint["frame"], "epoch": midpoint["epoch"],
        "expected_cues": options["cues_per_dance"], "cues": cues, "source": "fresh_paused_native_pixels"})
    record("midpoint_cues", dance=dance, frame=midpoint["frame"], pointer_x=midpoint["position"]["x"],
        left=len(cues["left"]), right=len(cues["right"]))
    reading = {"source": "scheduled_paused_midpoint", "sampled_frames": [midpoint["frame"]],
        "unreadable_frames": [midpoint["frame"]] if midpoint["position"]["x"] is None else [],
        "continuous_demo_observation": False, "scheduled_speed": options["pointer_pixels_per_emulator_frame"]}
    write_json(folder / "demo-reading.json", reading)
    try:
        if skip_reason:
            raise ReviewNeeded(skip_reason)
        planned = response_plan(midpoint, image, rank=options["rank"], expected_cues=options["cues_per_dance"],
            speed=options["pointer_pixels_per_emulator_frame"], hold_frames=options["hold_frames"],
            offset_frames=options["offset_frames"], dance=dance,
            side_offset_pixels=options["rank_timing"]["cue_side_offset_pixels"],
            counted_midpoint_anchor=midpoint.get("counted_midpoint_anchor"), cue_readings=cues)
    except ReviewNeeded as error:
        planned = skipped_response(midpoint, dance, options, str(error))
    if not planned.get("skip_reason"):
        origin = planned.get("counted_midpoint_anchor") or {"frame": midpoint["frame"], "pointer_x": midpoint["position"]["x"]}
        planned["response_end_frame"] = math.floor(origin["frame"] + (128 - origin["pointer_x"]) / planned["speed"]
            + options["rank_timing"]["response_frames"] + 0.5)
        planned["response_end_frame"] = max(midpoint["frame"] + 1, planned["response_end_frame"])
    planned.update(feedback_schedule(planned, delay_frames=options["first_feedback_delay_frames"], rank_timing=options["rank_timing"]))
    if dance == 1:
        planned["first_feedback_frame"] = next((frame for press, frame in zip(planned["presses"], planned["feedback_frames"])
            if press["input_index"] == 1), None)
    record("response_selected", dance=dance, expected_inputs=options["cues_per_dance"],
        planned_inputs=len(planned["presses"]), skipped_inputs=planned["skipped_input_count"],
        conflicts=len(planned["cues"]["conflicts"]), reason=planned.get("skip_reason"))
    write_json(folder / "response-plan.json", planned)
    path = folder / "response-plan.lua"
    path.write_text("return " + lua_value(planned) + "\n", encoding="utf-8", newline="\n")
    checked_dance_result(client.command("plan", path.as_posix()))
    return midpoint, planned, reading


def stage5_responses(runtime, plan):
    """Use the existing main client, Lua callbacks, deadlines, and slot ownership."""
    responses, last = [], None
    entry_saved = runtime.boundary["save_state"] if runtime.boundary else None
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
                checked_dance_result(runtime.command("visit", name, path.as_posix())), slot=None))

        try:
            gap = plan["leader_switch_gap_frames"] if dance in (3, 5) else 0
            checked_dance_result(runtime.command("select-dance", dance, folder.as_posix(), gap))
            if gap:
                checked_dance_result(runtime.action("wait-gap", expected="leader_switch_gap_complete"))
            if dance == 1:
                settle_saved_overlay(runtime, folder, dance, entry_saved, "demo", plan["native_osd_settle_host_seconds"])
            midpoint, planned, reading = prepare_response(runtime.emulator, runtime, folder, dance, plan, record=runtime.record)
            acknowledge("rank-dance-start", midpoint["frame"], midpoint["epoch"])
            responded = checked_dance_result(runtime.action("mirror"))
            if dance == 1:
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
            skipped.update(feedback_schedule(skipped, delay_frames=plan["first_feedback_delay_frames"], rank_timing=plan["rank_timing"]))
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
            if dance == 1 and not observation["trial"]["first_feedback_verified"]:
                acknowledge("rank-dance-offset", recovered["frame"], recovered["epoch"])
            responded = checked_dance_result(runtime.action("mirror", expected="dance_response_complete"))
        last = acknowledge("rank-dance-finish", responded["frame"], midpoint["epoch"])
        trial = last["checkpoint"]["snapshot"]["trial"]
        value = {"dance": dance, "rank": plan["rank"], "midpoint_frame": midpoint["frame"],
            "midpoint_pointer_x": midpoint["position"]["x"], "midpoint_timing": midpoint.get("midpoint_timing"),
            "end_frame": responded["frame"], "presses": trial["presses"], "judgments": trial["judgments"],
            "excellent_count": trial["excellent_count"], "miss_count": trial["miss_count"],
            "unreadable_demo_frames": len(reading["unreadable_frames"]), "observation_policy": "scheduled",
            "checkpoint_receipt": last["host_receipt"], "retained_state": None,
            "expected_inputs": plan["cues_per_dance"], "input_count": trial["inputs"],
            "skipped_input_count": trial["skipped_input_count"], "skipped_inputs": trial["skipped_inputs"],
            "conflict_count": trial["conflict_count"], "selection": planned["cues"]}
        if planned.get("counted_midpoint_anchor"):
            value["counted_midpoint_anchor"] = planned["counted_midpoint_anchor"]
        responses.append(value)
        write_json(folder / "dance-result.json", value)
        for path in folder.glob("judgment-*.gd"):
            stacked_gd(path).save(path.with_suffix(".png"))
        runtime.json("dance-progress.json", {"completed_dances": dance, "rank_timing": plan["rank_timing"], "trial": trial})
        runtime.record("dance_complete", dance=dance, feedback_policy="first_button_only",
            fresh_excellent_judgments=trial["excellent_count"], total_excellent=trial["total_excellent_count"],
            total_misses=trial["total_miss_count"], skipped_inputs=trial["total_skipped_inputs"])
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
            if isinstance(error, TimeoutError):
                raise
            reason = (str(error) or type(error).__name__).replace("|", "/").replace("\r", " ").replace("\n", " ")
            checked_dance_result(runtime.command("warn", reason))
            runtime.record("dance_warning", action=action, reason=reason, continuation="counted_handoff")
            checked_dance_result(runtime.action(action, expected=expected, name=name))
