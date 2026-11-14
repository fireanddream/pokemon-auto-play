"""Measure Smart/Tough Master pointer travel from repeated saved Dance 1 entries."""

from __future__ import annotations

import argparse
import ast
import json
import math
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contest import debug_contest, sections, super_contest
from host.checkpoints import write_json
from host.paths import ROOT


TOTAL_TIMEOUT_SECONDS = 30 * 60
SAMPLES_PER_TARGET = 10
MIN_WAIT_FRAMES = 20


def measurement_plan(*, total_timeout=TOTAL_TIMEOUT_SECONDS, random_seed=None, tough_only=False):
    if not isinstance(tough_only, bool):
        raise ValueError("The Tough-only selection must be a boolean.")
    categories = ("Tough",) if tough_only else ("Smart", "Tough")
    if random_seed is None:
        random_seed = random.SystemRandom().randrange(2 ** 31)
    if isinstance(random_seed, bool) or not isinstance(random_seed, int) or random_seed < 0:
        raise ValueError("The random seed must be a nonnegative integer.")
    plan = debug_contest.debug_plan(category=categories[0], rank="Master", measure_only=True,
        total_timeout=total_timeout, stage_timeout=total_timeout)
    targets = [debug_contest.fixed_target("Master", category=category) for category in categories]
    rng, sampling_targets = random.Random(random_seed), []
    for target in targets:
        profile = debug_contest.fixed_timing(target["category"], target["rank"])
        dance_frames = math.floor(240 / profile["pixels_per_frame"])
        maximum = dance_frames * 3 // 5
        sampling_targets.append({"target": target, "reference_speed": profile["pixels_per_frame"],
            "dance_frames": dance_frames, "max_frames": maximum,
            "wait_frames": rng.sample(range(MIN_WAIT_FRAMES, maximum + 1), SAMPLES_PER_TARGET)})
    plan.update(runner="contest_pointer_speed_debug", mode="pointer_speed", target=targets[0], targets=targets, tough_only=tough_only,
        total_contests=len(targets), required_samples=len(targets) * SAMPLES_PER_TARGET,
        dance_observation_policy="pointer_speed", metrics=["pointer_pixels_per_emulator_frame"],
        target_selection="Tough Master only from fresh loading" if tough_only
            else "Smart Master then one ROM reset and fresh loading for Tough Master",
        sampling={"samples_per_target": SAMPLES_PER_TARGET, "min_frames": MIN_WAIT_FRAMES,
            "maximum_fraction": 0.6, "random_seed": random_seed, "distinct_waits": True,
            "duration_definition": "demo plus response: x=8 through x=248; excludes inter-dance gaps",
            "targets": sampling_targets},
        calculation_policy="free-intercept linear fit of fresh endpoint pixels against actual counted emulator frames",
        capture_policy="one fresh saved entry and ten fresh endpoints per target; no extra post-load rendering frames",
        save_policy={"baseline_and_entry": True, "temporary_slot": 10, "endpoint_states": False},
        finish="ten samples for each selected target, final Tough entry reloaded without frame advance; paused")
    plan["runtime"]["adapter"] = "contest/debug_pointer_speed.lua"
    plan["baseline"].update(reload_after_each_completed_combination=False, reset_between_targets=not tough_only)
    for field in ("measurement_dances", "capture_wait_frames", "capture_wait_emulator_seconds"):
        plan.pop(field)
    plan["sections"][-1].update(checkpoint_facts="Ten counted input-free waits from one saved entry; fresh endpoint pixels and entry reloads.")
    return plan


