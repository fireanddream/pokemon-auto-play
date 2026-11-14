"""Exercise selected CPUJ contest responses using fixed measured timings."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contest import rank_dance, sections, stage1_load, stage3_enter, stage4_dress, super_contest
from contest.artifacts import OwnedArtifacts, cleanup_stale, inactive_runs
from contest.dance_timings import DEFAULT_OFFSET_FRAMES, RANKS, canonical_rank, fixed_timing as measured_timing
from contest.pt_dance_read import POINTER_LOWER_SPRITES, POINTER_SPRITES
from contest.runtime import ContestRuntime
from contest.stage3_enter import CATEGORIES
from host.checkpoints import write_json
from host.paths import ROOT


DEFAULT_CATEGORY = "Cool"
CHECK_WAIT_FRAMES = round(stage4_dress.FRAME_RATE)
CAPTURE_WAIT_FRAMES = round(2 * stage4_dress.FRAME_RATE)
RESULT_KEYS = ("outcome", "target", "active_stage", "completed_contests", "measured_contests", "total_contests",
    "loop_count", "required_midpoint_visits", "measurements_path", "results_path", "total_inputs", "required_response_inputs", "total_excellent",
    "timing_misses", "unreadable_feedback", "total_skipped_inputs", "total_conflicting_cues", "baseline_restored", "finish_reason", "frame", "emulator_paused",
    "inputs_released", "emulator_closed", "measurement_dances", "total_samples", "required_samples", "entry_restored",
    "total_host_seconds", "last_checkpoint", "last_save_state", "work_dir", "error", "cleanup_errors")

def fixed_target(rank, *, category=DEFAULT_CATEGORY):
    rank = canonical_rank(rank)
    if category not in CATEGORIES:
        raise ValueError("Select Cool, Beauty, Cute, Smart, or Tough.")
    category_index = CATEGORIES.index(category)
    tier = RANKS.index(rank)
    return {"category": category, "rank": rank,
        "category_down_presses": category_index, "rank_down_presses": tier, "party_slot": 1}


def fixed_timing(category, rank):
    target = fixed_target(rank, category=category)
    return measured_timing(target["category"], target["rank"])


def debug_plan(*, category=None, rank=None, total_timeout=super_contest.total_timeout_seconds,
               stage_timeout=super_contest.stage_timeout_seconds, skip_joined_from=None, resume_paused_from=None,
               measure_only=False, offset_frames=DEFAULT_OFFSET_FRAMES, no_timeout=False,
               loops=1, midpoint_diagnostics=False, measurement_dances=2):
    if category is None and rank is not None:
        raise ValueError("Supply a category with the selected rank, or omit both for all 20.")
    for name, value in (("total timeout", total_timeout), ("stage timeout", stage_timeout)):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"The {name} must be positive finite host seconds.")
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise ValueError("The debug input offset must be a whole emulator-frame count.")
    if not isinstance(no_timeout, bool):
        raise ValueError("The no-timeout setting must be a boolean.")
    if isinstance(loops, bool) or not isinstance(loops, int) or loops < 1:
        raise ValueError("The loop count must be a positive whole number.")
    if not isinstance(midpoint_diagnostics, bool) or midpoint_diagnostics and measure_only:
        raise ValueError("Midpoint diagnostics require the input debug pass.")
    if isinstance(measurement_dances, bool) or measurement_dances not in (1, 2):
        raise ValueError("Measure one or two complete dances before the next demo entry.")
    if measurement_dances == 1 and (not measure_only or resume_paused_from):
        raise ValueError("One-dance observation requires a fresh --measure-only run.")
    targets = ([fixed_target(rank, category=category)] if rank is not None else
        [fixed_target(tier, category=category) for tier in RANKS] if category is not None else
        [fixed_target(tier, category=kind) for kind in CATEGORIES for tier in RANKS])
    excluded, sources = set(), []
    for value in skip_joined_from or []:
        path = Path(value)
        path = path if path.is_absolute() else ROOT / path
        if path.is_dir():
            path /= "dance-results.json" if (path / "dance-results.json").is_file() else "measurements.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("mode") not in ("measurement", "debug_inputs") or not isinstance(report.get("contests"), list):
            raise ValueError("Skip sources must be saved contest debug reports.")
        for item in report["contests"]:
            if item["status"] != "pending":
                target = fixed_target(item["target"]["rank"], category=item["target"]["category"])
                excluded.add((target["category"], target["rank"]))
        sources.append(str(path.resolve()))
    skipped = [target for target in targets if (target["category"], target["rank"]) in excluded]
    targets = [target for target in targets if (target["category"], target["rank"]) not in excluded]
    if not targets:
        raise ValueError("Every selected contest was already started in the supplied reports.")
    targets = [{**target, **({"debug_loop": loop} if loops > 1 else {})}
        for loop in range(1, loops + 1) for target in targets]
    resume = None
    if resume_paused_from:
        if not measure_only:
            raise ValueError("Retained entry recovery requires --measure-only; response inputs need a fresh contest entry.")
        source = Path(resume_paused_from)
        source = source if source.is_absolute() else ROOT / source
        previous = json.loads((source / "runner-result.json").read_text(encoding="utf-8"))
        if (len(targets) != 1 or previous["target"] != targets[0] or previous["active_stage"] != 4
                or previous.get("emulator_paused") is not True or previous.get("inputs_released") is not True
                or previous.get("cleanup_errors") or previous["outcome"] == "pass"):
            raise ValueError("Resume one matching target from a clean paused Stage 4 stop.")
        visit = next(item for item in reversed(previous["visits"]) if item["stage"] == 4)
        folder = Path(visit["work_dir"])
        setup = json.loads((folder / "runner-setup.json").read_text(encoding="utf-8"))
        start = setup["source_frame"] + setup["stylus"]["hold_frames"] + setup["stylus"]["release_frames"] \
            + setup["transition"]["mash_frames"]
        interval = setup["entry_poll"]["wait_frames"]
        history = [{"path": str(path), "frame": start + int(path.stem.split("-")[-1]) * interval,
                    "epoch": setup["source_context"]["epoch"]} for path in sorted(folder.glob("sample-*.gd"))]
        if not history or history[-1]["frame"] != previous["frame"]:
            raise ValueError("The retained entry images must reach the stopped frame exactly.")
        resume = {"run_id": previous["run_id"], "emulator_pid": previous["emulator_pid"],
            "frame": previous["frame"], "history": history, "source": str(source.resolve())}
    entry = stage3_enter.entry_plan(targets[0]["category"], targets[0]["rank"], host_timeout=stage_timeout)
    transition = stage4_dress.dress_plan(host_timeout=stage_timeout, from_stage3=True)["timing"]
    transition_inputs, remaining = [], transition["mash_frames"]
    while remaining:
        hold = min(transition["mash_hold_frames"], remaining)
        release = min(transition["mash_release_frames"], remaining - hold)
        transition_inputs.append(f"A:{hold}:{release}")
        remaining -= hold + release
    plan = {"game": "CPUJ", "runner": "contest_dance_debug", "mode": "measurement",
        "debug": 0, "true_retry": 0, "uncapped": True,
        "total_timeout_seconds": total_timeout, "stage_timeout_seconds": stage_timeout,
        "host_timeouts_enabled": not no_timeout,
        "runtime": {"host_callers": 1, "lua_windows": 1, "lua_attachments": 1, "checkpoint_engines": 1,
            "bridge": "contest/super_contest.lua", "adapter": "contest/debug_measure.lua"},
        "target": targets[0], "targets": targets, "total_contests": len(targets),
        "loop_count": loops, "midpoint_diagnostics": midpoint_diagnostics,
        "skipped_targets": skipped, "skip_source_reports": sources,
        "resume_paused": resume,
        "target_selection": "loop-major, category-major, rank-minor; repeat the selected combinations in each loop",
        "speed_policy": {"setup": "uncapped", "dressing": "uncapped", "dancing": "uncapped",
            "terminal": "restore_normal_once"},
        "deadline_policy": "no whole-run or per-stage host deadline; completion or manual closure" if no_timeout
            else "existing stage host budget per visit, capped by the whole-run deadline",
        "native_osd_settle_host_seconds": 0,
        "baseline": {"source": "new run-owned state after game load, B, and its full released wait",
            "load_timing": stage1_load.load_plan()["timing"], "reload_after_each_completed_combination": True,
            "native_transport_slot": 10, "restore_transport_slot_on_finish": True},
        "dressing": {"enabled": False, "frame_limit": False,
            "finish": {"x": 230, "y": 173, "hold_frames": 2, "release_frames": 1}},
        "enrollment": {"continuous": True, "intro_game_seconds": entry["timing"]["intro_game_seconds"],
            "intro_frames": entry["timing"]["intro_frames"], "endpoint_check": "dress_up_entry"},
        "transition": {**transition, "inputs": transition_inputs, "continuous_after_finish": True},
        "entry_poll": {"wait_frames": CHECK_WAIT_FRAMES, "wait_emulator_seconds": 1,
            "input_policy": "none", "starts_after": "Finish and the complete transition A mash"},
        "dance_observation_policy": "measurement", "measurement_dances": measurement_dances,
        "capture_wait_frames": CAPTURE_WAIT_FRAMES,
        "capture_wait_emulator_seconds": 2, "dance_memory_snapshots": False,
        "dance_button_recognition": False, "dance_inputs": False,
        "pointer_templates": {"full": [vars(sprite) for sprite in POINTER_SPRITES],
            "lower": [vars(sprite) for sprite in POINTER_LOWER_SPRITES], "source": "existing_pink_pointer_sprite_reader"},
        "metrics": ["pointer_pixels_per_emulator_frame", "dance_1_to_dance_2_transition_frames",
            "pokemon_1_to_pokemon_2_transition_frames"],
        "calculation_policy": "skip unreadable/ambiguous samples; retain their elapsed frames; label fitted gaps as estimates/ranges",
        "sections": [{"stage": number, "callable": f"contest.debug_contest.{name}", "checkpoint_facts": facts}
            for number, name, facts in ((1, "load", "Post-B overworld and the new baseline state."),
                (3, "enter", "Selected contest's dress-up controls."),
                (4, "skip_dress", "Immediate Finish and continuous 40-second A mash, then input-free one-second Dance 1 checks."),
                (5, "dance", "Two visually tracked sweeps, then Pokemon 2 / dance 1 / demo."))],
        "save_policy": {"baseline_only": True, "screen_checks": "log_only", "memory_check_slots": [],
            "diagnostic_states": False, "temporary_slot": 10},
        "finish": "final confirmed Pokemon 2 / dance 1 demo capture of the last selected combination; paused"}
    if measure_only and measurement_dances == 1:
        plan.update(capture_policy="input-free frame-by-frame tracking; paused captures at intervals, first response, and first readable Dance 2 entry",
            metrics=["native_cue_spacing"],
            calculation_policy="retain Dance 1 pixels for offline cue-spacing analysis; no dance input planning",
            finish="first readable Pokemon 1 / dance 2 / demo, then immediate native slot-0 baseline reload; paused")
        plan["baseline"].update(native_slot=0, reload_after_final_combination=True)
        plan["sections"][-1].update(checkpoint_facts="Input-free Dance 1; stop on the first readable Dance 2 entry and reload slot 0.")
    if not measure_only:
        profiles = [fixed_timing(target["category"], target["rank"]) for target in targets]
        plan.update(mode="debug_inputs", dance_observation_policy="debug_inputs",
            dance_button_recognition=True, dance_inputs=True, feedback_policy="record_all_buttons",
            fixed_timings=profiles, required_response_inputs=sum(6 * item["cues_per_dance"] for item in profiles),
            required_midpoint_visits=6 * len(targets),
            input_timing={"hold_frames": rank_dance.INPUT_HOLD_FRAMES, "release_frames": rank_dance.FEEDBACK_RELEASE_FRAMES,
                "offset_frames": offset_frames},
            midpoint_policy={"capture_target_x": 127, "accepted_pointer_pixels": [128, 129], "rounding": "floor",
                "capture_each_visit": True, "retain_early_captures": True,
                "early_policy": "one_counted_input_free_adjustment" if midpoint_diagnostics else "counted_input_free_adjustment",
                "diagnostic_continuation": midpoint_diagnostics,
                "post_fix_policy": "record_and_continue" if midpoint_diagnostics else "skip_response_if_no_progress",
                "late_policy": "record_and_play_remaining_inputs", "unreadable_policy": "counted_input_free_response_skip"},
            feedback={"delay_frames": rank_dance.FIRST_FEEDBACK_DELAY_FRAMES,
                "source": "shared_scheduled_input_feedback_capture", "timing_miss_policy": "record_and_continue",
                "unreadable_feedback_policy": "recover_each_frame_until_next_input_or_response_end_or_full_release_wait",
                "recovery_exhausted_policy": "retain_capture_and_continue", "final_input_policy": "same_as_every_input"},
            cue_spacing_policy="fixed_category_rank",
            cue_selection_policy={"conflicts": "demo_prefix_response_suffix", "demo_prefix_count": "required_cues_minus_two",
                "response_suffix_count": 2, "both_incomplete": "confirmed_pairs_only",
                "excess": "confirmed_pairs_then_demo_prefix_response_suffix", "missing_inputs": "record_and_continue"},
            calculation_policy="fixed measured speed, gaps, and category/rank cue spacing; fresh cues and pointer alignment for each response",
            finish="final accounted response boundary, then native slot-0 baseline reload; paused")
        for field in ("capture_wait_frames", "capture_wait_emulator_seconds", "metrics", "measurement_dances"):
            plan.pop(field)
        plan["runtime"]["dance_adapter"] = "contest/rank_dance.lua"
        plan["baseline"].update(native_slot=0, reload_after_final_combination=True)
        plan["sections"][-1].update(checkpoint_facts="Six responses; every expected input executed or skipped, with feedback for executed inputs.")
    return plan


def load(runtime):
    plan = stage1_load.load_plan()
    runtime.start_section(plan, measurement_targets=runtime.settings["targets"],
        measurement_dances=runtime.settings.get("measurement_dances", 2))
    inputs = runtime.action("combo", "inputs", *plan["inputs"], expected="combo_complete", name="input-result.json")
    capture = runtime.command("capture")
    review, image = runtime.review(capture, plan["acceptance"], filename="stage1.png")
    pending = runtime.command("visit", review["verdict"], review["source"], image.as_posix(), review["note"],
        review["frame"], review["epoch"])
    runtime.acknowledge(pending, slot=10)
    runtime.baseline = dict(runtime.boundary)
    write_json(runtime.work / "baseline.json", runtime.baseline)
    return sections.accepted(runtime, baseline=runtime.baseline,
        input_frames=capture["frame"] - inputs["command_start_frame"])


def reload_baseline(runtime):
    source = runtime.baseline
    saved, checkpoint = source["save_state"], source["checkpoint"]
    retained = Path(saved["retained_path"])
    runtime.emulator.states.validate_header(retained, 10)
    armed = runtime.command("arm-measurement-reload", checkpoint["visit"])
    shutil.copy2(retained, runtime.emulator.states.path(10))
    runtime.emulator.states.load(10, refresh=False, timeout=None, checkpoint_visit=checkpoint["visit"])
    runtime.position = runtime.controller.status()
    if (runtime.position["frame"] != checkpoint["frame"]
            or runtime.position["checkpoint_epoch"] != armed["epoch"] + 1):
        raise RuntimeError("The debug baseline did not restore its saved frame and a fresh load epoch.")
    runtime.boundary, runtime.dance_handoff = source, None
    event = {"source_checkpoint": source["receipt"], "frame": checkpoint["frame"],
        "epoch": runtime.position["checkpoint_epoch"]}
    runtime.lifecycle.setdefault("measurement_baseline_loads", []).append(event)
    runtime.record("measurement_baseline_loaded", **event)
    return event


def retain_capture(runtime, capture, *, protected=False):
    image = capture["image"]
    raw = runtime.register(Path(image["path"]), kind="capture", protected=protected)
    png = runtime.register(raw.with_suffix(".png"), kind="capture", protected=protected)
    stage4_dress.stacked_gd(raw).save(png)
    sample = {"frame": capture["frame"], "epoch": capture["epoch"], "image": str(png), "raw_image": str(raw),
        "visual": capture.get("visual"), "source": "fresh_paused_framebuffer", "direct_read_frames": 0}
    if "survey" in capture:
        sample["survey"] = capture["survey"]
    return sample


def enter(runtime):
    target = runtime.target
    plan = stage3_enter.entry_plan(target["category"], target["rank"], host_timeout=runtime.settings["stage_timeout_seconds"])
    runtime.start_section(plan)
    runtime.action("combo", "inputs", *plan["inputs"], expected="debug_entry_complete", name="input-result.json")
    capture = runtime.command("capture")["observation"]
    sample = retain_capture(runtime, capture)
    if not capture["screen"]["confirmed"]:
        runtime.json("review-request.json", {"run_id": runtime.run_id, "frame": sample["frame"],
            "epoch": sample["epoch"], "image": sample["image"], "source": "fresh_paused_framebuffer",
            "reason": "The continuous enrollment sequence did not confirm dress-up entry.",
            "screen": capture["screen"]})
        raise RuntimeError(f"Continuous enrollment did not reach dress-up; retained image: {sample['image']}")
    runtime.acknowledge(runtime.command("visit"), slot=None)
    return sections.accepted(runtime, target=target, dress_up_capture=sample)


def skip_dress(runtime, *, checkpoint_slot=None):
    plan = {"entry_poll": runtime.settings["entry_poll"], "stylus": runtime.settings["dressing"]["finish"],
        "transition": runtime.settings["transition"], "pointer_templates": runtime.settings["pointer_templates"]}
    if checkpoint_slot is not None:
        plan["retain_entry_state"] = True
    runtime.start_section(plan)
    runtime.action("finish-dress", expected="debug_transition_complete", name="transition-input-result.json")
    while True:
        runtime.action("wait-entry", expected="debug_wait_complete")
        capture = runtime.command("capture")["observation"]
        sample = retain_capture(runtime, capture, protected=True)
        if capture["visual"]["readable"] and capture["visual"]["phase"] == "demo":
            runtime.acknowledge(runtime.command("visit"), slot=checkpoint_slot)
            runtime.dance_handoff = sample
            return sections.accepted(runtime, target=runtime.target, entry_capture=sample)
        if capture["visual"]["phase"] == "response":
            raise RuntimeError("The first clear dance capture is already in a response; review the retained entry images.")


def summarize_measurements(samples):
    """Fit moving segments only; transition gaps retain brackets and pixel uncertainty."""
    usable = [item for item in samples if item["visual"]["readable"]
        and item["visual"]["sequence_status"] == "confirmed"]
    pairs = []
    for index, first in enumerate(usable):
        a = first["visual"]
        for last in usable[index + 1:]:
            b = last["visual"]
            if (first["epoch"] != last["epoch"] or a["sweep"] != b["sweep"] or a["phase"] != b["phase"]
                    or a["x"] in (8, 128, 247) or b["x"] in (8, 128, 247)):
                continue
            frames, pixels = last["frame"] - first["frame"], b["x"] - a["x"]
            if frames > 0 and pixels > 1:
                pairs.append({"frames": [first["frame"], last["frame"]], "pixels": pixels,
                    "pixels_per_frame": pixels / frames, "range": [(pixels - 1) / frames, (pixels + 1) / frames]})
    speed = {"status": "unavailable", "pixels_per_frame": None, "range": None, "pairs": pairs}
    if pairs:
        speed.update(status="estimated", pixels_per_frame=statistics.median(item["pixels_per_frame"] for item in pairs),
            range=[min(item["range"][0] for item in pairs), max(item["range"][1] for item in pairs)])

    def transition(sweep):
        before = [item for item in usable if item["visual"]["sweep"] == sweep
            and item["visual"]["phase"] == "response" and 128 < item["visual"]["x"] < 247]
        after = [item for item in usable if item["visual"]["sweep"] == sweep + 1
            and item["visual"]["phase"] == "demo" and 8 < item["visual"]["x"] < 128]
        value = {"status": "unavailable", "estimate_frames": None, "range_frames": None,
            "definition": "previous response reaches strip end x=248 -> next demo starts at x=8"}
        if not before or not after:
            return value
        first, last = before[-1], after[0]
        elapsed = last["frame"] - first["frame"]
        value.update(status="bracketed", bracket_frames=[first["frame"], last["frame"]], range_frames=[0, elapsed])
        if speed["pixels_per_frame"] is not None:
            distance = 240 - first["visual"]["x"] + last["visual"]["x"]
            low, high = speed["range"]
            lower, upper = elapsed - (distance + 1) / low, elapsed - (distance - 1) / high
            if upper < 0:
                value.update(status="inconsistent_fit", note="Readable pixels do not support this constant-speed gap fit.")
            else:
                value.update(status="estimated", estimate_frames=max(0, round(elapsed - distance / speed["pixels_per_frame"])),
                    range_frames=[max(0, math.floor(lower)), max(0, math.ceil(upper))],
                    model="constant measured moving speed; one-pixel displacement uncertainty")
        return value

    return {"pointer_speed": speed, "dance_1_to_dance_2": transition(1), "pokemon_1_to_pokemon_2": transition(2),
        "sample_count": len(samples), "usable_sample_count": len(usable),
        "skipped_frames": [item["frame"] for item in samples if item not in usable],
        "frame_unit": "emulator_frames", "button_timing_calculated": False}


def input_dance(runtime):
    entry = runtime.dance_handoff
    inputs = runtime.settings["input_timing"]
    if runtime.settings["feedback"]["delay_frames"] != inputs["hold_frames"] + inputs["release_frames"] - 1:
        raise RuntimeError("The shared feedback capture must follow the complete held and released input block.")
    profile = fixed_timing(runtime.target["category"], runtime.target["rank"])
    plan = rank_dance.stage5_plan(rank=runtime.target["rank"], source_frame=entry["frame"],
        host_timeout=runtime.settings["stage_timeout_seconds"], entry_pointer_x=entry["visual"]["x"],
        hold_frames=inputs["hold_frames"], offset_frames=inputs["offset_frames"],
        fixed_timing=profile, feedback_policy="record_all_buttons", observation_policy="scheduled",
        first_feedback_delay_frames=runtime.settings["feedback"]["delay_frames"],
        midpoint_diagnostics=runtime.settings["midpoint_diagnostics"])
    runtime.start_section(plan)
    responses, last = rank_dance.stage5_responses(runtime, plan)
    trial = last["checkpoint"]["snapshot"]["trial"]
    required = plan["required_response_inputs"]
    if (len(responses) != 6 or trial["total_input_count"] + trial["total_skipped_inputs"] != required
            or trial["total_judgment_count"] != trial["total_input_count"]):
        raise RuntimeError("The debug contest must account for every input and capture feedback for executed inputs.")
    for raw in runtime.active_work.rglob("*.gd"):
        runtime.register(raw, kind="capture", protected=True)
        png = runtime.register(raw.with_suffix(".png"), kind="capture", protected=True)
        if not png.is_file():
            stage4_dress.stacked_gd(raw).save(png)
    final = dict(responses[-1]["judgments"][-1]["image"] if trial["final_feedback_bound"]
        else last["checkpoint"]["snapshot"]["image"])
    final["png"] = str(Path(final["path"]).with_suffix(".png"))
    if final["frame"] != last["checkpoint"]["frame"]:
        raise RuntimeError("The final response image must share the acknowledged reload boundary.")
    return sections.accepted(runtime, target=runtime.target, input_debug_complete=True, dances=responses,
        rank_timing=profile, input_count=trial["total_input_count"], feedback_count=trial["total_judgment_count"],
        excellent_count=trial["total_excellent_count"], timing_misses=trial["total_timing_misses"],
        unreadable_feedback=trial["total_unreadable_feedback"], skipped_input_count=trial["total_skipped_inputs"],
        conflict_count=sum(response["conflict_count"] for response in responses), final_capture=final,
        finish_point="final_response_input_feedback" if trial["final_feedback_bound"] else "final_response_accounted",
        **({"midpoint_diagnostics": [
            response["midpoint_diagnostic"] for response in responses]} if plan["midpoint_diagnostics"] else {}))


def dance(runtime):
    if runtime.settings["mode"] == "debug_inputs":
        return input_dance(runtime)
    plan = {"capture_wait_frames": runtime.settings["capture_wait_frames"],
        "measurement_dances": runtime.settings["measurement_dances"],
        "pointer_templates": runtime.settings["pointer_templates"]}
    resume = runtime.settings["resume_paused"]
    if resume:
        history = []
        for index, item in enumerate(resume["history"], 1):
            path = runtime.register(runtime.active_work / f"recovered-{index:05d}.gd", kind="capture", protected=True)
            shutil.copy2(item["path"], path)
            history.append({"frame": item["frame"], "epoch": item["epoch"],
                "image": {"path": path.as_posix(), "frame": item["frame"], "epoch": item["epoch"]},
                "original_image": item["path"], "original_run_id": resume["run_id"]})
        plan["resume_history"] = history
    runtime.start_section(plan)
    if resume:
        pending = runtime.command("resume-visit")
        samples = []
        for captured in pending["recovered_samples"][:-1]:
            sample = retain_capture(runtime, captured, protected=True)
            sample.update(source="retained_original_framebuffer", original_image=captured["original_image"],
                original_run_id=captured["original_run_id"])
            samples.append(sample)
        samples.append(retain_capture(runtime, pending["checkpoint"]["snapshot"], protected=True))
        runtime.acknowledge(pending, slot=None)
    else:
        samples = [runtime.dance_handoff]
    runtime.json("samples.json", {"target": runtime.target, "samples": samples})
    while True:
        runtime.action("observe", expected="debug_wait_complete")
        pending = runtime.command("measurement-visit")
        capture = pending["checkpoint"]["snapshot"]
        sample = retain_capture(runtime, capture, protected=True)
        samples.append(sample)
        runtime.json("samples.json", {"target": runtime.target, "samples": samples})
        metrics = summarize_measurements(samples)
        runtime.json("measurements.json", metrics)
        runtime.acknowledge(pending, slot=None)
        visual = sample["visual"]
        runtime.record("dance_sample", target=runtime.target, frame=sample["frame"], image=sample["image"], visual=visual)
        if visual["sequence_status"] == "ambiguous":
            runtime.json("review-request.json", {"run_id": runtime.run_id, "frame": sample["frame"],
                "epoch": sample["epoch"], "image": sample["image"], "source": "visual_sweep_tracking",
                "reason": visual["reason"], "samples": str(runtime.active_work / "samples.json")})
            raise RuntimeError("Readable dance positions no longer establish a unique sweep count; paused for visual review.")
        if visual.get("finish_confirmed"):
            return sections.accepted(runtime, target=runtime.target, measurement_complete=True,
                metrics=metrics, samples_path=str(runtime.active_work / "samples.json"), final_capture=sample,
                dance_input_count=0, dance_stylus_count=0, measurement_dances=plan["measurement_dances"],
                finish_point="pokemon_1_dance_2_demo" if plan["measurement_dances"] == 1 else "pokemon_2_dance_1_demo")


def validate_run_name(run_name):
    if not isinstance(run_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a fresh run name containing letters, digits, underscores, or hyphens.")


def python_host_alive(pid):
    """A recorded Python caller is inactive if Windows reused its PID for another app."""
    import ctypes
    from host.windows import KERNEL32, ProcessEntry, process_alive

    if not process_alive(pid):
        return False
    snapshot = KERNEL32.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        ready = KERNEL32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ready:
            if entry.pid == pid:
                name = entry.executable.lower()
                return name == Path(sys.executable).name.lower() or name.startswith("python") and name.endswith(".exe")
            ready = KERNEL32.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise ctypes.WinError(ctypes.get_last_error())
        return False
    finally:
        KERNEL32.CloseHandle(snapshot)


def run(*, run_name, **options):
    import msvcrt

    started = time.monotonic()
    plan = debug_plan(**options)
    validate_run_name(run_name)
    parent = ROOT / "tmp" / "contest"
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / "active-host.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("Another contest host caller is active.") from error
        try:
            return _run(run_name, plan, started)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _run(run_name, plan, started, *, pointer_sections=None):
    work = ROOT / "tmp" / "contest" / run_name
    if work.exists():
        raise FileExistsError("Use a fresh debug run name.")
    runtime = ContestRuntime(run_name, work, plan, started)
    session = runtime.confirm_ownership()
    if plan["resume_paused"]:
        resume = plan["resume_paused"]
        if (session is None or session["pid"] != resume["emulator_pid"] or not runtime.controller.paused(session)
                or runtime.controller.status()["frame"] != resume["frame"]):
            raise RuntimeError("Paused continuation requires the same emulator at the exact retained frame.")
    candidates = inactive_runs(work.parent, current_pid=os.getpid(), check=runtime.check, process_alive=python_host_alive)
    runtime.prepare_fresh_ownership(session)
    protected = [Path(session["lua_script"])] if session and session.get("lua_script") else []
    startup_cleanup = cleanup_stale(work.parent, current_pid=os.getpid(), protected=protected,
        check=runtime.check, candidates=candidates)
    work.mkdir(exist_ok=False)
    (work / "checkpoints").mkdir()
    runtime.artifacts = OwnedArtifacts(work, run_name, os.getpid())
    runtime.target = dict(plan["target"])
    write_json(work / "startup-cleanup.json", startup_cleanup)
    write_json(work / "run-owner.json", {"run_id": run_name, "host_pid": os.getpid(),
        "runtime": "pt-super-contest", "runner": plan["runner"]})
    result = {"outcome": "fail", "run_id": run_name, "host_pid": os.getpid(), "work_dir": str(work),
        "settings": plan, "target": plan["target"], "visits": [], "active_stage": None,
        "completed_contests": 0, "measured_contests": 0, "total_contests": len(plan["targets"]),
        "loop_count": plan["loop_count"],
        "measurements_path": str(work / "measurements.json")}
    inputs_mode = plan["mode"] == "debug_inputs"
    pointer_mode = plan["mode"] == "pointer_speed"
    if pointer_mode and pointer_sections is None:
        raise ValueError("Saved-entry pointer measurements require their configured section handlers.")
    handlers = {1: load, 3: enter, 4: skip_dress, 5: dance}
    if pointer_mode:
        handlers.update(pointer_sections["handlers"])
        result.update(total_samples=0, required_samples=plan["required_samples"], entry_restored=False)
    single_dance = not inputs_mode and not pointer_mode and plan["measurement_dances"] == 1
    reload_each = inputs_mode or single_dance
    report_path = work / ("dance-results.json" if inputs_mode else "measurements.json")
    if inputs_mode:
        result.pop("measurements_path")
        result.pop("measured_contests")
        result.update(results_path=str(report_path), total_inputs=0, total_excellent=0, timing_misses=0,
            total_skipped_inputs=0, total_conflicting_cues=0,
            unreadable_feedback=0, required_response_inputs=plan["required_response_inputs"],
            required_midpoint_visits=plan["required_midpoint_visits"], baseline_restored=False)
    else:
        if not pointer_mode:
            result["measurement_dances"] = plan["measurement_dances"]
        if single_dance:
            result.update(total_inputs=0, baseline_restored=False)
    report = {"schema": 1, "run_id": run_name, "mode": plan["mode"], "frame_unit": "emulator_frames",
        "skipped_targets": plan["skipped_targets"], "skip_source_reports": plan["skip_source_reports"],
        "loop_count": plan["loop_count"],
        "contests": [{"target": target, "number": index + 1, "loop": target.get("debug_loop", 1), "status": "pending"}
            for index, target in enumerate(plan["targets"])]}
    if inputs_mode:
        report["midpoint_policy"] = plan["midpoint_policy"]
    elif pointer_mode:
        report["sampling"] = plan["sampling"]
    else:
        report["measurement_dances"] = plan["measurement_dances"]
    write_json(report_path, report)
    write_json(work / "runner-setup.json", {**result, "whole_run_deadline": runtime.total_deadline})
    counts = Counter()
    emulator_closed = False

    def record(phase, **facts):
        event = {"phase": phase, "host_elapsed_seconds": round(time.monotonic() - started, 3), **facts}
        write_json(work / "runner-progress.json", {**result, "event": event})
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    runtime.on_progress = record

    def visit(stage, *, reload=False):
        counts[stage] += 1
        folder = runtime.begin_visit(stage, counts[stage])
        item = {"stage": stage, "visit": counts[stage], "work_dir": str(folder), "outcome": "running",
            "host_budget_seconds": runtime.stage_deadline - runtime.stage_started if runtime.stage_deadline is not None else None}
        result["active_stage"] = stage
        result["visits"].append(item)
        record("stage_start", stage=stage, host_budget_seconds=item["host_budget_seconds"])
        try:
            if reload:
                reload_baseline(runtime)
            child = handlers[stage](runtime)
            checkpoint, saved = child["checkpoint"], child["save_state"]
            result.update(last_checkpoint=child["checkpoint_receipt"], frame=checkpoint["frame"])
            if saved:
                result["last_save_state"] = saved["retained_path"]
            if runtime.target != result["target"]:
                raise RuntimeError("The debug section changed its configured contest target.")
            item.update(outcome=child["outcome"], result_path=str(folder / "runner-result.json"),
                frame=child["frame"], source_checkpoint=child["checkpoint_receipt"])
            runtime.json("runner-result.json", child)
            runtime.check()
        except (Exception, SystemExit, KeyboardInterrupt) as error:
            item.update(outcome="timeout" if isinstance(error, TimeoutError) else "fail", error=str(error) or type(error).__name__)
            runtime.json("runner-result.json", {"stage": stage, "run_id": run_name, "work_dir": str(folder), **item})
            raise
        finally:
            item["host_elapsed_seconds"] = time.monotonic() - runtime.stage_started
            runtime.artifacts.register_tree(folder)
        runtime.controller.live_deadline = runtime.total_deadline
        record("stage_end", stage=stage, outcome=child["outcome"], frame=child["frame"])
        return child

    try:
        runtime.setup(resume_measurement=plan["resume_paused"] is not None)
        result["emulator_pid"] = runtime.controller.session()["pid"]
        if not plan["resume_paused"]:
            visit(1)
        for index, target in enumerate(plan["targets"]):
            runtime.target = dict(target)
            result["target"] = target
            if pointer_mode:
                result["entry_restored"] = False
            if pointer_mode and index:
                pointer_sections["reset"](runtime)
                visit(1)
            entry = report["contests"][index]
            entry["status"] = "running"
            folder = work / "visits" / f"s5-{index + 1:03d}"
            if reload_each:
                result["baseline_restored"] = False
            if inputs_mode:
                entry["result_path"] = str(folder / "runner-result.json")
            else:
                entry.update(samples_path=str(folder / "samples.json"), measurements_path=str(folder / "measurements.json"))
            write_json(report_path, report)
            record("input_target" if inputs_mode else "measurement_target",
                category=target["category"], rank=target["rank"], number=index + 1, loop=target.get("debug_loop", 1))
            if not plan["resume_paused"]:
                visit(3, reload=index > 0 and not reload_each and not pointer_mode)
                visit(4)
            captured = visit(5)
            entry.update(final_capture=captured["final_capture"], checkpoint_receipt=captured["checkpoint_receipt"])
            if inputs_mode:
                entry.update({key: captured[key] for key in
                     ("rank_timing", "input_count", "feedback_count", "excellent_count", "timing_misses", "unreadable_feedback",
                      "skipped_input_count", "conflict_count")})
                if plan["midpoint_diagnostics"]:
                    entry["midpoint_diagnostics"] = captured["midpoint_diagnostics"]
                for total, field in (("total_inputs", "input_count"), ("total_excellent", "excellent_count"),
                        ("total_skipped_inputs", "skipped_input_count"), ("total_conflicting_cues", "conflict_count"),
                        ("timing_misses", "timing_misses"), ("unreadable_feedback", "unreadable_feedback")):
                    result[total] += captured[field]
                write_json(report_path, report)
                entry["baseline_reload"] = reload_baseline(runtime)
                entry["status"] = "completed"
                result.update(frame=runtime.position["frame"], baseline_restored=True)
            else:
                entry.update(status="captured", metrics=captured["metrics"],
                    dance_input_count=captured["dance_input_count"], dance_stylus_count=captured["dance_stylus_count"])
                if pointer_mode:
                    if captured["sample_count"] != plan["sampling"]["samples_per_target"]:
                        raise RuntimeError("Each pointer-speed target must complete its configured sample count.")
                    entry.update(status="completed", sample_count=captured["sample_count"],
                        entry_capture=captured["entry_capture"], entry_state=captured["entry_state"],
                        entry_reloads=captured["entry_reloads"], final_reload=captured["final_reload"],
                        last_sample_checkpoint=captured["last_sample_checkpoint"])
                    result["total_samples"] += captured["sample_count"]
                    result["measured_contests"] += 1
                    result.update(frame=runtime.position["frame"], entry_restored=True)
                elif single_dance:
                    entry["baseline_reload"] = reload_baseline(runtime)
                    entry["status"] = "completed"
                    result.update(frame=runtime.position["frame"], baseline_restored=True)
                    result["measured_contests"] += 1
                elif all(captured["metrics"][name]["status"] == "estimated"
                        for name in ("pointer_speed", "dance_1_to_dance_2", "pokemon_1_to_pokemon_2")):
                    result["measured_contests"] += 1
            result["completed_contests"] += 1
            write_json(report_path, report)
            record("contest_completed" if inputs_mode else "contest_captured", target=target,
                **{key: result[key] for key in ("completed_contests", "measured_contests", "total_inputs",
                    "total_excellent", "timing_misses", "unreadable_feedback", "total_skipped_inputs", "total_conflicting_cues") if key in result})
        if inputs_mode and result["total_inputs"] + result["total_skipped_inputs"] != result["required_response_inputs"]:
            raise RuntimeError("Executed and skipped inputs must account for the selected rank requirements.")
        if pointer_mode and result["total_samples"] != result["required_samples"]:
            raise RuntimeError("The pointer-speed pass did not finish all configured samples.")
        result.update(outcome="pass", finish_reason="all_responses_accounted_baseline_restored"
            if inputs_mode else "all_pointer_samples_complete_entry_restored"
            if pointer_mode else "all_single_dance_captures_complete_baseline_restored"
            if single_dance else "all_measurement_captures_complete")
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        session = runtime.controller.live_session or {}
        pid = result.get("emulator_pid") or session.get("pid")
        emulator_closed = runtime.owned and pid is not None and (not runtime.controller.process_alive(pid)
            or session.get("window") and not runtime.controller.USER32.IsWindow(session["window"]))
        expired = runtime.total_deadline is not None and time.monotonic() >= runtime.total_deadline
        result.update(outcome="stopped" if emulator_closed or isinstance(error, KeyboardInterrupt)
            else "timeout" if isinstance(error, TimeoutError) or expired else "fail",
            error=str(error) or type(error).__name__)
        if result["outcome"] == "stopped":
            result["finish_reason"] = "emulator_closed" if emulator_closed else "manual_interrupt"
        record("abort", stage=result["active_stage"], outcome=result["outcome"], error=result["error"])
    finally:
        final = runtime.finalize(error=result.get("error"), emulator_closed=emulator_closed)
        result.update({key: value for key, value in final.items() if key != "errors"})
        runtime.artifacts.register_tree(runtime.active_work)
        result["cleanup_errors"] = final["errors"]
        if final["errors"]:
            result.update(outcome="fail")
            result.setdefault("error", "; ".join(final["errors"]))
        elif result["outcome"] == "pass" and runtime.total_deadline is not None and time.monotonic() >= runtime.total_deadline:
            result.update(outcome="timeout", error="The whole-run host deadline expired during finalization.")
        for entry in report["contests"]:
            if entry["status"] == "running":
                entry.update(status=result["outcome"], error=result.get("error"))
        report.update({key: result[key] for key in ("outcome", "completed_contests", "measured_contests", "total_inputs",
            "required_response_inputs", "total_excellent", "timing_misses", "unreadable_feedback", "total_skipped_inputs",
            "total_conflicting_cues", "baseline_restored", "total_samples", "required_samples", "entry_restored") if key in result})
        write_json(report_path, report)
        result["total_host_seconds"] = time.monotonic() - started
        write_json(work / "runner-result.json", result)
        record("complete", **{key: result[key] for key in RESULT_KEYS if key in result})
    return result


def background(run_name, options):
    validate_run_name(run_name)
    plan = debug_plan(**options)
    parent = ROOT / "tmp" / "contest"
    parent.mkdir(parents=True, exist_ok=True)
    if (parent / run_name).exists():
        raise FileExistsError("Use a fresh debug run name.")
    log = parent / f"{run_name}.host.log"
    arguments = ["--execute" if value == "--background" else value for value in sys.argv[1:]]
    with log.open("xb") as output:
        child = subprocess.Popen([sys.executable, "-u", "-B", str(Path(__file__).resolve()), *arguments],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    launch = {"host_pid": child.pid, "work_dir": str(parent / run_name), "log": str(log), "settings": plan}
    write_json(parent / f"{run_name}.launch.json", launch)
    print(json.dumps({key: launch[key] for key in ("host_pid", "work_dir", "log")}, indent=2))
    return 0


def wait(run_name):
    from host.windows import process_alive

    validate_run_name(run_name)
    parent = ROOT / "tmp" / "contest"
    launch = json.loads((parent / f"{run_name}.launch.json").read_text(encoding="utf-8"))
    terminal = Path(launch["work_dir"]) / "runner-result.json"
    while True:
        if not process_alive(launch["host_pid"]):
            if not terminal.is_file():
                raise RuntimeError(f"The debug host exited without a terminal result; inspect {launch['log']}.")
            result = json.loads(terminal.read_text(encoding="utf-8"))
            print(json.dumps({key: result[key] for key in RESULT_KEYS if key in result}, indent=2))
            return 0 if result["outcome"] in ("pass", "stopped") else 1
        time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("check", "plan-only", "execute", "background", "wait"):
        mode.add_argument("--" + flag, action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--category", choices=CATEGORIES, help="Select all four ranks in this category, or one with --rank.")
    parser.add_argument("--rank")
    parser.add_argument("--loops", type=int, default=1, help="Repeat the selected combinations in order (default: 1).")
    parser.add_argument("--midpoint-diagnostics", action="store_true",
        help="Record pre/post midpoint errors, apply one existing early correction, and continue off-center.")
    parser.add_argument("--measure-only", action="store_true", help="Use the preserved input-free timing measurement pass.")
    parser.add_argument("--measurement-dances", type=int, choices=(1, 2), default=2,
        help="With --measure-only, use 1 to observe Dance 1 and reload slot 0 on the first readable Dance 2 frame.")
    parser.add_argument("--skip-joined-from", action="append", default=[], metavar="REPORT",
        help="Skip every target already started in a prior debug JSON report; may be repeated.")
    parser.add_argument("--resume-paused-from", metavar="RUN_DIRECTORY",
        help="With --measure-only, continue one target from retained Stage 4 pixels and its still-open paused emulator.")
    parser.add_argument("--total-timeout", type=float, default=super_contest.total_timeout_seconds)
    parser.add_argument("--stage-timeout", type=float, default=super_contest.stage_timeout_seconds)
    parser.add_argument("--no-timeout", action="store_true", help="Disable both run timers; continue through completion or manual closure.")
    parser.add_argument("--offset-frames", type=int, default=DEFAULT_OFFSET_FRAMES,
        help="Response input offset in emulator frames (default: -7).")
    args = parser.parse_args()
    options = {name: getattr(args, name) for name in ("category", "rank", "total_timeout", "stage_timeout",
        "skip_joined_from", "resume_paused_from", "measure_only", "offset_frames", "no_timeout", "loops", "midpoint_diagnostics", "measurement_dances")}
    try:
        if args.check:
            from tests.support.lua51 import Lua51, literal

            ast.parse(Path(__file__).read_text(encoding="utf-8"))
            super_contest.check()
            lua = Lua51()
            try:
                print(lua.run(f"assert(loadfile({literal(Path(__file__).with_name('debug_measure.lua').as_posix())})); "
                    "return 'Dance debug Python/Lua syntax OK'"))
            finally:
                lua.close()
            return 0
        if args.plan_only:
            print(json.dumps(debug_plan(**options), indent=2))
            return 0
        if not args.run_name:
            parser.error("Supply a fresh --run-name.")
        if args.wait:
            return wait(args.run_name)
        if args.background:
            return background(args.run_name, options)
        result = run(run_name=args.run_name, **options)
        return 0 if result["outcome"] in ("pass", "stopped") else 1
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
