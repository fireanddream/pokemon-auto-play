"""Main-run Acting plans, fresh render preparation, and bounded visual readiness."""

import math

from contest.inputs import whole
from contest.pt_dance_read import stacked_gd
from contest.timing_profiles import FRAME_RATE, load_profile, section, setting
from host.checkpoints import write_json
from host.guards import wait_visual_match
from host.paths import project_path


DIALOGUE_TOP_BOUNDS = (124, 146, 131, 149)
DIALOGUE_TOP_RGB = (239, 142, 158)


def acting_plan(*, host_timeout, hold_frames=None, release_frames=None,
                round_seconds=None, final_seconds=None, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    values = section(profile, "stage6_acting")
    memory_menu = section(profile, "stage2_preparation")
    hold_frames = setting(hold_frames, values, "menu_hold_frames")
    release_frames = setting(release_frames, values, "menu_release_frames")
    round_seconds = setting(round_seconds, values, "rounds_1_to_3_b_mash_game_seconds")
    final_seconds = setting(final_seconds, values, "final_round_and_results_b_mash_game_seconds")
    if isinstance(host_timeout, bool) or not math.isfinite(host_timeout) or host_timeout <= 0:
        raise ValueError("The host timeout must be positive seconds.")
    whole(hold_frames, 1, "Menu held frames")
    whole(release_frames, 1, "Menu released frames")
    for value in (round_seconds, final_seconds):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0 or round(value * FRAME_RATE) < 1:
            raise ValueError("Acting waits must be positive game seconds covering at least one emulator frame.")
    return {"stage": 6, "timing_profile_source": profile["source"], "host_timeout_seconds": host_timeout,
        "uncapped": True, "speed_percent": 100, "frame_limit": False, "fast_forward": False,
        "retry_policy": "finish_contest_then_prepare_and_reenter_from_current_overworld",
        "capture_policy": "judges_and_acting_order_each_round_and_available_move_types_hearts_at_entry",
        "move_policy": "exclude_empty_previous_and_unrecognized_then_most_raw_hearts_or_first_legal_fallback",
        "unrecognized_move_policy": "warn_discard_then_first_available_if_no_recognized_legal_move",
        "zero_heart_policy": "only_if_one_recognized_legal_move_remains",
        "move_category_read_policy": "once_per_attempt_from_first_round_framebuffer",
        "move_heart_read_policy": "once_per_attempt_from_first_round_framebuffer",
        "memory_x_input": f"X:{memory_menu['menu_hold_frames']}:{memory_menu['menu_release_frames']}",
        "memory_x_frames": memory_menu["menu_hold_frames"] + memory_menu["menu_release_frames"],
        "acting_order_read_policy": "each_round_from_player_panel_fill_pixels",
        "move_navigation": "remembered_row_major_2x2",
        "judge_policy": "early_four_stars_with_matching_move_else_first_fewest",
        "entry_mode": "stage5_handoff", "handoff_render_frames": values["handoff_render_frames"],
        "entry_render_method": "input_bridge_advance_then_fresh_paused_capture",
        "between_round_readiness": {
            "retry_game_seconds": values["round_readiness_retry_b_mash_game_seconds"],
            "retry_frames": round(values["round_readiness_retry_b_mash_game_seconds"] * FRAME_RATE),
            "settle_game_seconds": values["round_readiness_settle_b_mash_game_seconds"],
            "settle_frames": round(values["round_readiness_settle_b_mash_game_seconds"] * FRAME_RATE),
            "checks": "fresh_paused_judges_order_and_move_menu_with_15_empty_stars_at_entry",
            "matched_sequence": "plan_paused_then_b_delay_and_selection_in_one_combo",
            "deadline": "existing_section_host_deadline", "messages": "silent"},
        "post_acting_readiness": {
            "retry_game_seconds": values["post_acting_retry_b_mash_game_seconds"],
            "retry_frames": round(values["post_acting_retry_b_mash_game_seconds"] * FRAME_RATE),
            "settle_game_seconds": values["post_acting_settle_b_mash_game_seconds"],
            "settle_frames": round(values["post_acting_settle_b_mash_game_seconds"] * FRAME_RATE),
            "checks": "accepted_startup_player_position_and_closed_dialogue_top",
            "dialogue_top_bounds": DIALOGUE_TOP_BOUNDS,
            "deadline": "existing_section_host_deadline", "messages": "silent",
            "before": "first_result_memory_x"},
        "timing": {"hold_frames": hold_frames, "release_frames": release_frames,
            "mash_hold_frames": values["mash_hold_frames"], "mash_release_frames": values["mash_release_frames"],
            "frame_rate": FRAME_RATE, "frame_conversion": "nearest emulator frame at the native frame rate"},
        "rounds": [{"round": index, "mash_game_seconds": seconds, "mash_frames": round(seconds * FRAME_RATE)}
            for index, seconds in enumerate((round_seconds, round_seconds, round_seconds, final_seconds), 1)],
        "success": "retain_current_run_evidence_until_next_launch",
        "unexpected_result": "abort_paused_with_evidence", "memory_proven_phase": False}


def overworld_reference_from_checkpoint(path, previous):
    """Reuse the accepted startup player/hall pixels and the reviewed closed-dialogue tip."""
    from PIL import Image

    receipt_path = project_path(path)
    if previous.get("status") != "complete" or previous.get("proceed") is not True:
        raise ValueError("The overworld reference requires an acknowledged checkpoint.")
    screen = previous["snapshot"].get("screen", {})
    if (screen.get("confirmed") is not True or screen.get("expected") != "loaded_overworld"
            or screen.get("source") != "local_reference_pixels"
            or any(screen.get(key) != previous[key] for key in ("frame", "epoch"))):
        raise ValueError("Use the accepted main-run contest-hall overworld checkpoint.")
    image_path = project_path(screen["path"])
    with Image.open(image_path) as opened:
        if opened.format != "PNG" or opened.size != (256, 384):
            raise ValueError("The overworld reference must be a native stacked PNG.")
        image = opened.convert("RGB")
    regions = ((124, 54, 131, 67), (124, 78, 131, 95))
    coordinates = [(x, y) for y in (54, 60, 66, 78, 84, 94) for x in (124, 130)]
    points = [[x, y, *image.getpixel((x, y))] for x, y in coordinates]
    left, top, right, bottom = DIALOGUE_TOP_BOUNDS
    dialogue_points = [[x, y, *DIALOGUE_TOP_RGB]
        for y in range(top, bottom) for x in range(left, right)]
    return {"points": points, "player_points": points[6:], "regions": regions,
        "dialogue_top": {"points": dialogue_points, "bounds": DIALOGUE_TOP_BOUNDS,
            "source": "reviewed_closed_hall_top_pixels"},
        "reference": {"path": str(image_path),
        "checkpoint": str(receipt_path), "frame": previous["frame"], "epoch": previous["epoch"],
        "source": "accepted_startup_contest_hall",
        "scope": "central_player_and_receptionist_with_closed_dialogue_top"}}


def retain_capture(snapshot, work, *, judges=False):
    item = snapshot["image"]
    path = project_path(item["path"])
    if not path.is_relative_to(work):
        raise RuntimeError("The fresh screenshot is outside this Stage 6 run.")
    image = stacked_gd(path)
    png = path.with_suffix(".png")
    image.save(png)
    result = {**item, "png": str(png)}
    if judges:
        crop = path.with_name(path.stem + "-judges.png")
        image.crop((80, 0, 176, 50)).save(crop)
        result["judges_png"] = str(crop)
    return result


def _wait_readiness(emulator, client, work, check_action, accepted_action, *arguments, settle_matched=True):
    controller = emulator.controller

    def observe():
        response = client.command(check_action, *arguments)
        snapshot = response["observation"]
        capture = retain_capture(snapshot, work)
        sample = {**snapshot, "image": capture, "end_frame": capture["frame"]}
        evidence = project_path(capture["path"]).with_suffix(".json")
        write_json(evidence, sample)
        return sample

    def tap(sample):
        if sample["readiness"]["ready"] and not settle_matched:
            return
        pause = sample["pause_plan"]
        progressed = emulator.command("combo", "inputs", *pause["steps"],
            timeout=None, deadline=controller.live_deadline)
        if progressed.get("stop_reason") != "combo_complete" or progressed["frame"] != pause["end_frame"]:
            raise RuntimeError("The Acting readiness B segment did not reach its configured endpoint.")
        sample["end_frame"] = progressed["frame"]
        write_json(project_path(sample["image"]["path"]).with_suffix(".json"), sample)

    return wait_visual_match(controller, observe, tap, matches=lambda sample: sample["readiness"]["ready"],
        accept=lambda _: client.command(accepted_action, *arguments))


def wait_round_ready(emulator, client, work, number):
    """Match entry and pre-rounds 2-4 while paused; the selected plan owns the B delay."""
    if number not in (1, 2, 3, 4):
        raise ValueError("Readiness polling applies only before Acting rounds 1-4.")
    return _wait_readiness(emulator, client, work, "round-readiness", "round-ready", number,
        settle_matched=False)


def wait_post_acting_ready(emulator, client, work):
    """Match the startup position and closed dialogue tip, then settle before the first X."""
    return _wait_readiness(emulator, client, work, "post-acting-readiness", "post-acting-settled")


def prepare_render(emulator, client, initial, *, plan):
    """Advance input-free from the accepted handoff, then confirm native pause."""
    controller = emulator.controller
    render_frames = plan["handoff_render_frames"]
    controller.remaining()
    session = controller.session()
    if initial.get("emulator_paused") is not True or not controller.paused(session):
        raise RuntimeError("Stage 6 render preparation requires its paused source.")
    advanced = emulator.command("advance", render_frames, timeout=None, deadline=controller.live_deadline)
    if advanced.get("emulator_paused") is not True or not controller.paused(session):
        raise RuntimeError("The configured handoff render advance did not return to native pause.")
    prepared = client.command("render-prepared")
    if prepared["frame"] != initial["frame"] + render_frames:
        raise RuntimeError("The Acting handoff changed during its configured render preparation.")
    if prepared["epoch"] != initial["epoch"]:
        raise RuntimeError("The Acting source epoch changed during render preparation.")
    return {"start_frame": initial["frame"], "end_frame": prepared["frame"],
        "epoch": prepared["epoch"], "frames": prepared["frame"] - initial["frame"],
        "input_policy": "none", "method": "input_bridge_advance", "entry_mode": "stage5_handoff",
        "emulator_paused": True}
