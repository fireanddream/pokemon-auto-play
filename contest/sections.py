"""Callable contest gameplay sections inside the persistent master runtime."""

import random
import shutil

from contest import rank_dance, stage1_load, stage2_prep, stage3_enter, stage4_dress, stage6_act
from contest.dance_timings import fixed_timing
from contest.runtime import ACTING_ENTRY_SLOT
from contest.stage2_bag import Stage2BagState
from contest.timing_profiles import section
from host.checkpoints import write_json


def accepted(runtime, **facts):
    checkpoint, saved = runtime.boundary["checkpoint"], runtime.boundary["save_state"]
    return {"outcome": "pass", "stage": runtime.stage, "run_id": runtime.run_id,
        "section_visit": runtime.section_visit, "work_dir": str(runtime.active_work), "frame": checkpoint["frame"],
        "checkpoint": checkpoint, "checkpoint_receipt": runtime.boundary["receipt"],
        "save_state": saved, "emulator_paused": True, "inputs_released": True,
        "handoff_mode": "persistent_current_state", **facts}


def reviewed_inputs(runtime, plan, expected, filename):
    if plan["stage"] == 1:
        timing = section(runtime.settings.get("timing_profile"), "stage2_preparation")
        x_tap = f"X:{timing['menu_hold_frames']}:{timing['menu_release_frames']}"
        plan = {**plan, "total_emulator_frames": plan["total_emulator_frames"]
            + 2 * (timing["menu_hold_frames"] + timing["menu_release_frames"])}
    runtime.start_section(plan)
    inputs = runtime.action("combo", "inputs", *plan["inputs"], expected="combo_complete", name="input-result.json")
    if plan["stage"] == 1:
        runtime.action("combo", "inputs", x_tap, expected="combo_complete")
        runtime.command("inspect")
        runtime.action("combo", "inputs", x_tap, expected="combo_complete")
    capture = runtime.command("capture")
    review, image = runtime.review(capture, expected, filename=filename)
    pending = runtime.command("visit", review["verdict"], review["source"], image.as_posix(), review["note"],
        review["frame"], review["epoch"])
    runtime.acknowledge(pending, slot=runtime.stage)
    return {"screen_evidence": review, "screenshot": str(image),
        "input_frames": capture["frame"] - inputs["command_start_frame"]}


def readiness_warnings(snapshot):
    party, warnings = snapshot["party"], []
    accessories = snapshot.get("accessories") or {}
    if accessories.get("read_valid") is True:
        count = accessories["total_owned"]
        if count < 10:
            warnings.append(f"Loading: WARN - Only {count} contest accessory copies owned; recommended minimum is 10. Continuing.")
    else:
        warnings.append("Loading: WARN - Contest accessory ownership could not be read reliably. Continuing.")
    if party["sheen"] < 255:
        warnings.append(f"Loading: WARN - First Pokemon Sheen is {party['sheen']}/255. Continuing.")
    owned_items = {party["held_item_id"]} | {item["item_id"] for item in snapshot["bag"]["items"] if item["quantity"] > 0}
    missing = [name for item_id, name in stage2_prep.SCARVES.items() if item_id not in owned_items]
    if missing:
        warnings.append(f"Loading: WARN - Missing scarf items: {', '.join(missing)}. Continuing.")
    return warnings


def load(runtime):
    plan = stage1_load.load_plan(timing_profile=runtime.settings.get("timing_profile"))
    facts = reviewed_inputs(runtime, plan, plan["acceptance"], "stage1.png")
    runtime.overworld = stage6_act.overworld_reference_from_checkpoint(runtime.boundary["receipt"], runtime.boundary["checkpoint"])
    write_json(runtime.work / "overworld-reference.json", runtime.overworld)
    snapshot = runtime.boundary["checkpoint"]["snapshot"]
    party = snapshot["party"]
    runtime.unavailable_move_slots = list(party["unavailable_move_slots"])
    warnings = readiness_warnings(snapshot)
    for message in warnings:
        runtime.controller.log_lua(message)
        runtime.record("readiness_warning", message=message)
    runtime.record("readiness", hp=party["current_hp"], max_hp=party["max_hp"], sheen=party["sheen"],
        move_count=party["move_count"], unavailable_move_slots=party["unavailable_move_slots"],
        text_speed=snapshot["options"]["text_speed"], accessories=snapshot.get("accessories"),
        warnings=warnings, screenshot=facts["screenshot"])
    return accepted(runtime, readiness_warnings=warnings, **facts)


