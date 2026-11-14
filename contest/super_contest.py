"""Collect all first-party CPUJ contest ribbons under one shared host deadline."""

from __future__ import annotations

import argparse
import ast
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
from contest.artifacts import OwnedArtifacts, cleanup_stale, inactive_runs
from contest.dance_timings import all_fixed_timings
from contest.runtime import ACTING_RETRY_SLOT, DIAGNOSTIC_SLOT, ContestRuntime, checkpoint_identity
from host.checkpoints import write_json
from host.paths import ROOT


HERE = Path(__file__).resolve().parent
dress_grid = stage4_dress.DRESS_GRID
dress_seconds = stage4_dress.AUTO_SECONDS
dress_pickup_frames = stage4_dress.PICKUP_FRAMES
dress_drag_frames = stage4_dress.DRAG_FRAMES
dress_release_frames = stage4_dress.DRAG_RELEASE_FRAMES
dress_page_wait_frames = stage4_dress.PAGE_WAIT_FRAMES
debug = 0
true_retry = 0
total_timeout_seconds = 120 * 60
stage_timeout_seconds = 5 * 60
acting_round_seconds = 64
acting_final_seconds = 132
OSD_SETTLE_SECONDS = 0
STAGES = {1: stage1_load, 2: stage2_prep, 3: stage3_enter,
          4: stage4_dress, 5: stage5_dance, 6: stage6_act}