def fit_speed(start, samples, *, category):
    if len(samples) != SAMPLES_PER_TARGET:
        raise ValueError("Fit the complete ten-sample target.")
    frames = [item["elapsed_frames"] for item in samples]
    positions = [item["endpoint_x"] for item in samples]
    mean_frames, mean_position = sum(frames) / len(frames), sum(positions) / len(positions)
    spread = math.fsum((value - mean_frames) ** 2 for value in frames)
    if spread <= 0:
        raise ValueError("Pointer-speed fitting needs distinct elapsed frame values.")
    speed = math.fsum((x - mean_frames) * (y - mean_position) for x, y in zip(frames, positions)) / spread
    if speed <= 0:
        raise ValueError("The observed endpoint fit has no positive pointer travel.")
    intercept = mean_position - speed * mean_frames

    def residual_metrics(rate):
        origin = mean_position - rate * mean_frames
        residuals = [y - (origin + rate * x) for x, y in zip(frames, positions)]
        squares = math.fsum(value ** 2 for value in residuals)
        return {"speed_pixels_per_frame": rate, "intercept_x": origin,
            "rms_error_pixels": math.sqrt(squares / len(samples)), "max_abs_error_pixels": max(map(abs, residuals)),
            "residuals_pixels": residuals, "sum_squared_error_pixels": squares}

    fitted = residual_metrics(speed)
    points = sorted(zip(frames, positions))
    lower, upper = -math.inf, math.inf
    for index, (a, x) in enumerate(points):
        for b, y in points[index + 1:]:
            lower, upper = max(lower, (y - x - 1) / (b - a)), min(upper, (y - x + 1) / (b - a))
    current = debug_contest.fixed_timing(category, "Master")["pixels_per_frame"]
    proposed = 7 / 12 if category == "Smart" else 5 / 8
    candidates = {"current": residual_metrics(current), "proposed": residual_metrics(proposed),
        "5/9": residual_metrics(5 / 9)}
    for candidate in candidates.values():
        candidate["within_quantization_envelope"] = lower <= candidate["speed_pixels_per_frame"] <= upper
    return {"status": "estimated", "pixels_per_frame": speed, "frames_per_pixel": 1 / speed,
        "intercept_x": intercept, "displacement_intercept_pixels": intercept - start["visual"]["x"],
        "sample_count": len(samples), "sampled_wait_range": [min(frames), max(frames)],
        "rms_error_pixels": fitted["rms_error_pixels"], "max_abs_error_pixels": fitted["max_abs_error_pixels"],
        "slope_standard_error": math.sqrt(fitted["sum_squared_error_pixels"] / (len(samples) - 2) / spread),
        "residuals_pixels": fitted["residuals_pixels"],
        "quantization_envelope": {"compatible": lower <= upper, "range_pixels_per_frame": [lower, upper],
            "assumption": "constant velocity and less than one pixel of rounding error between any endpoint pair"},
        "candidate_fits": candidates, "source": "fresh endpoint pixels and actual emulator-frame waits"}


def reload_entry(runtime, source):
    checkpoint, saved = source["checkpoint"], source["save_state"]
    retained = Path(saved["retained_path"])
    runtime.emulator.states.validate_header(retained, 10)
    armed = runtime.command("arm-measurement-reload", checkpoint["visit"])
    shutil.copy2(retained, runtime.emulator.states.path(10))
    runtime.emulator.states.load(10, refresh=False, timeout=None, checkpoint_visit=checkpoint["visit"])
    runtime.position = runtime.controller.status()
    if (runtime.position["frame"] != checkpoint["frame"]
            or runtime.position["checkpoint_epoch"] != armed["epoch"] + 1
            or runtime.position["emulator_paused"] is not True):
        raise RuntimeError("The measurement entry did not restore its exact frame, fresh epoch, and native pause.")
    runtime.boundary = source
    event = {"source_visit": checkpoint["visit"], "source_checkpoint": source["receipt"],
        "frame": runtime.position["frame"], "epoch": runtime.position["checkpoint_epoch"],
        "post_load_advance_frames": 0, "emulator_paused": True}
    runtime.lifecycle.setdefault("pointer_entry_loads", []).append(event)
    runtime.record("pointer_entry_loaded", **event)
    return event


