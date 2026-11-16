"""Prepare one ribbon target with the passed local scarf test's inputs and reads."""

from __future__ import annotations

import argparse
import ast
import filecmp
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contest.dance_master import attach, lua_value
from contest.runtime import ROM, TimedController, TimedSlots, setup_speed, finish_speed
from contest.stage2_bag import Stage2BagState
from contest.stage3_enter import accepted_source
from host.checkpoints import CheckpointClient, CheckpointStopped, write_json
from host.controller import ControllerConfig
from host.emulator import DeSmuME
from host.paths import ROOT
from contest.timing_profiles import load_profile, section


HERE = Path(__file__).resolve().parent
INPUT_SLOT, OUTPUT_SLOT = 10, 2
SCARVES = {260: "Red Scarf", 261: "Blue Scarf", 262: "Pink Scarf", 263: "Green Scarf", 264: "Yellow Scarf"}
BAG_DOWN_RELEASE_FRAMES = section(None, "stage2_preparation")["bag_down_release_frames"]


def prep_plan(snapshot, *, bag_state, cursor=1, target=None, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    values = section(profile, "stage2_preparation")
    party, entries = snapshot["party"], snapshot["bag"]["items"]
    target = party.get("next_target") if target is None else dict(target)
    required = target["required_scarf_id"] if target else None
    scarf = next((item for item in entries if item["item_id"] == required), None)
    held = party["held_item_id"]
    inputs = []
    after = cursor
    mode = "all_ribbons_owned" if target is None else "correct_scarf_held" if held == required else "scarf_unavailable"
    if target is not None and held != required and scarf is not None:
        mode = "equip_scarf"
        delta = scarf["position"] - cursor

        def press(button, release=None):
            if release is None:
                release = values["bag_down_release_frames"] if button == "down" else values["menu_release_frames"]
            return f"{button}:{values['menu_hold_frames']}:{release}"

        inputs = [press(button) for button in bag_state.opening_buttons()]
        inputs += [press("down" if delta > 0 else "up")] * abs(delta)
        inputs += [press("A"), press("A", values["give_confirmation_release_frames"])]
        inputs += [press("A")] * (4 if held else 2) + [press("B")] * 2
        after = scarf["position"]
        if scarf["quantity"] == 1 and after == len(entries):
            old_stack = any(item["item_id"] == held for item in entries)
            if not held or old_stack:
                after = max(1, after - 1)
    return {"stage": 2, "timing_profile_source": profile["source"], "mode": mode, "target": target, "source_species_id": party["species_id"],
        "expected_item_id": required if inputs else held, "required_scarf_id": required,
        "bag_opened_before": bag_state.opened_once, "cursor_before": cursor,
        "target_position": scarf["position"] if scarf else None,
        "target_quantity": scarf["quantity"] if scarf else None,
        "bag_context": {"opened_once": bag_state.opened_once or bool(inputs), "cursor_position": after},
        "inputs": inputs,
        "emulator_frames": sum(sum(map(int, step.split(":")[1:])) for step in inputs),
        "checkpoint_slot": OUTPUT_SLOT, "memory_proven_phase": False}


def run(*, run_name, source_checkpoint, host_timeout, deadline=None,
        keep_uncapped=False, bag_state=None, cursor=1, timing_profile=None):
    started = time.monotonic()
    if isinstance(host_timeout, bool) or not math.isfinite(host_timeout) or host_timeout <= 0:
        raise ValueError("The Stage 2 host timeout must be positive seconds.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    receipt_path, source, previous = accepted_source(source_checkpoint)
    timing_profile = load_profile() if timing_profile is None else timing_profile
    bag_state = bag_state if bag_state is not None else Stage2BagState()
    work = ROOT / "tmp" / "contest" / run_name
    work.mkdir(parents=True, exist_ok=False)
    (work / "checkpoints").mkdir()
    shutil.copy2(receipt_path, work / "source-checkpoint.json")
    shutil.copy2(source, work / "source-state.ds0")
    controller = TimedController(ControllerConfig(rom=ROM))
    controller.live_deadline = min(started + host_timeout, deadline if deadline is not None else float("inf"))
    controller.timeouts_enabled = False
    emulator = DeSmuME(controller)
    emulator.states = TimedSlots(controller)
    client = CheckpointClient(emulator, work, run_name)
    result = {"outcome": "fail", "stage": 2, "run_id": run_name, "host_pid": os.getpid(),
        "work_dir": str(work), "stage2_accepted": False, "host_timeout_seconds": host_timeout,
        "source_checkpoint": str(receipt_path), "source_frame": previous["frame"]}
    backups, touched = {}, set()
    owned = attached = False

    def record(phase, **details):
        event = {"phase": phase, "host_elapsed_seconds": round(time.monotonic() - started, 3), **details}
        write_json(work / "runner-status.json", event)
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    try:
        controller.remaining()
        record("setup")
        owners = set(controller.emulator_process_ids()) | set(controller.emulator_window_process_ids())
        if owners:
            session = controller.session()
            if owners != {session["pid"]} or Path(session["rom"]).resolve() != ROM.resolve():
                raise RuntimeError("Stage 2 requires the sole controlled Platinum emulator.")
        else:
            controller.launch(ROM)
        owned = True
        controller.set_pause(True)
        emulator.stop_movie()
        for slot in (INPUT_SLOT, OUTPUT_SLOT):
            path = emulator.states.path(slot)
            backups[slot] = work / f"previous-slot-{slot}.ds{slot % 10}" if path.exists() else None
            if backups[slot] is not None:
                shutil.copy2(path, backups[slot])
        result["speed_setup"] = setup_speed(emulator, inherited_uncapped=keep_uncapped)
        attach(controller, HERE / "stage2_prep.lua", bridge="pt-contest-stage2")
        attached = True
        touched.add(INPUT_SLOT)
        shutil.copy2(source, emulator.states.path(INPUT_SLOT))
        emulator.load_state(INPUT_SLOT, refresh=False)
        initial = client.command("inspect")
        snapshot = initial["observation"]
        write_json(work / "source-snapshot.json", initial)
        if initial["frame"] != previous["frame"] or snapshot["party"]["is_egg"]:
            raise RuntimeError(f"Stage 2 source mismatch: expected frame {previous['frame']}; actual {initial['frame']}; "
                               f"first-party eligible: {not snapshot['party']['is_egg']}.")
        plan = prep_plan(snapshot, bag_state=bag_state, cursor=cursor, timing_profile=timing_profile)
        result.update(target=plan["target"], mode=plan["mode"], bag_context=plan["bag_context"])
        write_json(work / "runner-setup.json", {**result, **plan, "shared_host_deadline": deadline})
        plan_path = work / "plan.lua"
        plan_path.write_text("return " + lua_value(plan) + "\n", encoding="utf-8", newline="\n")
        client.command("init", run_name, (work / "checkpoint-events.jsonl").as_posix(), plan_path.as_posix())
        result["speed_setup"].update(emulator.set_frame_limit(False))
        record("preparing", mode=plan["mode"], target=plan["target"], emulator_frames=plan["emulator_frames"])
        if plan["inputs"]:
            inputs = emulator.command("combo", "inputs", *plan["inputs"], timeout=None, deadline=controller.live_deadline)
            write_json(work / "input-result.json", inputs)
        pending = client.command("visit")
        actual = pending["checkpoint"]["snapshot"]["party"]
        if actual["held_item_id"] != plan["expected_item_id"]:
            expected_name = SCARVES.get(plan["expected_item_id"], f"Item {plan['expected_item_id']}")
            raise RuntimeError(f"Scarf equip failed: expected {expected_name}; actual {actual['held_item']}.")
        touched.add(OUTPUT_SLOT)
        accepted = client.acknowledge(pending, slot=OUTPUT_SLOT,
            previous_state=str(backups[OUTPUT_SLOT]) if backups[OUTPUT_SLOT] else None)
        controller.remaining()
        if plan["inputs"]:
            bag_state.record_opened()
        result.update(outcome="pass", stage2_accepted=True, checkpoint=accepted["checkpoint"],
            save_state=accepted["host_savestate"], frame=accepted["frame"], source_party=snapshot["party"],
            final_party=actual, all_ribbons_owned=actual["all_ribbons_owned"])
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        result["outcome"] = "timeout" if isinstance(error, TimeoutError) or time.monotonic() >= controller.live_deadline else "fail"
        result["error"] = str(error) or type(error).__name__
        if isinstance(error, CheckpointStopped):
            result["failed_checkpoint"] = error.record
        record("abort", outcome=result["outcome"], error=result["error"])
    finally:
        controller.live_deadline, controller.timeouts_enabled = None, True
        cleanup = []
        if owned:
            try:
                controller.set_pause(True)
                if attached and result["outcome"] != "pass":
                    touched.add(OUTPUT_SLOT)
                    result["stopped_evidence"] = client.stop(result["error"], slot=OUTPUT_SLOT)
                    controller.screenshot(work / "failure.png")
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
            for name, action in (("movie_finish", emulator.stop_movie), ("speed_finish",
                    lambda: finish_speed(emulator, handoff=keep_uncapped and result["outcome"] == "pass"))):
                try:
                    result[name] = action()
                except (Exception, SystemExit) as error:
                    cleanup.append(str(error))
            try:
                result["emulator_paused"] = controller.paused(controller.session())
                result["inputs_released"] = attached and controller.status().get("buttons") == ""
                if not result["emulator_paused"] or (attached and not result["inputs_released"]):
                    raise RuntimeError("Stage 2 finalization did not leave paused, released inputs.")
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
        for slot in sorted(touched, reverse=True):
            if slot == OUTPUT_SLOT and result["outcome"] == "pass" and not cleanup:
                continue
            try:
                path = emulator.states.path(slot)
                if backups[slot] is not None:
                    shutil.copy2(backups[slot], path)
                    if not filecmp.cmp(backups[slot], path, shallow=False):
                        raise RuntimeError(f"Slot {slot} restoration differs from its backup.")
                else:
                    path.unlink(missing_ok=True)
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
        if cleanup:
            result.update(outcome="fail", stage2_accepted=False)
            result.setdefault("error", "; ".join(cleanup))
        result["cleanup_errors"] = cleanup
        result["total_host_seconds"] = time.monotonic() - started
        write_json(work / "runner-result.json", result)
        record("complete", outcome=result["outcome"], emulator_paused=result.get("emulator_paused"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--source-checkpoint")
    parser.add_argument("--host-timeout", type=float)
    args = parser.parse_args()
    if args.check:
        from tests.support.lua51 import Lua51, literal

        ast.parse(Path(__file__).read_text(encoding="utf-8"))
        lua = Lua51()
        try:
            code = "; ".join(f"assert(loadfile({literal((HERE / name).as_posix())}))"
                             for name in ("bag_contest.lua", "stage2_prep.lua"))
            print(lua.run(code + "; return 'Stage 2 Python/Lua syntax OK'"))
        finally:
            lua.close()
        return 0
    if not args.run_name or not args.source_checkpoint or args.host_timeout is None:
        parser.error("Execution requires --run-name, --source-checkpoint, and --host-timeout.")
    result = run(run_name=args.run_name, source_checkpoint=args.source_checkpoint, host_timeout=args.host_timeout)
    return 0 if result["outcome"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