def chain_plan(*, debug=debug, true_retry=true_retry,
               total_timeout=total_timeout_seconds, stage_timeout=stage_timeout_seconds,
               osd_settle_seconds=OSD_SETTLE_SECONDS, offset_frames=rank_dance.DEFAULT_OFFSET_FRAMES,
               acting_round_seconds=acting_round_seconds, acting_final_seconds=acting_final_seconds,
               dress_grid=dress_grid, dress_seconds=dress_seconds,
               dress_pickup_frames=dress_pickup_frames, dress_drag_frames=dress_drag_frames,
               dress_release_frames=dress_release_frames, dress_page_wait_frames=dress_page_wait_frames):
    for name, value in (("debug", debug), ("true_retry", true_retry)):
        if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
            raise ValueError(f"{name} must be 0 or 1.")
    for name, value in (("total timeout", total_timeout), ("stage timeout", stage_timeout)):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"The {name} must be positive finite host seconds.")
    if isinstance(osd_settle_seconds, bool) or not math.isfinite(osd_settle_seconds) or osd_settle_seconds < 0:
        raise ValueError("Overlay settling must be nonnegative host seconds.")
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise ValueError("The dance input offset must be whole emulator frames.")
    acting = stage6_act.acting_plan(host_timeout=stage_timeout, round_seconds=acting_round_seconds,
                                    final_seconds=acting_final_seconds, uncapped=True, source_slot=ACTING_RETRY_SLOT)
    dressing = stage4_dress.dress_plan(host_timeout=stage_timeout, from_stage3=True,
        dress_grid=dress_grid, dress_seconds=dress_seconds, pickup_frames=dress_pickup_frames,
        drag_frames=dress_drag_frames, drag_release_frames=dress_release_frames,
        page_wait_frames=dress_page_wait_frames)
    dressing["automatic_dressing"]["frame_limit"] = False
    facts = {1: "Reviewed native contest-hall overworld pixels.",
        2: "Validated first-party ribbon flags, ordered Items pocket, and required held-item result.",
        3: "Dress-up Finish controls against the reviewed native reference.",
        4: "Zero-frame Stage 3 handoff, fresh accessory grid/rank marker, counted dressing deadline, and a readable green Dance 1 demo pointer.",
        5: "Fixed category/rank metrics and cue spacing, early-targeted midpoints, non-fatal cue/input skips, one first-button feedback result, six counted responses, input-free lead dances, and the scripted B transition.",
        6: "Counted direct-entry preparation or one input-free host second after an Acting-entry state reload, confirmed native pause, all 15 empty star slots and four move categories matched in the first capture, four fresh judge readings, overworld, and target ribbon."}
    return {"game": "CPUJ", "debug": debug, "true_retry": true_retry,
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
        "dance_timings": all_fixed_timings(), "dance_rank_selection": "current accepted contest target.category and target.rank",
        "dance_midpoint_policy": "target x=127 with floored travel; correct early captures input-free, re-anchor at fresh midpoints, and skip unusable inputs locally",
        "dance_feedback_policy": "check only Dance 1's first input with bounded recovery; report failure, unreadable/skipped feedback, or consecutive Good contests from the second onward and continue; non-Good results reset the streak",
        "dance_failure_policy": "account for skipped inputs and continue through the counted response endpoints and Acting handoff",
        "dressing": dressing["automatic_dressing"], "stage4_entry_mode": dressing["entry_mode"],
        "stage4_entry_render_frames": dressing["entry_render_frames"],
        "stage4_transition": dressing["timing"],
        "stage6_retry_render_host_seconds": acting["retry_render_host_seconds"],
        "stage6_direct_render_frames": acting["handoff_render_frames"],
        "acting_round_game_seconds": acting_round_seconds,
        "acting_final_game_seconds": acting_final_seconds,
        "acting_wait_emulator_frames": [item["mash_frames"] for item in acting["rounds"]],
        "acting_move_policy": acting["move_policy"], "acting_move_category_read_policy": acting["move_category_read_policy"],
        "acting_move_navigation": acting["move_navigation"],
        "target_selection": "first missing first-party Sinnoh contest ribbon; select the next missing ribbon after each win",
        "sections": [{"stage": stage, "callable": f"contest.sections.{sections.RUNNERS[stage].__name__}",
                      "reused_stage": str(Path(module.__file__).relative_to(ROOT)),
                      "checkpoint_slot": ACTING_RETRY_SLOT if stage == 5 else stage if debug == 1 and stage in (2, 6) else None,
                      "checkpoint_facts": facts[stage]} for stage, module in STAGES.items()],
        "ribbon_miss": {"source": "stage_6_overworld" if true_retry else f"accepted_slot_{ACTING_RETRY_SLOT}",
                        "load_slot": None if true_retry else ACTING_RETRY_SLOT, "requires": "acknowledged_stage_6_ribbon_miss",
                        "paired_context": "current ribbons and Bag opening/cursor" if true_retry else "target, prior ribbons, Bag opening/cursor, and Acting-entry tracking",
                        "stages": [2, 3, 4, 5, 6] if true_retry else [6]},
        "ribbon_success": {"source": "stage_6_overworld", "stages": [2, 3, 4, 5, 6],
                           "preserve": "acquired ribbons, Bag opening state, and Bag cursor"},
        "handoff_policy": "all stages continue from the current accepted paused state through the persistent dispatcher; zero success-path loads or Lua restarts",
        "checkpoint_policy": "pause, fresh zero-frame Lua checks, policy-selected state save, final log acknowledgement",
        "save_policy": {"screen_checks": "log_only", "memory_check_slots": [2, 6] if debug == 1 else [],
                         "required_retry_slot": ACTING_RETRY_SLOT, "temporary_slot": DIAGNOSTIC_SLOT,
                         "routine_dance_states": False, "diagnostic_states": debug == 1},
        "transient_cleanup": {"hooks": ["fresh_run_startup", "every_acknowledged_ribbon_win"],
            "ownership": "recorded runner-owned artifacts only", "references": "durable pixel points and paired context",
            "abort_timeout": "retain current stopped evidence"},
        "unknown_screen_policy": "abort paused with evidence for later review",
        "finish": "all 20 first-party Sinnoh contest ribbons owned, or paused abort/timeout; restore normal speed"}


def receipt_path(result):
    return Path(result["checkpoint_receipt"])