def measure(runtime):
    start, source = runtime.dance_handoff, dict(runtime.boundary)
    selected = next(item for item in runtime.settings["sampling"]["targets"] if item["target"] == runtime.target)
    runtime.start_section({"sampling": {"sample_count": SAMPLES_PER_TARGET, "min_frames": MIN_WAIT_FRAMES,
        "max_frames": selected["max_frames"], "dance_frames": selected["dance_frames"], "wait_frames": selected["wait_frames"]},
        "pointer_templates": runtime.settings["pointer_templates"]})
    samples, last_reload, last_sample = [], None, None
    report = {"target": runtime.target, "entry_capture": start, "entry_state": source,
        "sampling": selected, "samples": samples}
    runtime.json("samples.json", report)
    for number, frames in enumerate(selected["wait_frames"], 1):
        waited = runtime.action("sample", frames, expected="pointer_speed_wait_complete", name=f"wait-{number:02d}.json")
        pending = runtime.command("speed-sample")
        snapshot = pending["checkpoint"]["snapshot"]
        endpoint = debug_contest.retain_capture(runtime, snapshot, protected=True)
        counted = snapshot["sampling"]
        sample = {"number": number, "wait_frames": frames, "elapsed_frames": counted["elapsed_frames"],
            "start_frame": counted["start_frame"], "start_epoch": counted["start_epoch"],
            "start_x": start["visual"]["x"], "endpoint_x": endpoint["visual"].get("x"), "endpoint": endpoint,
            "input_free_frames": counted["input_free_frames"], "readable_bar_frames": counted["readable_bar_frames"],
            "unreadable_bar_frames": counted["unreadable_bar_frames"], "reset_seen": counted["reset_seen"],
            "strip_end_seen": counted["strip_end_seen"],
            "status": "rejected" if pending["checkpoint"]["decision"] == "abort" else "captured",
            "checkpoint_error": pending["checkpoint"].get("error"),
            "failed_checks": [item["id"] for item in pending["checkpoint"]["checks"] if not item["passed"]]}
        samples.append(sample)
        runtime.json("samples.json", report)
        last_sample = runtime.acknowledge(pending, slot=None)
        if (sample["elapsed_frames"] != frames or waited["frame"] - waited["command_start_frame"] != frames
                or sample["input_free_frames"] != frames or endpoint["epoch"] != counted["start_epoch"]):
            raise RuntimeError("The captured sample differs from its complete counted input-free wait.")
        sample.update(status="accepted", displacement_pixels=sample["endpoint_x"] - sample["start_x"],
            pair_speed=(sample["endpoint_x"] - sample["start_x"]) / frames,
            checkpoint_receipt=last_sample["host_receipt"])
        last_reload = reload_entry(runtime, source)
        sample["reload"] = last_reload
        runtime.json("samples.json", report)
        runtime.record("pointer_sample", category=runtime.target["category"], rank=runtime.target["rank"],
            sample=number, wait_frames=frames, start_x=sample["start_x"], endpoint_x=sample["endpoint_x"],
            pair_speed=sample["pair_speed"], image=endpoint["image"])
    metrics = {"pointer_speed": fit_speed(start, samples, category=runtime.target["category"]),
        "sample_count": len(samples), "frame_unit": "emulator_frames", "button_timing_calculated": False}
    runtime.json("measurements.json", metrics)
    return sections.accepted(runtime, target=runtime.target, measurement_complete=True, metrics=metrics,
        samples_path=str(runtime.active_work / "samples.json"), final_capture=samples[-1]["endpoint"],
        entry_capture=start, entry_state=source, sample_count=len(samples), entry_reloads=len(samples), final_reload=last_reload,
        last_sample_checkpoint=last_sample["host_receipt"], dance_input_count=0, dance_stylus_count=0,
        finish_point="saved_dance_1_entry_restored")


def reset_for_tough(runtime):
    armed = runtime.command("arm-pointer-speed-reset")
    if armed["next_target"] != runtime.target:
        raise RuntimeError("The ROM reset did not authorize the configured Tough target.")
    runtime.action("reset", expected="command_complete", name="rom-reset.json")
    runtime.controller.state_changed("reset")
    runtime.position = runtime.controller.status()
    runtime.boundary, runtime.baseline, runtime.dance_handoff = None, None, None
    event = {"frame": runtime.position["frame"], "epoch": runtime.position["checkpoint_epoch"], "target": runtime.target}
    runtime.lifecycle.setdefault("pointer_rom_resets", []).append(event)
    runtime.record("pointer_rom_reset", **event)


