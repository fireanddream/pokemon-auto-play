"""Run twenty random scarf equips with held-item and closed-menu checks."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contest import sections, stage1_load, stage2_prep, stage3_enter, super_contest
from contest.artifacts import OwnedArtifacts, cleanup_stale, previous_runs
from contest.debug_contest import validate_run_name
from contest.runtime import ContestRuntime
from contest.stage4_dress import stacked_gd
from host.checkpoints import CheckpointStopped, write_json
from host.paths import ROOT
from contest.timing_profiles import load_profile


HERE = Path(__file__).resolve().parent
CYCLES = 20
REPORT = HERE / "scarf-test-results.txt"
RESULT_KEYS = ("outcome", "completed_cycles", "total_cycles", "menu_closed", "bag_context", "frame",
    "emulator_paused", "inputs_released", "contest_entries", "state_saves", "state_loads", "total_host_seconds",
    "report", "work_dir", "error", "cleanup_errors")


def test_plan(*, total_timeout=super_contest.total_timeout_seconds,
              stage_timeout=super_contest.stage_timeout_seconds, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    for name, value in (("total timeout", total_timeout), ("stage timeout", stage_timeout)):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"The {name} must be positive finite host seconds.")
    return {"game": profile["game_code"], "timing_profile": profile, "runner": "contest_scarf_tester", "debug": 0, "uncapped": True,
        "categories": stage3_enter.CATEGORIES, "total_cycles": CYCLES, "attempts_per_cycle": 1,
        "target_selection": "fresh random choice excluding the scarf held at the accepted current boundary",
        "total_timeout_seconds": total_timeout, "stage_timeout_seconds": stage_timeout,
        "dance_observation_policy": "scarf_test",
        "runtime": {"host_callers": 1, "lua_attachments": 1, "checkpoint_engines": 1,
            "bridge": "contest/super_contest.lua", "adapter": "contest/scarf_tester.lua"},
        "load_timing": stage1_load.load_plan(timing_profile=profile)["timing"],
        "continuation": "current game state; preserve remembered Bag menu selection and item cursor between cycles",
        "checks": ["first-party readiness", "required held scarf after every equip",
            "fresh overworld and closed-menu pixels against the accepted starting view"],
        "checkpoint_policy": "pause, fresh zero-frame checks, state-free acknowledgement once per boundary",
        "save_policy": {"enabled": False, "loads_enabled": False,
            "temporary_slot": None, "diagnostic_states": False},
        "unknown_screen_policy": "abort paused with the actual stopped evidence",
        "finish": "twenty verified scarf equips; final menu closed; paused overworld with normal speed"}


def choose_target(held_item_id, cycle):
    choices = [category for index, category in enumerate(stage3_enter.CATEGORIES)
        if 260 + index != held_item_id]
    category = random.SystemRandom().choice(choices)
    position = stage3_enter.CATEGORIES.index(category)
    return {"category": category, "rank": "Normal", "party_slot": 1, "scarf_cycle": cycle,
        "category_down_presses": position, "rank_down_presses": 0,
        "ribbon_bit": 4 * position, "required_scarf_id": 260 + position}


def load(runtime):
    plan = stage1_load.load_plan(timing_profile=runtime.settings.get("timing_profile"))
    runtime.start_section(plan, scarf_cycles=runtime.settings["total_cycles"])
    inputs = runtime.action("combo", "inputs", *plan["inputs"], expected="combo_complete", name="input-result.json")
    capture = runtime.command("capture")
    review, image = runtime.review(capture, plan["acceptance"], filename="stage1.png")
    pending = runtime.command("visit", review["verdict"], review["source"], image.as_posix(), review["note"],
        review["frame"], review["epoch"])
    runtime.acknowledge(pending, slot=None)
    runtime.party = runtime.boundary["checkpoint"]["snapshot"]["party"]
    return sections.accepted(runtime, input_frames=capture["frame"] - inputs["command_start_frame"])


def equip(runtime, *, target):
    prepared = sections.prep(runtime, target=target)
    snapshot = prepared["checkpoint"]["snapshot"]
    raw = runtime.register(snapshot["image"]["path"], kind="capture")
    image = runtime.register(raw.with_suffix(".png"), kind="capture")
    stacked_gd(raw).save(image)
    return {**prepared, "menu_closed": snapshot["menu_closed"]["confirmed"],
        "overworld_confirmed": snapshot["screen"]["confirmed"], "screenshot": str(image),
        "input_frames": prepared["frame"] - prepared["source_party"]["frame"]}


def write_report(result):
    def item(value):
        return stage2_prep.SCARVES.get(value, "not reached" if value is None else f"Item {value}")

    counts = Counter(trial["status"] for trial in result["trials"])
    lines = ["Random scarf equip test",
        "Scope: twenty cycles on the first party Pokemon; exclude its currently held scarf from every random draw.",
        "Each cycle: equip, close menu, read and verify held scarf, acknowledge, continue from current state.",
        "Bag menu selection and item cursor persist across cycles.",
        f"Outcome: {result['outcome']}; completed {result['completed_cycles']}/{result['total_cycles']}; "
            f"failed {counts['fail']}; unreadable {counts['unreadable']}; skipped {result['skipped_cycles']}.",
        f"Guards: {result['settings']['total_timeout_seconds']:g} whole-run / "
            f"{result['settings']['stage_timeout_seconds']:g} per-visit host seconds; no retries.",
        "Inputs: shared Stage 2 defaults; frame costs are counted emulator frames.",
        "Cycle | Category | Expected | Before | After | Menu closed | Frames | Bag cursor before -> after | Status"]
    for trial in result["trials"]:
        target = trial["target"]
        before = trial["bag_context_before"]["cursor_position"]
        after = trial.get("bag_context_after", {}).get("cursor_position", "unconfirmed")
        lines.append(f"{trial['number']} | {target['category']} | {item(target['required_scarf_id'])} | "
            f"{item(trial.get('source_item_id'))} | {item(trial.get('actual_item_id'))} | "
            f"{trial.get('menu_closed', 'unconfirmed')} | {trial.get('input_frames', 'unconfirmed')} | "
            f"{before} -> {after} | {trial['status']}")
    lines.append("Category totals: selected / passed / failed / unreadable")
    for category in stage3_enter.CATEGORIES:
        selected = [trial for trial in result["trials"] if trial["target"]["category"] == category]
        statuses = Counter(trial["status"] for trial in selected)
        lines.append(f"{category}: {len(selected)} / {statuses['pass']} / {statuses['fail']} / {statuses['unreadable']}")
    lines += [f"Final menu closed: {result['menu_closed']}; "
        f"paused: {result.get('emulator_paused')}; inputs released: {result.get('inputs_released')}.",
        f"Contest entries: {result['contest_entries']}; quick saves: {result['state_saves']}; quick loads: {result['state_loads']}.",
        f"Live duration: {result['total_host_seconds']:.3f} host seconds.",
        f"Evidence and checkpoint receipts: {Path(result['work_dir']).relative_to(ROOT).as_posix()}"]
    if result.get("error"):
        lines.append("Error: " + result["error"].replace("\n", " "))
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*, run_name, **options):
    import msvcrt

    started = time.monotonic()
    plan = test_plan(**options)
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


def _run(run_name, plan, started):
    work = ROOT / "tmp" / "contest" / run_name
    if work.exists():
        raise FileExistsError("Use a fresh scarf-test run name.")
    runtime = ContestRuntime(run_name, work, plan, started)
    session = runtime.confirm_ownership()
    candidates = previous_runs(work.parent, check=runtime.check)
    runtime.prepare_fresh_ownership(session)
    protected = [Path(session["lua_script"])] if session and session.get("lua_script") else []
    startup_cleanup = cleanup_stale(work.parent, protected=protected,
        check=runtime.check, candidates=candidates)
    work.mkdir(exist_ok=False)
    (work / "checkpoints").mkdir()
    runtime.artifacts = OwnedArtifacts(work, run_name, os.getpid())
    write_json(work / "startup-cleanup.json", startup_cleanup)
    write_json(work / "run-owner.json", {"run_id": run_name, "host_pid": os.getpid(), "runner": plan["runner"]})
    result = {"outcome": "fail", "run_id": run_name, "host_pid": os.getpid(), "work_dir": str(work),
        "settings": plan, "visits": [], "active_stage": None, "completed_cycles": 0,
        "total_cycles": plan["total_cycles"], "menu_closed": False, "report": str(REPORT),
        "contest_entries": 0, "state_saves": 0, "state_loads": 0, "trials": []}
    write_json(work / "runner-setup.json", result)
    counts = Counter()
    trial = None

    def record(phase, **facts):
        event = {"phase": phase, "host_elapsed_seconds": round(time.monotonic() - started, 3), **facts}
        write_json(work / "runner-progress.json", {**result, "event": event})
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    runtime.on_progress = record

    def visit(stage, handler, **inputs):
        counts[stage] += 1
        folder = runtime.begin_visit(stage, counts[stage])
        item = {"stage": stage, "visit": counts[stage], "work_dir": str(folder), "outcome": "running"}
        result["active_stage"] = stage
        result["visits"].append(item)
        record("stage_start", stage=stage, visit=counts[stage])
        try:
            child = handler(runtime, **inputs)
            if child["save_state"] is not None:
                raise RuntimeError("The scarf-only tester requires state-free checkpoints.")
            runtime.json("runner-result.json", child)
            item.update(outcome=child["outcome"], frame=child["frame"], checkpoint=child["checkpoint_receipt"])
            result.update(frame=child["frame"], last_checkpoint=child["checkpoint_receipt"])
            runtime.check()
        except (Exception, SystemExit, KeyboardInterrupt) as error:
            item.update(outcome="fail", error=str(error) or type(error).__name__)
            if isinstance(error, CheckpointStopped):
                item["stopped_checkpoint"] = error.record
            runtime.json("runner-result.json", item)
            raise
        finally:
            item["host_elapsed_seconds"] = time.monotonic() - runtime.stage_started
            runtime.artifacts.register_tree(folder)
        runtime.controller.live_deadline = runtime.total_deadline
        record("stage_end", stage=stage, outcome=child["outcome"], frame=child["frame"])
        return child

    try:
        runtime.setup()
        result["emulator_pid"] = runtime.controller.session()["pid"]
        visit(1, load)
        for number in range(1, plan["total_cycles"] + 1):
            target = choose_target(runtime.party["held_item_id"], number)
            runtime.target = dict(target)
            result["target"] = target
            trial = {"number": number, "target": target, "status": "running",
                "source_item_id": runtime.party["held_item_id"], "bag_context_before": dict(runtime.bag_context)}
            result["trials"].append(trial)
            record("scarf_target", number=number, category=target["category"],
                source_scarf=stage2_prep.SCARVES.get(trial["source_item_id"], f"Item {trial['source_item_id']}"),
                expected_scarf=stage2_prep.SCARVES[target["required_scarf_id"]])
            result["menu_closed"] = False
            prepared = visit(2, equip, target=target)
            trial.update(status="pass", actual_item_id=prepared["final_party"]["held_item_id"],
                preparation_mode=prepared["mode"], checkpoint=prepared["checkpoint_receipt"],
                menu_closed=prepared["menu_closed"], screenshot=prepared["screenshot"],
                input_frames=prepared["input_frames"], bag_context_after=dict(runtime.bag_context))
            result.update(completed_cycles=number, menu_closed=True, bag_context=dict(runtime.bag_context))
            record("scarf_cycle_complete", number=number, category=target["category"],
                expected_item_id=target["required_scarf_id"], actual_item_id=trial["actual_item_id"],
                completed_cycles=number, menu_closed=True, bag_context=dict(runtime.bag_context))
        result.update(outcome="pass", finish_reason="all_twenty_scarf_equips_checked_menu_closed")
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        result.update(outcome="timeout" if isinstance(error, TimeoutError)
            or time.monotonic() >= runtime.total_deadline else "fail", error=str(error) or type(error).__name__)
        if trial is not None:
            trial.update(status="fail", error=result["error"])
            if isinstance(error, CheckpointStopped):
                snapshot = error.record.get("snapshot") or {}
                trial["actual_item_id"] = snapshot.get("party", {}).get("held_item_id")
                trial["menu_closed"] = snapshot.get("menu_closed", {}).get("confirmed", False)
                if not snapshot.get("screen", {}).get("confirmed") or not trial["menu_closed"]:
                    trial["status"] = "unreadable"
        record("abort", stage=result["active_stage"], outcome=result["outcome"], error=result["error"])
    finally:
        final = runtime.finalize(error=result.get("error"))
        result.update({key: value for key, value in final.items() if key != "errors"})
        runtime.artifacts.register_tree(runtime.active_work)
        result["cleanup_errors"] = final["errors"]
        if final["errors"]:
            result["outcome"] = "fail"
            result.setdefault("error", "; ".join(final["errors"]))
        elif result["outcome"] == "pass" and time.monotonic() >= runtime.total_deadline:
            result.update(outcome="timeout", error="timeout")
        result["skipped_cycles"] = result["total_cycles"] - len(result["trials"])
        result["total_host_seconds"] = time.monotonic() - started
        write_json(work / "runner-result.json", result)
        write_report(result)
        record("complete", **{key: result[key] for key in RESULT_KEYS if key in result})
    return result


def background(run_name):
    validate_run_name(run_name)
    parent = ROOT / "tmp" / "contest"
    parent.mkdir(parents=True, exist_ok=True)
    if (parent / run_name).exists():
        raise FileExistsError("Use a fresh scarf-test run name.")
    log = parent / f"{run_name}.host.log"
    arguments = ["--execute" if value == "--background" else value for value in sys.argv[1:]]
    if "--run-name" not in arguments:
        arguments += ["--run-name", run_name]
    with log.open("xb") as output:
        child = subprocess.Popen([sys.executable, "-u", "-B", str(Path(__file__).resolve()), *arguments],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    launch = {"host_pid": child.pid, "work_dir": str(parent / run_name), "log": str(log)}
    write_json(parent / f"{run_name}.launch.json", launch)
    print(json.dumps(launch, indent=2))
    return 0


def wait(run_name):
    from host.windows import process_alive

    validate_run_name(run_name)
    parent = ROOT / "tmp" / "contest"
    launch = json.loads((parent / f"{run_name}.launch.json").read_text(encoding="utf-8"))
    while process_alive(launch["host_pid"]):
        time.sleep(0.2)
    result = json.loads((Path(launch["work_dir"]) / "runner-result.json").read_text(encoding="utf-8"))
    print(json.dumps({key: result[key] for key in RESULT_KEYS if key in result}, indent=2))
    return 0 if result["outcome"] == "pass" else 1


def check(*, lua_dll=None):
    from contest.checks import check_module

    super_contest.check(lua_dll=lua_dll)
    check_module(__file__, ("scarf_tester.lua",), "Scarf tester Python/Lua syntax OK", lua_dll=lua_dll)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("check", "plan-only", "execute", "background", "wait"):
        mode.add_argument("--" + flag, action="store_true")
    parser.add_argument("--run-name", help="Fresh run name; generated automatically for execution if omitted.")
    parser.add_argument("--total-timeout", type=float, default=super_contest.total_timeout_seconds)
    parser.add_argument("--stage-timeout", type=float, default=super_contest.stage_timeout_seconds)
    parser.add_argument("--lua-dll", type=Path, help="Use this Lua 5.1 DLL for the input-free --check.")
    args = parser.parse_args()
    try:
        if args.check:
            check(lua_dll=args.lua_dll)
            return 0
        if args.lua_dll is not None:
            parser.error("--lua-dll belongs to --check.")
        options = {"total_timeout": args.total_timeout, "stage_timeout": args.stage_timeout}
        plan = test_plan(**options)
        if args.plan_only:
            print(json.dumps(plan, indent=2))
            return 0
        if args.wait:
            if not args.run_name:
                parser.error("--wait requires --run-name.")
            return wait(args.run_name)
        run_name = args.run_name or datetime.now().strftime("scarf-%Y%m%d-%H%M%S-%f")
        if args.background:
            return background(run_name)
        result = run(run_name=run_name, **options)
        return 0 if result["outcome"] == "pass" else 1
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