def prep(runtime, *, target=None):
    config, _ = runtime.start_section({"stage": 2})
    initial = runtime.command("inspect")
    runtime.json("source-snapshot.json", initial)
    image = initial["observation"].get("image")
    if image and image["path"].endswith("/stage2-source.gd"):
        runtime.register(image["path"], kind="capture")
        runtime.controller.screenshot(runtime.register(runtime.active_work / "stage2-source.png", kind="capture"))
    plan = stage2_prep.prep_plan(initial["observation"],
        bag_state=Stage2BagState(runtime.bag_context["opened_once"]), cursor=runtime.bag_context["cursor_position"], target=target,
        timing_profile=runtime.settings.get("timing_profile"))
    runtime.target = plan["target"]
    runtime.json("runner-setup.json", {**config, **plan, "shared_host_deadline": runtime.stage_deadline})
    runtime.command("init-prep", runtime.lua("prep-plan.lua", plan).as_posix())
    if plan["inputs"]:
        runtime.action("combo", "inputs", *plan["inputs"], expected="combo_complete", name="input-result.json")
    pending = runtime.command("visit")
    if pending["checkpoint"]["decision"] == "abort":
        runtime.acknowledge(pending, slot=2)
    actual = pending["checkpoint"]["snapshot"]["party"]
    if actual["held_item_id"] != plan["expected_item_id"]:
        expected = stage2_prep.SCARVES.get(plan["expected_item_id"], f"Item {plan['expected_item_id']}")
        raise RuntimeError(f"Scarf equip failed: expected {expected}; actual {actual['held_item']}.")
    runtime.acknowledge(pending, slot=2)
    runtime.party, runtime.bag_context = actual, dict(plan["bag_context"])
    return accepted(runtime, stage2_accepted=True, target=runtime.target, mode=plan["mode"],
        bag_context=runtime.bag_context, source_party=initial["observation"]["party"], final_party=actual,
        all_ribbons_owned=actual["all_ribbons_owned"])


def enter(runtime):
    target = runtime.target
    plan = stage3_enter.entry_plan(target["category"], target["rank"], host_timeout=runtime.settings["stage_timeout_seconds"],
        timing_profile=runtime.settings.get("timing_profile"))
    facts = reviewed_inputs(runtime, plan, plan["acceptance"], "stage3.png")
    return accepted(runtime, stage3_accepted=True, target=target, **facts)


def custom_enter(runtime):
    """Inert continuous-enrollment helper for the current accepted contest target."""
    target = runtime.target
    plan = stage3_enter.entry_plan(target["category"], target["rank"], host_timeout=runtime.settings["stage_timeout_seconds"],
        timing_profile=runtime.settings.get("timing_profile"))
    runtime.start_section(plan)
    inputs = runtime.action("combo", "inputs", *plan["inputs"], expected="combo_complete", name="input-result.json")
    capture = runtime.command("capture")
    image = runtime.register(runtime.active_work / "stage3-custom.png", kind="capture")
    runtime.controller.screenshot(image)
    check = capture["observation"]["automatic_check"]
    sample = {"frame": capture["frame"], "epoch": capture["epoch"], "image": str(image),
        "screen": check, "source": "fresh_paused_framebuffer", "direct_read_frames": 0}
    if not check["confirmed"]:
        runtime.json("review-request.json", {"run_id": runtime.run_id, **sample,
            "reason": "The continuous enrollment sequence did not confirm dress-up entry."})
        raise RuntimeError(f"Continuous enrollment did not reach dress-up; retained image: {image}")
    pending = runtime.command("visit", "pass", "local_reference_pixels", image.as_posix(),
        "Continuous enrollment confirmed dress-up controls.", capture["frame"], capture["epoch"])
    runtime.acknowledge(pending, slot=3)
    return accepted(runtime, stage3_accepted=True, target=target, dress_up_capture=sample,
        input_frames=capture["frame"] - inputs["command_start_frame"])