def run(*, run_name, **options):
    import msvcrt

    started = time.monotonic()
    plan = measurement_plan(**options)
    validate_definitions()
    debug_contest.validate_run_name(run_name)
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
            return debug_contest._run(run_name, plan, started, pointer_sections={
                "handlers": {4: lambda runtime: debug_contest.skip_dress(runtime, checkpoint_slot=10), 5: measure},
                "reset": reset_for_tough})
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def background(run_name, options):
    debug_contest.validate_run_name(run_name)
    plan = measurement_plan(**options)
    parent = ROOT / "tmp" / "contest"
    parent.mkdir(parents=True, exist_ok=True)
    if (parent / run_name).exists():
        raise FileExistsError("Use a fresh pointer measurement run name.")
    log = parent / f"{run_name}.host.log"
    arguments = ["--execute", "--run-name", run_name, "--total-timeout", str(plan["total_timeout_seconds"]),
        "--random-seed", str(plan["sampling"]["random_seed"])]
    if plan["tough_only"]:
        arguments.append("--tough-only")
    with log.open("xb") as output:
        child = subprocess.Popen([sys.executable, "-u", "-B", str(Path(__file__).resolve()), *arguments],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    launch = {"host_pid": child.pid, "work_dir": str(parent / run_name), "log": str(log), "settings": plan}
    write_json(parent / f"{run_name}.launch.json", launch)
    print(json.dumps({key: launch[key] for key in ("host_pid", "work_dir", "log")}, indent=2))
    return 0


def validate_definitions():
    from tests.support.lua51 import Lua51, literal

    lua = Lua51()
    try:
        module = literal(Path(__file__).with_suffix(".lua").as_posix())
        master = literal((ROOT / "lua" / "checkpoint_master.lua").as_posix())
        return lua.run(f"""
            local base = {{}}
            for _, name in ipairs({{'stage-1-load', 'stopped'}}) do
                base[name] = {{checks = {{{{id='supported',field='game.supported',expected=true}}}}, branches={{pass='finish'}}}}
            end
            local definitions = dofile({module}).definitions(base, {{}})
            local inactive = function() error('Declaration validation must not operate an emulator') end
            dofile({master}).new({{run_id='pointer-declaration-check', definitions=definitions,
                emulator={{pause=inactive,release=inactive,frame=inactive,epoch=inactive}},
                reader={{snapshot=inactive}}, log=inactive}})
            return 'Saved-entry pointer checkpoint declarations OK'
        """)
    finally:
        lua.close()


def check():
    super_contest.check()
    ast.parse(Path(__file__).read_text(encoding="utf-8"))
    ast.parse(Path(debug_contest.__file__).read_text(encoding="utf-8"))
    print(validate_definitions())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("check", "plan-only", "execute", "background", "wait"):
        mode.add_argument("--" + flag, action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--total-timeout", type=float, default=TOTAL_TIMEOUT_SECONDS, help="Total host seconds (default: 1800).")
    parser.add_argument("--random-seed", type=int, help="Optional reproducible seed for distinct random frame waits.")
    parser.add_argument("--tough-only", action="store_true", help="Restart just Tough Master from fresh loading.")
    args = parser.parse_args()
    options = {"total_timeout": args.total_timeout, "random_seed": args.random_seed, "tough_only": args.tough_only}
    try:
        if args.check:
            check()
            return 0
        if args.plan_only:
            print(json.dumps(measurement_plan(**options), indent=2))
            return 0
        if not args.run_name:
            parser.error("Supply a fresh --run-name.")
        if args.wait:
            return debug_contest.wait(args.run_name)
        if args.background:
            return background(args.run_name, options)
        result = run(run_name=args.run_name, **options)
        return 0 if result["outcome"] == "pass" else 1
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
