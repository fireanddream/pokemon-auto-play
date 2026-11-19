"""Main-run accessory dressing and the fresh Dance 1 entry handoff."""

import math

from contest.dance_timings import fixed_timing
from contest.inputs import whole
from contest.pt_dance_read import native_top, read_pointer, read_progress, stacked_gd
from contest.runtime import lua_value
from contest.timing_profiles import FRAME_RATE, load_profile, section, setting
from host.checkpoints import write_json


_DRESS = section(None, "stage4_dress_up")
TRANSITION_SECONDS = _DRESS["dance_transition_a_mash_game_seconds"]
POLL_SECONDS = _DRESS["dance_entry_poll_game_seconds"]
AUTO_SECONDS = _DRESS["accessory_budget_game_seconds"]
DRESS_GRID = 8
PICKUP_FRAMES = _DRESS["accessory_pickup_hold_frames"]
DRAG_FRAMES = _DRESS["accessory_drag_frames"]
DRAG_RELEASE_FRAMES = _DRESS["accessory_release_frames"]
PAGE_WAIT_FRAMES = _DRESS["page_render_wait_frames"]
INITIAL_PAGE_TAPS = (0, 7)
PAGE_FLIP_LIMIT = 14


def dress_plan(*, host_timeout, transition_seconds=None,
               stylus_x=230, stylus_y=173, stylus_hold_frames=None, stylus_release_frames=None,
               dress_grid=DRESS_GRID, dress_seconds=None, pickup_frames=None, drag_frames=None,
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
    whole(dress_grid, 1, "Dress-up grid size")
    if dress_grid not in (6, 7, 8):
        raise ValueError("The accessory grid must be 6 by 6, 7 by 7, or 8 by 8.")
    if isinstance(dress_seconds, bool) or not math.isfinite(dress_seconds) or dress_seconds <= 0:
        raise ValueError("The automatic dress-up duration must be positive game seconds.")
    for value, label in ((pickup_frames, "Accessory pickup frames"), (drag_frames, "Accessory drag frames"),
                         (drag_release_frames, "Accessory release frames")):
        whole(value, 1, label)
    whole(page_wait_frames, 0, "Accessory page render frames")
    dress_frames = round(dress_seconds * FRAME_RATE)
    initial_page_frames = values["page_tap_hold_frames"] + values["page_tap_release_frames"] + page_wait_frames
    if dress_frames < INITIAL_PAGE_TAPS[1] * initial_page_frames + drag_release_frames:
        raise ValueError("The dressing window must cover initial page navigation and the released-input tail.")
    automatic = {"enabled": True, "game_seconds": dress_seconds, "budget_frames": dress_frames,
        "frame_rate": FRAME_RATE, "speed_percent": 100, "frame_limit": False, "grid_size": dress_grid,
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
        "entry_mode": "stage3_handoff", "entry_render_frames": 0, "automatic_dressing": automatic,
        "dressing_speed_percent": 100, "accelerated_frame_limit": False,
        "timing": {"transition_game_seconds": transition_seconds, "mash_frames": mash_frames,
                   "mash_hold_frames": values["transition_mash_hold_frames"], "mash_release_frames": values["transition_mash_release_frames"],
                   "poll_game_seconds": values["dance_entry_poll_game_seconds"], "frame_rate": FRAME_RATE,
                   "frame_conversion": "nearest frame at 67027965 / 1120380 frames per second"},
        "stylus": {"x": stylus_x, "y": stylus_y, "hold_frames": stylus_hold_frames,
                   "release_frames": stylus_release_frames, "button": "Finish"},
        "dance": 1, "require_demo_pointer": True,
        "dance_check": {"tip_x": 8, "tip_rows": [155, 156, 157, 158],
                        "source": "fresh_zero_frame_left_tip_pixels"},
        "checkpoint_slot": 4,
        "acceptance": "Fresh green Dance 1 entry with a readable paused demonstration pointer.",
        "review_source": "fresh_zero_frame_left_tip_pixels", "memory_proven_phase": False}


def pointer_timing(image, *, rank_timing=None, timing_profile=None):
    pointer = read_pointer(native_top(image))
    progress = read_progress({"status": "unreadable", "position_bounds": None, "first_empty_x": None}, pointer,
        frames_per_pixel=(rank_timing or fixed_timing("Cool", "Master", timing_profile=timing_profile))["frames_per_pixel"])
    return {"source": "pt_dance_read_jigglypuff_pixels", "pointer": pointer, "progress": progress,
            "memory_proven": False}


def place_accessories(emulator, client, plan, work, source_frame, initial_page_taps, *, acknowledge, record):
    """The runtime supplies the shared deadline, save ownership, and speed policy."""
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
                or navigated["command_end_frame"] != source_frame + navigation["planned_frames"]):
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


def locate_dance_entry(emulator, client, plan, work, *, record, rank_timing, timing_profile=None):
    """Bind the first green Dance 1 entry to its fresh pointer and fixed metrics."""
    controller = emulator.controller
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
    image_path = work / "stage4.png"
    controller.screenshot(image_path)
    result = {"frame": capture["frame"], "screenshot": str(image_path), "dance_checks": checks,
        "transition_emulator_frames": capture["observation"]["transition"]["total_frames"],
        "dance_entry_poll_frames": wait_frames, "no_input_wait_frames": wait_frames}
    half_frames = rank_timing["visible_half_frames"]
    progress, bounds = reading["progress"], reading["progress"]["frame_range"]
    evidence = {"frame": capture["frame"], "epoch": capture["epoch"], "dance": 1,
        "source": "fresh_zero_frame_left_tip_pixels", "path": (work / "stage4.gd").as_posix(),
        "tip": capture["observation"]["tip"], "phase": progress["phase"], "frame_range": bounds,
        "pointer": reading["pointer"], "pointer_source": reading["source"], "memory_proven": False}
    write_json(work / "endpoint-reading.json", {**evidence, "reading": reading})
    endpoint_path = work / "endpoint.lua"
    endpoint_path.write_text("return " + lua_value(evidence) + "\n", encoding="utf-8", newline="\n")
    anchor = {"source": reading["source"], "exact": False, "demo_start_frame": None,
        "demo_start_frame_range": None, "response_start_frame": None, "response_start_frame_range": None,
        "rank": rank_timing["rank"], "pixels_per_frame": rank_timing["pixels_per_frame"],
        "profile_source": rank_timing["profile_source"]}
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
    result.update(landing=evidence, timing_anchor=anchor, rank_timing=rank_timing)
    record("landing", frame=capture["frame"], dance_phase=progress["phase"], demo_frame_range=bounds, poll_count=poll)
    return result, client.command("visit", endpoint_path.as_posix())