def dress(runtime):
    settings = runtime.settings["dressing"]
    plan = stage4_dress.dress_plan(host_timeout=runtime.settings["stage_timeout_seconds"], from_stage3=True,
        dress_grid=settings["grid_size"], dress_seconds=settings["game_seconds"],
        pickup_frames=settings["motion"]["pickup_frames"], drag_frames=settings["motion"]["drag_frames"],
        drag_release_frames=settings["motion"]["release_frames"], page_wait_frames=settings["next_page"]["wait_frames"],
        timing_profile=runtime.settings.get("timing_profile"))
    plan["automatic_dressing"]["frame_limit"] = settings["frame_limit"]
    plan["require_demo_pointer"] = True
    plan["acceptance"] = "Fresh green Dance 1 entry with a readable paused demonstration pointer."
    chosen = random.SystemRandom().randint(*stage4_dress.INITIAL_PAGE_TAPS)
    runtime.set_speed("dressing")
    config, _ = runtime.start_section(plan, initial_page_taps=chosen)
    runtime.command("init")
    automatic = stage4_dress.place_accessories(runtime.emulator, runtime, plan, runtime.active_work,
        config["source_frame"], chosen,
        acknowledge=lambda pending: runtime.acknowledge(pending, slot=runtime.settings["save_policy"]["temporary_slot"]),
        record=runtime.record)
    runtime.set_speed("run")
    timing = fixed_timing(runtime.target["category"], runtime.target["rank"], timing_profile=runtime.settings.get("timing_profile"))
    landing, pending = stage4_dress.locate_dance_entry(runtime.emulator, runtime, plan, runtime.active_work,
        record=runtime.record, rank_timing=timing, timing_profile=runtime.settings.get("timing_profile"))
    timing = landing["rank_timing"]
    runtime.acknowledge(pending, slot=4)
    checkpoint = runtime.boundary["checkpoint"]
    saved = runtime.boundary["save_state"]
    handoff = {"source_stage": 4, "source_checkpoint": runtime.boundary["receipt"],
        "source_state": saved["retained_path"] if saved else None, "source_frame": checkpoint["frame"],
        "source_epoch": checkpoint["epoch"], "run_id": runtime.run_id, "dance": 1, "phase": landing["landing"]["phase"],
        "target": runtime.target, "runner_result": str(runtime.active_work / "runner-result.json"),
        "demo_frames": timing["visible_half_frames"], "response_frames": timing["response_frames"],
        "rank_timing": timing, "timing_anchor": landing["timing_anchor"],
        "pointer_calibration": landing["pointer_calibration"],
        "entry_pointer": landing["landing"]["pointer"],
        "source_image": {"path": landing["landing"]["path"], "frame": checkpoint["frame"], "origin_epoch": checkpoint["epoch"],
            "origin_run_id": runtime.run_id, "source": "accepted_stage4_checkpoint_framebuffer"}}
    runtime.json("dance-handoff.json", handoff)
    runtime.dance_handoff = handoff
    return accepted(runtime, stage4_accepted=True, initial_page_taps=chosen, automatic_dressing=automatic, **landing,
        dance_handoff=str(runtime.active_work / "dance-handoff.json"))


def dance(runtime):
    handoff = runtime.dance_handoff
    pointer = handoff["entry_pointer"]
    bounds = pointer["position_bounds"]
    entry_pointer_x = bounds[0] if pointer["status"] == "exact" and bounds is not None else None
    plan = rank_dance.stage5_plan(rank=runtime.target["rank"], host_timeout=runtime.settings["stage_timeout_seconds"],
        source_frame=handoff["source_frame"],
        entry_pointer_x=entry_pointer_x,
        fixed_timing=handoff["rank_timing"], observation_policy=runtime.settings["dance_observation_policy"],
        first_feedback_policy=runtime.settings["dance_first_feedback_policy"],
        offset_frames=runtime.settings["dance_offset_frames"], osd_settle_seconds=runtime.settings["native_osd_settle_host_seconds"],
        timing_profile=runtime.settings.get("timing_profile"))
    source_image = runtime.register(runtime.active_work / "source-stage4.gd", kind="capture")
    shutil.copy2(handoff["source_image"]["path"], source_image)
    runtime.start_section(plan, source_image={**handoff["source_image"], "path": source_image.as_posix()})
    dances, last = rank_dance.stage5_responses(runtime, plan)
    rank_dance.stage5_handoff(runtime, plan)
    runtime.acknowledge(runtime.command("visit", "stage-5-dance"), slot=ACTING_ENTRY_SLOT)
    checkpoint = runtime.boundary["checkpoint"]
    saved = runtime.boundary["save_state"]
    acting = {"source_stage": 5, "source_checkpoint": runtime.boundary["receipt"],
        "source_state": saved["retained_path"] if saved else None, "source_frame": checkpoint["frame"],
        "source_epoch": checkpoint["epoch"], "run_id": runtime.run_id, "phase": "acting_entry", "target": runtime.target,
        "runner_result": str(runtime.active_work / "runner-result.json")}
    runtime.json("acting-handoff.json", acting)
    trial = last["checkpoint"]["snapshot"]["trial"]
    return accepted(runtime, stage5_accepted=True, dances=dances, completed_dances=len(dances),
        rank_timing=plan["rank_timing"], input_count=trial["total_input_count"], excellent_count=trial["total_excellent_count"],
        skipped_input_count=trial["total_skipped_inputs"], miss_count=trial["total_miss_count"],
        offset_check=last["checkpoint"]["snapshot"]["offset_check"], readiness_check="stage6_first_judge_capture",
        acting_handoff=str(runtime.active_work / "acting-handoff.json"))