def run(*, run_name, **options):
    started = time.monotonic()
    plan = chain_plan(**options)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    parent = ROOT / "tmp" / "contest"
    parent.mkdir(parents=True, exist_ok=True)
    # The OS releases this one-caller lock even if its host process exits unexpectedly.
    import msvcrt

    with (parent / "active-host.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("Another contest-chain host caller is active.") from error
        try:
            return _run(run_name, plan, started)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _run(run_name, plan, started):
    work = ROOT / "tmp" / "contest" / run_name
    if work.exists():
        raise FileExistsError("Use a fresh contest run name.")
    runtime = ContestRuntime(run_name, work, plan, started)
    session = runtime.confirm_ownership()
    candidates = inactive_runs(work.parent, current_pid=os.getpid(), check=runtime.check)
    runtime.prepare_fresh_ownership(session)
    protected = [Path(session["lua_script"])] if session and session.get("lua_script") else []
    startup_cleanup = cleanup_stale(work.parent, current_pid=os.getpid(), protected=protected, check=runtime.check, candidates=candidates)
    work.mkdir(exist_ok=False)
    (work / "checkpoints").mkdir()
    runtime.artifacts = OwnedArtifacts(work, run_name, os.getpid())
    write_json(work / "startup-cleanup.json", startup_cleanup)
    write_json(work / "run-owner.json", {"run_id": run_name, "host_pid": os.getpid(), "runtime": "pt-super-contest"})
    result = {"outcome": "fail", "run_id": run_name, "host_pid": os.getpid(), "work_dir": str(work),
        "settings": plan, "visits": [], "active_stage": None, "completed_acting_attempts": 0,
        "contest_entries": 0, "ribbon_acquired": False, "ribbons_acquired": [],
        "all_ribbons_owned": False, "owned_ribbon_count": None, "initial_owned_ribbon_count": None,
        "cleanup_manifests": [str(work / "startup-cleanup.json")]}
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

    def visit(stage, *, retry_slot=None, **inputs):
        counts[stage] += 1
        folder = runtime.begin_visit(stage, counts[stage])
        item = {"stage": stage, "visit": counts[stage], "run_id": run_name, "work_dir": str(folder),
            "outcome": "running", "host_budget_seconds": runtime.stage_deadline - runtime.stage_started}
        result["active_stage"] = stage
        result["visits"].append(item)
        record("stage_start", stage=stage, visit=counts[stage], host_budget_seconds=item["host_budget_seconds"])
        child = None
        try:
            if retry_slot is not None:
                runtime.retry(retry_slot)
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
            item.update(outcome="timeout" if isinstance(error, TimeoutError) else "fail",
                        error=str(error) or type(error).__name__)
            stopped = {**(child or {}), "stage": stage, "run_id": run_name, "work_dir": str(folder),
                "outcome": item["outcome"], "error": item["error"]}
            runtime.json("runner-result.json", stopped)
            record("stage_end", stage=stage, visit=counts[stage], outcome=item["outcome"], error=item["error"])
            raise
        finally:
            item["host_elapsed_seconds"] = time.monotonic() - runtime.stage_started
            runtime.artifacts.register_tree(folder)
        runtime.controller.live_deadline = runtime.total_deadline
        record("stage_end", stage=stage, visit=counts[stage], outcome=child["outcome"], frame=child.get("frame"))
        return child

    try:
        record("ownership")
        runtime.setup()
        result["emulator_pid"] = runtime.controller.session()["pid"]
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
            acting_retry = None
            while True:
                acted = visit(6, retry_slot=acting_retry, attempt=result["completed_acting_attempts"] + 1)
                acting_retry = None
                record("ribbon_result", acquired=acted["ribbon_acquired"], true_retry=plan["true_retry"],
                    completed_acting_attempts=result["completed_acting_attempts"],
                    owned_ribbon_count=result["owned_ribbon_count"], ribbons_acquired=len(result["ribbons_acquired"]))
                if acted["ribbon_acquired"]:
                    write_json(work / "collection-context.json", {"source_context": checkpoint_identity(runtime.boundary["checkpoint"]),
                        "source_checkpoint": runtime.boundary["receipt"], "save_state": runtime.boundary["save_state"],
                        "next_target": runtime.party.get("next_target"), "party": runtime.party,
                        "bag_context": runtime.bag_context, "ribbons_acquired": result["ribbons_acquired"]})
                    for slot, backup in runtime.originals.items():
                        if slot != plan["save_policy"]["temporary_slot"] and backup is not None and slot in runtime.accepted_slots:
                            runtime.artifacts.unprotect(backup)
                    manifest = runtime.artifacts.cleanup(f"win-{len(result['ribbons_acquired']):02d}", check=runtime.check)
                    result["cleanup_manifests"].append(str(manifest))
                    record("ribbon_cleanup", manifest=str(manifest), next_target=runtime.party.get("next_target"))
                    break
                if plan["true_retry"]:
                    record("retry_contest", source="stage_6_overworld", source_checkpoint=result["last_checkpoint"],
                        stages=[2, 3, 4, 5, 6], category=target["category"], rank=target["rank"], bag_context=dict(runtime.bag_context))
                    break
                acting_retry = ACTING_RETRY_SLOT
                record("retry_acting", source_slot=ACTING_RETRY_SLOT, stages=[6])
            if result["all_ribbons_owned"]:
                result.update(outcome="pass", finish_reason="all_ribbons_owned")
                break
            if result["ribbon_acquired"]:
                record("next_ribbon", source_checkpoint=result["last_checkpoint"], stages=[2, 3, 4, 5, 6],
                    bag_context=result["bag_context"])
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        if isinstance(error, TimeoutError) or time.monotonic() >= runtime.total_deadline:
            result["outcome"] = "timeout"
        elif result["outcome"] != "timeout":
            result["outcome"] = "fail"
        result["error"] = str(error) or type(error).__name__
        record("abort", outcome=result["outcome"], stage=result["active_stage"], error=result["error"])
    finally:
        final = runtime.finalize(error=result.get("error"))
        result.update({key: value for key, value in final.items() if key != "errors"})
        runtime.artifacts.register_tree(runtime.active_work)
        if final["errors"]:
            result["outcome"] = "fail"
            result.setdefault("error", "; ".join(final["errors"]))
        elif result["outcome"] == "pass" and time.monotonic() >= runtime.total_deadline:
            result.update(outcome="timeout", error="The whole-run host deadline expired during finalization.")
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


def check():
    from tests.support.lua51 import Lua51, literal

    from contest import artifacts, dance_timings, rank_dance, runtime

    for module in (*STAGES.values(), sections, artifacts, dance_timings, rank_dance, runtime):
        ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    ast.parse(Path(__file__).read_text(encoding="utf-8"))
    lua = Lua51()
    try:
        names = ("super_contest.lua", "sections.lua", "acting_plan.lua", "stage1_load.lua", "stage2_prep.lua", "bag_contest.lua", "party_contest.lua",
                   "stage3_enter.lua", "stage4_dress.lua", "dress_pixels.lua", "dance_master.lua", "rank_dance.lua", "dance_pixels.lua", "stage6_act.lua", "screen_checks.lua")
        code = "; ".join(f"assert(loadfile({literal((HERE / name).as_posix())}))" for name in names)
        print(lua.run(code + "; return 'Persistent contest Python/Lua syntax OK'"))
    finally:
        lua.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--background", action="store_true")
    mode.add_argument("--wait", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--dress-grid", type=int, choices=(6, 7), default=dress_grid)
    parser.add_argument("--dress-seconds", type=float, default=dress_seconds, help="Automatic dressing window in emulator game seconds.")
    parser.add_argument("--dress-pickup-frames", type=int, default=dress_pickup_frames)
    parser.add_argument("--dress-drag-frames", type=int, default=dress_drag_frames)
    parser.add_argument("--dress-release-frames", type=int, default=dress_release_frames)
    parser.add_argument("--dress-page-wait-frames", type=int, default=dress_page_wait_frames)
    parser.add_argument("--debug", type=int, choices=(0, 1), default=debug,
                        help="Enable memory-check and diagnostic states; the Acting-entry state always uses the final slot (10).")
    parser.add_argument("--true-retry", type=int, choices=(0, 1), default=true_retry)
    parser.add_argument("--total-timeout", type=float, default=total_timeout_seconds)
    parser.add_argument("--stage-timeout", type=float, default=stage_timeout_seconds)
    parser.add_argument("--osd-settle-seconds", type=float, default=OSD_SETTLE_SECONDS)
    parser.add_argument("--offset-frames", type=int, default=rank_dance.DEFAULT_OFFSET_FRAMES)
    parser.add_argument("--acting-round-seconds", type=float, default=acting_round_seconds)
    parser.add_argument("--acting-final-seconds", type=float, default=acting_final_seconds)
    args = parser.parse_args()
    if args.check:
        check()
        return 0
    options = {"debug": args.debug, "true_retry": args.true_retry,
        "total_timeout": args.total_timeout, "stage_timeout": args.stage_timeout,
        "osd_settle_seconds": args.osd_settle_seconds, "offset_frames": args.offset_frames,
        "acting_round_seconds": args.acting_round_seconds, "acting_final_seconds": args.acting_final_seconds,
        "dress_grid": args.dress_grid, "dress_seconds": args.dress_seconds,
        "dress_pickup_frames": args.dress_pickup_frames, "dress_drag_frames": args.dress_drag_frames,
        "dress_release_frames": args.dress_release_frames, "dress_page_wait_frames": args.dress_page_wait_frames}
    try:
        if args.plan_only:
            print(json.dumps(chain_plan(**options), indent=2))
            return 0
        if not args.run_name:
            parser.error("Supply a fresh --run-name.")
        if args.wait:
            return wait(args.run_name)
        if args.background:
            return background(args.run_name, options)
        result = run(run_name=args.run_name, **options)
        return 0 if result["outcome"] == "pass" else 1
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
