"""Collect all first-party Platinum contest ribbons under one shared host deadline."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contest import rank_dance, sections, stage1_load, stage2_prep, stage3_enter, stage4_dress, stage5_dance, stage6_act
from contest.artifacts import cleanup_run_folders
from contest.dance_timings import all_fixed_timings
from contest.runtime import ACTING_ENTRY_SLOT, DIAGNOSTIC_SLOT, ContestRuntime, checkpoint_identity
from host.checkpoints import write_json
from host.paths import ROOT
from contest.messages import ContestCallerActive, abort_message, is_timeout
from contest.platinum import GAME_CODES, identify_rom
from contest.runtime import ROM
from contest.timing_profiles import check_profiles, load_profile, section, setting
from host.paths import project_path


HERE = Path(__file__).resolve().parent
dress_grid = stage4_dress.DRESS_GRID
dress_seconds = stage4_dress.AUTO_SECONDS
dress_pickup_frames = stage4_dress.PICKUP_FRAMES
dress_drag_frames = stage4_dress.DRAG_FRAMES
dress_release_frames = stage4_dress.DRAG_RELEASE_FRAMES
dress_page_wait_frames = stage4_dress.PAGE_WAIT_FRAMES
debug = 0
total_timeout_seconds = 120 * 60
stage_timeout_seconds = 5 * 60
acting_round_seconds = stage6_act.ROUND_SECONDS
acting_final_seconds = stage6_act.FINAL_SECONDS
OSD_SETTLE_SECONDS = section(None, "stage5_dancing")["native_overlay_settle_host_seconds"]
STAGES = {1: stage1_load, 2: stage2_prep, 3: stage3_enter,
          4: stage4_dress, 5: stage5_dance, 6: stage6_act}


def chain_plan(*, debug=debug,
                total_timeout=total_timeout_seconds, stage_timeout=stage_timeout_seconds,
                osd_settle_seconds=None, offset_frames=None,
                acting_round_seconds=None, acting_final_seconds=None,
                dress_grid=dress_grid, dress_seconds=None,
                dress_pickup_frames=None, dress_drag_frames=None,
                dress_release_frames=None, dress_page_wait_frames=None, game_code="CPUJ", timing_profile=None):
    profile = load_profile(game_code) if timing_profile is None else timing_profile
    dance_values = section(profile, "stage5_dancing")
    osd_settle_seconds = setting(osd_settle_seconds, dance_values, "native_overlay_settle_host_seconds")
    offset_frames = setting(offset_frames, dance_values, "input_offset_frames")
    if isinstance(debug, bool) or not isinstance(debug, int) or debug not in (0, 1):
        raise ValueError("debug must be 0 or 1.")
    for name, value in (("total timeout", total_timeout), ("stage timeout", stage_timeout)):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"The {name} must be positive finite host seconds.")
    if isinstance(osd_settle_seconds, bool) or not math.isfinite(osd_settle_seconds) or osd_settle_seconds < 0:
        raise ValueError("Overlay settling must be nonnegative host seconds.")
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise ValueError("The dance input offset must be whole emulator frames.")
    acting = stage6_act.acting_plan(host_timeout=stage_timeout, round_seconds=acting_round_seconds,
        final_seconds=acting_final_seconds, from_stage5=True, uncapped=True, source_slot=ACTING_ENTRY_SLOT, timing_profile=profile)
    dressing = stage4_dress.dress_plan(host_timeout=stage_timeout, from_stage3=True,
        dress_grid=dress_grid, dress_seconds=dress_seconds, pickup_frames=dress_pickup_frames,
        drag_frames=dress_drag_frames, drag_release_frames=dress_release_frames,
        page_wait_frames=dress_page_wait_frames, timing_profile=profile)
    acting_round_seconds = acting["rounds"][0]["mash_game_seconds"]
    acting_final_seconds = acting["rounds"][-1]["mash_game_seconds"]
    dressing["automatic_dressing"]["frame_limit"] = False
    facts = {1: "Twelve central player/receptionist pixels; one paused Options/Bag/party/accessory snapshot confirms Text Speed = Fast, HP > 0, Sheen > 0, at least two moves, and not an Egg; startup warnings cover accessory copies < 10, Sheen < 255, and missing scarves.",
        2: "Validated first-party ribbon flags, ordered Items pocket, and required held-item result.",
        3: "All 5/10/15/20 rank-selected empty accessory slots on the native top screen; no button or text matching.",
        4: "Zero-frame Stage 3 handoff, fresh accessory grid/rank marker, counted dressing deadline, and a readable green Dance 1 demo pointer.",
        5: "Fixed category/rank metrics and cue spacing, early-targeted midpoints, non-fatal cue/input skips, one first-button feedback result, six counted responses, input-free lead dances, and the scripted B transition.",
        6: "Profile-selected counted input-free entry frames, confirmed native pause, all 15 empty star slots and available move categories matched in the first capture, four fresh judge readings, completed contest, overworld, and target ribbon."}
    return {"game": profile["game_code"], "timing_profile": profile, "debug": debug,
        "uncapped": True, "total_timeout_seconds": total_timeout, "stage_timeout_seconds": stage_timeout,
        "runtime": {"host_callers": 1, "lua_windows": 1, "lua_attachments": 1, "checkpoint_engines": 1,
                    "bridge": "contest/super_contest.lua", "terminal_native_cleanups": 1},
        "speed_policy": {"setup": "100_percent_uncapped_fast_forward_off", "automatic_dressing": "uncapped",
                         "normal_handoffs": "preserve", "terminal": "restore_normal_once"},
        "deadline_policy": "fresh stage budget per visit, capped by the original whole-run deadline",
        "native_osd_settle_host_seconds": osd_settle_seconds,
        "dance_offset_frames": offset_frames,
        "dance_pointer_calibration_frames": 0,
        "dance_observation_policy": "scheduled",
        "dance_first_feedback_policy": "warn_and_continue",
        "dance_timings": all_fixed_timings(timing_profile=profile), "dance_rank_selection": "current accepted contest target.category and target.rank",
        "dance_midpoint_policy": "target x=127 with floored travel; correct early captures input-free, re-anchor at fresh midpoints, and skip unusable inputs locally",
        "dance_feedback_policy": "check only Dance 1's first input with bounded recovery; recognize the Excellent bubble palette, count every other feedback outcome as Miss, warn and continue; skipped inputs remain separately accounted",
        "dance_failure_policy": "account for skipped inputs and continue through the counted response endpoints and Acting handoff",
        "dressing": dressing["automatic_dressing"], "stage4_entry_mode": dressing["entry_mode"],
        "stage4_entry_render_frames": dressing["entry_render_frames"],
        "stage4_transition": dressing["timing"],
        "stage6_direct_render_frames": acting["handoff_render_frames"],
        "acting_round_game_seconds": acting_round_seconds,
        "acting_final_game_seconds": acting_final_seconds,
        "acting_wait_emulator_frames": [item["mash_frames"] for item in acting["rounds"]],
        "acting_between_round_readiness": acting["between_round_readiness"],
        "acting_move_policy": acting["move_policy"], "acting_move_category_read_policy": acting["move_category_read_policy"],
        "acting_move_navigation": acting["move_navigation"],
        "target_selection": "first missing first-party Sinnoh contest ribbon; select the next missing ribbon after each win",
        "sections": [{"stage": stage, "callable": f"contest.sections.{sections.RUNNERS[stage].__name__}",
                      "reused_stage": str(Path(module.__file__).relative_to(ROOT)),
                      "checkpoint_slot": stage if debug == 1 and stage in (2, 4, 5, 6) else None,
                      "checkpoint_facts": facts[stage]} for stage, module in STAGES.items()],
        "ribbon_miss": {"source": "stage_6_overworld", "load_slot": None,
                        "requires": "completed_contest_and_acknowledged_stage_6_ribbon_miss",
                        "preserve": "current ribbons, Bag opening/cursor, and initial empty-move-slot blacklist",
                        "stages": [2, 3, 4, 5, 6]},
        "ribbon_success": {"source": "stage_6_overworld", "stages": [2, 3, 4, 5, 6],
                           "preserve": "acquired ribbons, Bag opening state, and Bag cursor"},
        "handoff_policy": "all stages and ribbon-miss reentries continue from the current accepted paused state through the persistent dispatcher; zero quick-loads or Lua restarts",
        "checkpoint_policy": "pause, fresh zero-frame Lua checks, policy-selected state save, final log acknowledgement",
        "save_policy": {"enabled": debug == 1, "loads_enabled": False, "screen_checks": "log_only",
                         "memory_check_slots": [2, 6] if debug == 1 else [],
                         "handoff_slots": [4, ACTING_ENTRY_SLOT] if debug == 1 else [],
                         "temporary_slot": DIAGNOSTIC_SLOT if debug == 1 else None,
                         "routine_dance_states": False, "diagnostic_states": debug == 1},
        "run_folder_cleanup": {"hook": "after_emulator_and_lua_ready", "parent": "tmp/contest",
            "older_runs": "delete_all", "current_run": "retain_until_next_launch",
            "persistent_evidence": "contest/debug"},
        "unknown_screen_policy": "abort paused with evidence for later review",
        "finish": "all 20 first-party Sinnoh contest ribbons owned, or paused abort/timeout; restore normal speed"}


def receipt_path(result):
    return Path(result["checkpoint_receipt"])


def run(*, run_name, controller_config=None, fresh_start=False, game_code=None, **options):
    started = time.monotonic()
    rom = controller_config.rom if controller_config is not None else ROM
    actual_code = identify_rom(project_path(rom))["code"]
    if game_code is not None and game_code != actual_code:
        raise ValueError("The requested timing profile does not match the selected ROM game code.")
    if options.get("timing_profile") is None:
        options["timing_profile"] = load_profile(actual_code)
    elif options["timing_profile"]["game_code"] != actual_code:
        raise ValueError("The supplied timing profile does not match the selected ROM game code.")
    plan = chain_plan(**options)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    parent = ROOT / "tmp" / "contest"
    parent.mkdir(parents=True, exist_ok=True)
    # The OS releases this one-caller lock even if its host process exits unexpectedly.
    with (parent / "active-host.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ContestCallerActive("Another contest script is already running. Stop the current run before starting another.") from error
        try:
            return _run(run_name, plan, started, controller_config=controller_config, fresh_start=fresh_start)
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _run(run_name, plan, started, *, controller_config=None, fresh_start=False):
    work = ROOT / "tmp" / "contest" / run_name
    if work.exists():
        raise FileExistsError("Use a fresh contest run name.")
    if controller_config is not None and controller_config.mailbox_dir != (work / "mailbox").resolve():
        raise ValueError("The main runner requires its mailbox inside the current run folder.")
    if controller_config is None:
        runtime = ContestRuntime(run_name, work, plan, started)
    else:
        from contest.runtime import TimedController
        runtime = ContestRuntime(run_name, work, plan, started, controller=TimedController(controller_config))
    if fresh_start:
        runtime.validate_startup()
        runtime.lifecycle["startup_guard"] = "active_script_lock"
        runtime.lifecycle["closed_existing_pids"] = runtime.controller.close_existing()
        session = None
    else:
        session = runtime.confirm_ownership()
    runtime.prepare_fresh_ownership(session)
    work.mkdir(exist_ok=False)
    (work / "checkpoints").mkdir()
    (work / "mailbox").mkdir()
    owner = {"run_id": run_name, "host_pid": os.getpid(), "runtime": "pt-super-contest", "temporary_run": True}
    write_json(work / "run-owner.json", owner)
    result = {"outcome": "fail", "run_id": run_name, "host_pid": os.getpid(), "work_dir": str(work),
        "settings": plan, "visits": [], "active_stage": None, "completed_acting_attempts": 0,
        "contest_entries": 0, "ribbon_acquired": False, "ribbons_acquired": [],
        "consecutive_ribbon_misses": 0,
        "all_ribbons_owned": False, "owned_ribbon_count": None, "initial_owned_ribbon_count": None,
        "cleanup_manifests": []}
    write_json(work / "runner-setup.json", {**result, "whole_run_deadline": runtime.total_deadline})
    counts = Counter()

    def record(phase, **details):
        event = {"phase": phase, "host_elapsed_seconds": round(time.monotonic() - started, 3), **details}
        write_json(work / "runner-progress.json", {**result, "event": event})
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    runtime.on_progress = record

    def retain_progress(child):
        checkpoint, saved = child["checkpoint"], child["save_state"]
        accepted = receipt_path(child)
        result.update(last_checkpoint=str(accepted), last_save_state=saved["retained_path"] if saved else None, frame=checkpoint["frame"],
                      bag_context=dict(runtime.bag_context))
        if checkpoint["checkpoint"] in {"stage-2-prep", "stage-6-act"}:
            party = checkpoint["snapshot"]["party"]
            result.update(owned_ribbon_count=party["owned_count"], all_ribbons_owned=party["all_ribbons_owned"])
            if result["initial_owned_ribbon_count"] is None:
                result["initial_owned_ribbon_count"] = party["owned_count"]
        if checkpoint["checkpoint"] == "stage-6-act":
            result["ribbon_acquired"] = checkpoint["decision"] == "hit"
            if result["ribbon_acquired"] and not any(item["ribbon_bit"] == child["target"]["ribbon_bit"] for item in result["ribbons_acquired"]):
                result["ribbons_acquired"].append({**{key: child["target"][key] for key in ("category", "rank", "ribbon_bit")},
                    "checkpoint": str(accepted)})

    def visit(stage, **inputs):
        counts[stage] += 1
        folder = runtime.begin_visit(stage, counts[stage])
        item = {"stage": stage, "visit": counts[stage], "run_id": run_name, "work_dir": str(folder),
            "outcome": "running", "host_budget_seconds": runtime.stage_deadline - runtime.stage_started}
        result["active_stage"] = stage
        result["visits"].append(item)
        record("stage_start", stage=stage, visit=counts[stage], host_budget_seconds=item["host_budget_seconds"])
        child = None
        try:
            child = sections.RUNNERS[stage](runtime, **inputs)
            retain_progress(child)
            item.update(outcome=child["outcome"], result_path=str(folder / "runner-result.json"), frame=child["frame"],
                source_checkpoint=str(receipt_path(child)))
            if stage == 6:
                result["completed_acting_attempts"] += child["completed_attempts"]
            if stage == 3:
                result["contest_entries"] += 1
            runtime.json("runner-result.json", child)
            runtime.check()
        except (Exception, SystemExit, KeyboardInterrupt) as error:
            item.update(outcome="timeout" if is_timeout(error) else "fail",
                        error=abort_message(error, stage=stage))
            stopped = {**(child or {}), "stage": stage, "run_id": run_name, "work_dir": str(folder),
                "outcome": item["outcome"], "error": item["error"]}
            runtime.json("runner-result.json", stopped)
            record("stage_end", stage=stage, visit=counts[stage], outcome=item["outcome"], error=item["error"])
            raise
        finally:
            item["host_elapsed_seconds"] = time.monotonic() - runtime.stage_started
        runtime.controller.live_deadline = runtime.total_deadline
        record("stage_end", stage=stage, visit=counts[stage], outcome=child["outcome"], frame=child.get("frame"))
        return child

    try:
        record("ownership")
        if fresh_start:
            runtime.setup(fresh_start=True)
        else:
            runtime.setup()
        result["emulator_pid"] = runtime.controller.session()["pid"]
        runtime.controller.log_lua("Cleaning up previous run folders...")
        record("startup_cleanup_start")
        result["cleanup_manifests"].append(str(work / "startup-cleanup.json"))
        startup_cleanup = cleanup_run_folders(work, check=runtime.check)
        removed = len(startup_cleanup["removed_folders"])
        runtime.controller.log_lua(f"Startup cleanup complete: removed {removed} old run folders. Starting the contest.")
        record("startup_cleanup_complete", removed_folders=removed)
        visit(1)
        while True:
            prepared = visit(2)
            if result["all_ribbons_owned"]:
                result.update(outcome="pass", finish_reason="all_ribbons_owned")
                break
            target = prepared["target"]
            result.update(target=target, ribbon_acquired=False)
            record("target_selected", category=target["category"], rank=target["rank"],
                owned_ribbon_count=result["owned_ribbon_count"])
            visit(3)
            visit(4)
            visit(5)
            acted = visit(6, attempt=result["completed_acting_attempts"] + 1)
            if acted["ribbon_acquired"]:
                result["consecutive_ribbon_misses"] = 0
            else:
                result["consecutive_ribbon_misses"] += 1
            record("ribbon_result", acquired=acted["ribbon_acquired"],
                completed_acting_attempts=result["completed_acting_attempts"],
                consecutive_ribbon_misses=result["consecutive_ribbon_misses"],
                owned_ribbon_count=result["owned_ribbon_count"], ribbons_acquired=len(result["ribbons_acquired"]))
            if result["consecutive_ribbon_misses"] == 5:
                message = (f"Contest: WARN - No ribbon earned in {result['consecutive_ribbon_misses']} consecutive contests. "
                    "Does the first Pokemon have good contest stats and suitable moves? Retrying automatically.")
                runtime.controller.log_lua(message)
                record("ribbon_miss_warning", message=message, category=target["category"], rank=target["rank"],
                    completed_acting_attempts=result["completed_acting_attempts"],
                    consecutive_ribbon_misses=result["consecutive_ribbon_misses"])
            write_json(work / "collection-context.json", {"source_context": checkpoint_identity(runtime.boundary["checkpoint"]),
                "source_checkpoint": runtime.boundary["receipt"], "save_state": runtime.boundary["save_state"],
                "next_target": runtime.party.get("next_target"), "party": runtime.party,
                "bag_context": runtime.bag_context, "unavailable_move_slots": runtime.unavailable_move_slots,
                "consecutive_ribbon_misses": result["consecutive_ribbon_misses"],
                "ribbons_acquired": result["ribbons_acquired"]})
            if not acted["ribbon_acquired"]:
                record("retry_contest", source="stage_6_overworld", source_checkpoint=result["last_checkpoint"],
                    stages=[2, 3, 4, 5, 6], category=target["category"], rank=target["rank"], bag_context=dict(runtime.bag_context))
            if result["all_ribbons_owned"]:
                result.update(outcome="pass", finish_reason="all_ribbons_owned")
                break
            if result["ribbon_acquired"]:
                record("next_ribbon", source_checkpoint=result["last_checkpoint"], stages=[2, 3, 4, 5, 6],
                    bag_context=result["bag_context"])
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        if is_timeout(error) or time.monotonic() >= runtime.total_deadline:
            result["outcome"] = "timeout"
        elif result["outcome"] != "timeout":
            result["outcome"] = "fail"
        result["error"] = "timeout" if result["outcome"] == "timeout" else abort_message(error, stage=result["active_stage"])
        result["message"] = result["error"]
        record("abort", outcome=result["outcome"], stage=result["active_stage"], error=result["error"])
    finally:
        final = runtime.finalize(error=result.get("error"))
        result.update({key: value for key, value in final.items() if key != "errors"})
        if final["errors"]:
            if result["outcome"] != "timeout":
                result["outcome"] = "timeout" if any(is_timeout(item) for item in final["errors"]) else "fail"
            result.setdefault("error", "timeout" if result["outcome"] == "timeout" else abort_message("; ".join(final["errors"])))
        elif result["outcome"] == "pass" and time.monotonic() >= runtime.total_deadline:
            result.update(outcome="timeout", error="timeout")
        if result["outcome"] != "pass":
            result["message"] = result["error"] = "timeout" if result["outcome"] == "timeout" else result["error"]
        result["cleanup_errors"] = final["errors"]
        result["total_host_seconds"] = time.monotonic() - started
        write_json(work / "runner-result.json", result)
        record("complete", outcome=result["outcome"], acquired=result["ribbon_acquired"],
            all_ribbons_owned=result["all_ribbons_owned"], owned_ribbon_count=result["owned_ribbon_count"],
            ribbons_acquired=len(result["ribbons_acquired"]),
            stage=result["active_stage"], frame=result.get("frame"), total_host_seconds=result["total_host_seconds"])
    return result


def background(run_name, options):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    plan = chain_plan(**options)
    parent = ROOT / "tmp" / "contest"
    parent.mkdir(parents=True, exist_ok=True)
    if (parent / run_name).exists():
        raise FileExistsError("Use a fresh chain run name.")
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
    """Read files/process liveness only; the detached host remains the sole emulator caller."""
    from host.windows import process_alive

    parent = ROOT / "tmp" / "contest"
    launch = json.loads((parent / f"{run_name}.launch.json").read_text(encoding="utf-8"))
    work = Path(launch["work_dir"])
    while True:
        terminal = work / "runner-result.json"
        if terminal.is_file() and not process_alive(launch["host_pid"]):
            result = json.loads(terminal.read_text(encoding="utf-8"))
            print(json.dumps({key: result[key] for key in ("outcome", "target", "ribbon_acquired", "active_stage",
                "all_ribbons_owned", "initial_owned_ribbon_count", "owned_ribbon_count", "ribbons_acquired", "finish_reason",
                "completed_acting_attempts", "contest_entries", "frame", "emulator_paused", "inputs_released",
                "total_host_seconds", "last_checkpoint", "last_save_state", "work_dir", "error", "cleanup_errors")
                if key in result}, indent=2))
            return 0 if result["outcome"] == "pass" else 1
        if not process_alive(launch["host_pid"]):
            # Final publication and process exit can occur between the two liveness checks.
            if terminal.is_file():
                continue
            raise RuntimeError(f"The chain host exited without a terminal result; inspect {launch['log']}.")
        time.sleep(0.2)


def check(*, lua_dll=None):
    from contest.checks import check_sources, launcher_path
    from contest import artifacts, dance_timings, messages, platinum, rank_dance, runtime, timing_profiles
    from host import controller, dialogs

    modules = (*STAGES.values(), sections, artifacts, dance_timings, messages, platinum,
        rank_dance, runtime, timing_profiles, controller, dialogs)
    python_files = [Path(module.__file__) for module in modules]
    python_files.extend((Path(__file__), launcher_path(), HERE / "checks.py"))
    python_files.extend(ROOT / "host" / name for name in ("bootstrap.py", "lua51.py", "windows.py"))
    check_profiles()
    names = ("resource_root.lua", "super_contest.lua", "sections.lua", "acting_plan.lua", "stage1_load.lua",
        "stage2_prep.lua", "bag_contest.lua", "party_contest.lua", "stage3_enter.lua", "stage4_dress.lua",
        "dress_pixels.lua", "dance_master.lua", "rank_dance.lua", "dance_pixels.lua", "stage6_act.lua", "screen_checks.lua")
    lua_files = [HERE / name for name in names]
    lua_files.extend(sorted((ROOT / "lua").glob("*.lua")))
    check_sources(python_files, lua_files, lua_dll=lua_dll)
    print("Persistent contest Python/Lua syntax OK")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--background", action="store_true")
    mode.add_argument("--wait", action="store_true")
    parser.add_argument("--lua-dll", type=Path, help="Use this Lua 5.1 DLL for the input-free --check.")
    parser.add_argument("--run-name")
    parser.add_argument("--game-code", choices=sorted(GAME_CODES),
                        help="Preview a regional timing profile; execution verifies it against the selected ROM.")
    parser.add_argument("--dress-grid", type=int, choices=(6, 7, 8), default=dress_grid)
    parser.add_argument("--dress-seconds", type=float, help="Override profile dressing game seconds.")
    parser.add_argument("--dress-pickup-frames", type=int)
    parser.add_argument("--dress-drag-frames", type=int)
    parser.add_argument("--dress-release-frames", type=int)
    parser.add_argument("--dress-page-wait-frames", type=int)
    parser.add_argument("--debug", type=int, choices=(0, 1), default=debug,
                        help="0 writes no quick saves; 1 saves checkpoints in slots 2, 4, 5, 6 and stopped diagnostics in slot 9.")
    parser.add_argument("--total-timeout", type=float, default=total_timeout_seconds)
    parser.add_argument("--stage-timeout", type=float, default=stage_timeout_seconds)
    parser.add_argument("--osd-settle-seconds", type=float)
    parser.add_argument("--offset-frames", type=int)
    parser.add_argument("--acting-round-seconds", type=float)
    parser.add_argument("--acting-final-seconds", type=float)
    args = parser.parse_args()
    if args.check:
        try:
            check(lua_dll=args.lua_dll)
        except (OSError, ValueError, RuntimeError) as error:
            parser.error(str(error))
        return 0
    if args.lua_dll is not None:
        parser.error("--lua-dll belongs to --check.")
    options = {"debug": args.debug,
        "total_timeout": args.total_timeout, "stage_timeout": args.stage_timeout,
        "osd_settle_seconds": args.osd_settle_seconds, "offset_frames": args.offset_frames,
        "acting_round_seconds": args.acting_round_seconds, "acting_final_seconds": args.acting_final_seconds,
        "dress_grid": args.dress_grid, "dress_seconds": args.dress_seconds,
        "dress_pickup_frames": args.dress_pickup_frames, "dress_drag_frames": args.dress_drag_frames,
        "dress_release_frames": args.dress_release_frames, "dress_page_wait_frames": args.dress_page_wait_frames}
    try:
        if args.plan_only:
            print(json.dumps(chain_plan(game_code=args.game_code or "CPUJ", **options), indent=2))
            return 0
        if not args.run_name:
            parser.error("Supply a fresh --run-name.")
        if args.wait:
            return wait(args.run_name)
        if args.background:
            if args.game_code is not None:
                options["game_code"] = args.game_code
            return background(args.run_name, options)
        result = run(run_name=args.run_name, game_code=args.game_code, **options)
        return 0 if result["outcome"] == "pass" else 1
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