def act(runtime, *, attempt):
    plan = stage6_act.acting_plan(host_timeout=runtime.settings["stage_timeout_seconds"], from_stage5=True,
        uncapped=True, round_seconds=runtime.settings["acting_round_game_seconds"], final_seconds=runtime.settings["acting_final_game_seconds"],
        source_slot=ACTING_ENTRY_SLOT, timing_profile=runtime.settings.get("timing_profile"))
    plan.update(checkpoint_slot=6 if runtime.settings["debug"] == 1 else None,
        source_slot=ACTING_ENTRY_SLOT if runtime.settings["debug"] == 1 else None,
        temporary_slot=runtime.settings["save_policy"]["temporary_slot"],
        retry_policy="finish_contest_then_prepare_and_reenter_from_current_overworld")
    plan.pop("retry_render_host_seconds")
    runtime.start_section(plan, attempt=attempt, overworld=runtime.overworld,
        unavailable_move_slots=runtime.unavailable_move_slots if runtime.unavailable_move_slots is not None
            else runtime.party["unavailable_move_slots"])
    initial = runtime.command("begin-attempt")
    runtime.json("source-snapshot.json", initial)
    preparation = stage6_act.prepare_render(runtime.emulator, runtime, initial, entry_mode=plan["entry_mode"], plan=plan)
    runtime.json("render-preparation.json", preparation)
    runtime.record("render_prepared", **preparation)
    rounds, images, move_categories = [], set(), None
    for number in range(1, 5):
        pending = runtime.command("round-visit", number)
        if not pending["checkpoint"].get("snapshot"):
            runtime.acknowledge(pending, slot=plan["temporary_slot"])
        snapshot = pending["checkpoint"]["snapshot"]
        capture = stage6_act.retain_capture(snapshot, runtime.active_work, images, judges=True)
        if number == 1:
            move_categories = snapshot["move_categories"]
            runtime.json("move-categories.json", move_categories)
        checked = runtime.acknowledge(pending, slot=plan["temporary_slot"])
        chosen = checked["checkpoint"]["snapshot"]["plan"]
        runtime.record("acting_round", round=number, stars=snapshot["stars"]["counts"], move=chosen["move"], judge=chosen["judge"],
            move_category=chosen["move_category"], move_hearts=chosen["move_hearts"], acting_position=chosen["acting_position"],
            unrecognized_move_fallback=chosen["unrecognized_fallback"],
            matching_category_override=chosen["matching_category_override"])
        inputs = runtime.action("combo", "inputs", *chosen["steps"], expected="combo_complete")
        item = {"round": number, "image": capture, "stars": snapshot["stars"], "acting_order": snapshot["acting_order"], "plan": chosen,
            "checkpoint_visit": checked["checkpoint"]["visit"], "end_frame": inputs["frame"]}
        rounds.append(item)
        runtime.json(f"round-{number}.json", item)
    runtime.command("inspect")
    runtime.action("combo", "inputs", f"X:{plan['timing']['hold_frames']}:{plan['timing']['release_frames']}",
        expected="combo_complete")
    pending = runtime.command("result-visit")
    if not pending["checkpoint"].get("snapshot"):
        runtime.acknowledge(pending, slot=6)
    snapshot = pending["checkpoint"]["snapshot"]
    result_image = stage6_act.retain_capture(snapshot, runtime.active_work, images)
    runtime.acknowledge(pending, slot=6)
    runtime.party = snapshot["party"]
    acquired = runtime.boundary["checkpoint"]["decision"] == "hit"
    return accepted(runtime, outcome="pass" if acquired else "miss", stage6_accepted=True, ribbon_acquired=acquired,
        completed_attempts=1, target=runtime.target, rounds=rounds, move_categories=move_categories,
        result_image=result_image, render_preparation=preparation)


RUNNERS = {1: load, 2: prep, 3: enter, 4: dress, 5: dance, 6: act}
