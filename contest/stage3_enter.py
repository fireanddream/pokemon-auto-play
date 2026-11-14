"""Enter the selected CPUJ contest and retain a reviewed dress-up checkpoint."""

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
import struct
import subprocess
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contest.runtime import ROM, TimedController, TimedSlots, automatic_review, setup_speed, finish_speed
from host.checkpoints import CheckpointClient, CheckpointStopped, write_json
from host.controller import ControllerConfig
from host.emulator import DeSmuME
from host.paths import ROOT, project_path
from contest.inputs import MASH_HOLD_FRAMES, MASH_RELEASE_FRAMES, NORMAL_HOLD_FRAMES, NORMAL_RELEASE_FRAMES, taps


HERE = Path(__file__).resolve().parent
CATEGORIES = ("Cool", "Beauty", "Cute", "Smart", "Tough")
RANKS = ("Normal", "Super", "Hyper", "Master")
INPUT_SLOT, OUTPUT_SLOT = 10, 3
INTRO_SECONDS = 50


class Stage3Controller(TimedController):
    def remaining(self):
        if self.live_deadline is not None and time.monotonic() >= self.live_deadline:
            raise TimeoutError("The Stage 3 host deadline expired.")
        return super().remaining()


def entry_plan(category, rank, *, host_timeout, hold_frames=NORMAL_HOLD_FRAMES,
               release_frames=NORMAL_RELEASE_FRAMES, intro_seconds=INTRO_SECONDS, timing_probe=False,
               enrollment_check=False):
    if category not in CATEGORIES or rank not in RANKS:
        raise ValueError("Supply a supported contest category and rank.")
    if not math.isfinite(host_timeout) or host_timeout <= 0:
        raise ValueError("The host timeout must be positive seconds.")
    if not math.isfinite(intro_seconds) or intro_seconds <= 0:
        raise ValueError("The introduction duration must be positive game seconds.")
    if timing_probe and intro_seconds < 40:
        raise ValueError("The timing probe begins taking screenshots at 40 game seconds.")
    if timing_probe and enrollment_check:
        raise ValueError("Choose timing measurement or enrollment verification for this run.")
    menu = ["A"] * 4 + ["down"] * RANKS.index(rank) + ["A"]
    menu += ["down"] * CATEGORIES.index(category) + ["A"] * 4
    steps = taps(menu, hold_frames, release_frames)
    intro_frames = round(intro_seconds * 67027965 / 1120380)
    if intro_frames < 1:
        raise ValueError("The introduction must cover at least one emulator frame.")
    cycle = MASH_HOLD_FRAMES + MASH_RELEASE_FRAMES
    count, tail = divmod(intro_frames, cycle)
    steps += taps(["A"] * count, MASH_HOLD_FRAMES, MASH_RELEASE_FRAMES) if count else []
    if tail > MASH_HOLD_FRAMES:
        steps += taps("A", MASH_HOLD_FRAMES, tail - MASH_HOLD_FRAMES)
    elif tail:
        steps.append(f"wait:{tail}")
    sample_seconds = list(range(40, math.ceil(intro_seconds), 5)) if timing_probe else []
    if timing_probe and (not sample_seconds or sample_seconds[-1] != intro_seconds):
        sample_seconds.append(intro_seconds)
    tap_frames = hold_frames + release_frames
    rank_frame = (4 + RANKS.index(rank)) * tap_frames
    category_frame = rank_frame + (1 + CATEGORIES.index(category)) * tap_frames
    total_frames = len(menu) * tap_frames + intro_frames
    enrollment_samples = [
        {"label": "rank_selection", "expected": rank, "entry_frame_offset": rank_frame},
        {"label": "category_selection", "expected": category, "entry_frame_offset": category_frame},
        {"label": "party_selection", "expected": "party slot 1", "entry_frame_offset": category_frame + 2 * tap_frames},
        {"label": "dress_up_entry", "expected": "dress-up controls", "entry_frame_offset": total_frames},
    ] if enrollment_check else []
    return {"stage": 3, "target": {"category": category, "rank": rank, "party_slot": 1},
        "host_timeout_seconds": host_timeout, "attempts": 1, "uncapped": True,
        "timing_probe": timing_probe,
        "enrollment_check": enrollment_check, "enrollment_samples": enrollment_samples,
        "timing_samples": [{"intro_game_seconds": seconds,
                            "intro_frame_offset": round(seconds * 67027965 / 1120380)}
                           for seconds in sample_seconds],
        "timing": {"hold_frames": hold_frames, "release_frames": release_frames,
            "mash_hold_frames": MASH_HOLD_FRAMES, "mash_release_frames": MASH_RELEASE_FRAMES,
            "intro_game_seconds": intro_seconds, "intro_frames": intro_frames,
            "frame_conversion": "nearest frame at 67027965 / 1120380 frames per second"},
        "rank_down_presses": RANKS.index(rank), "category_down_presses": CATEGORIES.index(category),
        "inputs": steps, "menu_frames": len(menu) * (hold_frames + release_frames),
        "total_emulator_frames": total_frames,
        "checkpoint_slot": OUTPUT_SLOT,
        "acceptance": "All requested timing images retained." if timing_probe else "Dress-up entry for the selected contest.",
        "review_source": "post_run_visual_review" if timing_probe else "visual_review", "memory_proven_phase": False}


def preview_plan(category, rank, **options):
    if (category is None) != (rank is None):
        raise ValueError("Supply both category and rank, or omit both for first-party ribbon selection.")
    plan = entry_plan(category or CATEGORIES[0], rank or RANKS[0], **options)
    if category is not None:
        return plan
    return {**{key: plan[key] for key in ("stage", "host_timeout_seconds", "timing", "timing_probe", "enrollment_check")},
            "target": None, "target_selection": "fresh first-party Sinnoh contest-ribbon flags",
            "category_order": CATEGORIES, "rank_order": RANKS, "input_plan_pending_source_read": True}


def accepted_source(path):
    receipt_path = project_path(path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    completed = receipt.get("completed", {})
    saved = completed.get("savestate", {})
    retained = receipt.get("host_savestate", {})
    if completed.get("status") != "complete" or completed.get("proceed") is not True:
        raise ValueError("The source must have an acknowledged successful checkpoint.")
    if any(saved.get(key) != completed.get(key) for key in ("run_id", "visit", "frame", "epoch")):
        raise ValueError("The source save receipt does not match its checkpoint.")
    source = project_path(retained["retained_path"])
    if project_path(saved["path"]) != source or retained.get("emulator_paused") is not True:
        raise ValueError("The source checkpoint does not identify its retained paused state.")
    if completed.get("snapshot", {}).get("game", {}).get("code") != "CPUJ":
        raise ValueError("The source checkpoint must belong to Japanese Platinum.")
    TimedSlots.validate_header(source, retained["slot"])
    return receipt_path, source, completed


def attach(controller):
    dialog = controller.session().get("lua_window", 0)
    if controller.USER32.IsWindow(dialog):
        stop = controller.USER32.GetDlgItem(dialog, 312)
        if stop and controller.USER32.IsWindowEnabled(stop):
            controller.send(dialog, 0x111, 312, stop)
            while controller.USER32.IsWindowEnabled(stop):
                controller.remaining()
                time.sleep(0.01)
    controller.set_pause(True)
    started = time.time_ns()
    controller.load_lua(HERE / "stage3_enter.lua")
    dialog = controller.session()["lua_window"]
    stop = controller.USER32.GetDlgItem(dialog, 312)
    if not controller.USER32.IsWindowEnabled(stop):
        controller.send(dialog, 0x111, 311, controller.USER32.GetDlgItem(dialog, 311))
    while True:
        controller.remaining()
        try:
            fresh = controller.STATUS.stat().st_mtime_ns >= started
            status = json.loads(controller.STATUS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fresh, status = False, {}
        if fresh and status.get("bridge") == "pt-contest-stage3" and status.get("checkpoint_protocol") == 1:
            controller.set_pause(True)
            return
        if not controller.USER32.IsWindowEnabled(stop):
            raise RuntimeError("The Stage 3 Lua bridge stopped before reporting ready.")
        time.sleep(0.02)


def await_review(controller, request):
    path = Path(request["review_path"])
    while not path.is_file():
        controller.remaining()
        if not controller.paused(controller.session()):
            controller.set_pause(True)
            raise RuntimeError("Native pause was lost during dress-up screenshot review.")
        time.sleep(0.2)
    controller.remaining()
    review = json.loads(path.read_text(encoding="utf-8"))
    if any(review.get(key) != request[key] for key in ("run_id", "frame", "epoch")):
        raise ValueError("The review belongs to a different paused boundary.")
    if review.get("verdict") not in {"pass", "fail"} or review.get("source") not in {"visual_review", "local_reference_pixels"}:
        raise ValueError("Supply a pass/fail verdict with its actual visual source.")
    note = review.get("note")
    if not isinstance(note, str) or not note.strip() or any(char in note for char in "|\r\n"):
        raise ValueError("The review requires a single-line visual explanation.")
    if request.get("enrollment_check"):
        if any(key not in review for key in ("selected_rank", "selected_category", "selected_party_slot")):
            raise ValueError("The enrollment review needs the observed rank, category, and party slot.")
    return review


def retain_images(controller, work, planned, sampling):
    from PIL import Image, ImageDraw

    observed = sampling["samples"]
    kind = sampling["kind"]
    offset_key = "entry_frame_offset" if kind == "enrollment" else "intro_frame_offset"
    if len(observed) != len(planned):
        raise RuntimeError("The timing probe did not capture every requested image.")
    sheet = Image.new("RGB", (3 * 256, math.ceil(len(observed) / 3) * 408), "white")
    draw = ImageDraw.Draw(sheet)
    manifest = []
    for index, (expected, actual) in enumerate(zip(planned, observed)):
        controller.remaining()
        if (actual[offset_key] != expected[offset_key]
                or actual["frame"] != sampling["start_frame"] + expected[offset_key]):
            raise RuntimeError("A screenshot does not match its requested emulator frame.")
        path = project_path(actual["path"])
        if path != work / "samples" / f"sample-{index + 1:02d}.gd":
            raise RuntimeError("A timing image is outside this run's sample sequence.")
        raw = path.read_bytes()
        if len(raw) < 11:
            raise ValueError("Truncated timing screenshot.")
        magic, width, height = struct.unpack_from(">HHH", raw)
        if magic != 65534 or raw[6] != 1 or (width, height) != (256, 384) or len(raw) != 11 + width * height * 4:
            raise ValueError("Expected a native stacked-screen GD image.")
        channels = Image.frombytes("RGBA", (width, height), raw[11:]).split()
        image = Image.merge("RGB", channels[1:])
        png = path.with_suffix(".png")
        image.save(png)
        item = {**expected, **actual, "image": str(png),
                "actual_game_seconds": actual[offset_key] * 1120380 / 67027965}
        manifest.append(item)
        x, y = index % 3 * 256, index // 3 * 408
        label = expected["label"] if kind == "enrollment" else f"{expected['intro_game_seconds']:g} s"
        draw.text((x + 5, y + 5), f"{label} / frame {actual['frame']}", fill="black")
        sheet.paste(image, (x, y + 24))
    sheet_path = work / f"{kind}-contact-sheet.png"
    sheet.save(sheet_path)
    write_json(work / f"{kind}-samples.json", {"time_origin": "entry_start" if kind == "enrollment" else "intro_start",
                                            "start_frame": sampling["start_frame"], "samples": manifest})
    return manifest, str(sheet_path)


def run(*, run_name, source_checkpoint, category, rank, host_timeout, ribbon_test=None, deadline=None,
        auto_review=False, keep_uncapped=False, **timing):
    started = time.monotonic()
    plan = preview_plan(category, rank, host_timeout=host_timeout, **timing)
    automatic_target = category is None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    receipt_path, source, previous = accepted_source(source_checkpoint)
    test_manifest = None
    if ribbon_test is not None:
        if not plan["enrollment_check"]:
            raise ValueError("A modified ribbon state requires enrollment verification.")
        test_manifest = json.loads(project_path(ribbon_test).read_text(encoding="utf-8"))
        if (project_path(test_manifest["base_checkpoint"]) != receipt_path
                or project_path(test_manifest["base_state"]) != source or test_manifest.get("test_only") is not True):
            raise ValueError("The ribbon-test state does not identify this accepted base checkpoint.")
        source = project_path(test_manifest["state"])
        TimedSlots.validate_header(source, INPUT_SLOT)
    work = ROOT / "tmp" / "contest" / run_name
    work.mkdir(parents=True, exist_ok=False)
    (work / "checkpoints").mkdir()
    if plan["timing_probe"] or plan["enrollment_check"]:
        (work / "samples").mkdir()
    shutil.copy2(receipt_path, work / "source-checkpoint.json")
    shutil.copy2(source, work / "source-state.ds0")
    source_context = {key: previous[key] for key in ("checkpoint", "run_id", "visit", "frame", "epoch")}
    setup = {**plan, "run_id": run_name, "host_pid": os.getpid(), "source_checkpoint": str(receipt_path),
             "source_state": str(source), "source_context": source_context, "work_dir": str(work),
             "source_kind": "derived_ribbon_test" if test_manifest else "accepted_checkpoint",
             "ribbon_test": test_manifest}
    write_json(work / "runner-setup.json", setup)
    result = {"outcome": "fail", "stage": 3, "run_id": run_name, "work_dir": str(work),
              "target": plan["target"], "host_timeout_seconds": host_timeout, "source_context": source_context,
              "timing_probe": plan["timing_probe"], "enrollment_check": plan["enrollment_check"], "stage3_accepted": False}
    if test_manifest:
        result["ribbon_test"] = test_manifest
    controller = Stage3Controller(ControllerConfig(rom=ROM))
    emulator = DeSmuME(controller)
    emulator.states = TimedSlots(controller)
    client = CheckpointClient(emulator, work, run_name)
    backups, touched = {}, set()
    owned = attached = False
    controller.live_deadline = started + host_timeout
    if deadline is not None:
        controller.live_deadline = min(controller.live_deadline, deadline)

    def record(phase, **details):
        event = {"phase": phase, "host_elapsed_seconds": round(time.monotonic() - started, 3), **details}
        write_json(work / "runner-status.json", event)
        with (work / "runner-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        print(json.dumps(event), flush=True)

    try:
        record("setup", target=plan["target"])
        owners = set(controller.emulator_process_ids()) | set(controller.emulator_window_process_ids())
        if owners:
            session = controller.session()
            if owners != {session["pid"]} or project_path(session["rom"]) != ROM.resolve():
                raise RuntimeError("Stage 3 requires the sole controlled Platinum emulator.")
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
        attach(controller)
        attached = True
        touched.add(INPUT_SLOT)
        shutil.copy2(work / "source-state.ds0", emulator.states.path(INPUT_SLOT))
        emulator.load_state(INPUT_SLOT, refresh=False)
        initial = client.command("inspect")
        if initial["frame"] != previous["frame"] or not initial["observation"]["game"]["supported"]:
            raise RuntimeError("The loaded source does not match the accepted CPUJ checkpoint.")
        write_json(work / "source-snapshot.json", initial)
        source_party = initial["observation"]["source_party"]
        result["source_party"] = source_party
        if not source_party["read_valid"] or source_party["is_egg"]:
            raise ValueError("The first party Pokemon must be readable and eligible for contest entry.")
        if source_party["all_ribbons_owned"]:
            if test_manifest:
                raise ValueError("The test expected a missing ribbon, but the reader reports all ribbons owned.")
            result.update(outcome="pass", all_ribbons_owned=True, frame=initial["frame"])
            record("all_ribbons_owned", species_id=source_party["species_id"])
            return result
        selected = source_party["next_target"]
        if test_manifest:
            expected = test_manifest["expected_target"]
            if (source_party["owned_ribbon_bits"] != test_manifest["owned_ribbon_bits"]
                    or source_party["species_id"] != test_manifest["species_id"]
                    or source_party["held_item_id"] != test_manifest["held_item_id"]
                    or (selected["category"], selected["rank"]) != (expected["category"], expected["rank"])):
                raise ValueError("The fresh first-party read does not match the prepared ribbon-test variant.")
        if automatic_target:
            category, rank = selected["category"], selected["rank"]
        if (category, rank) != (selected["category"], selected["rank"]):
            raise ValueError("The requested contest differs from the first Pokemon's next missing ribbon.")
        plan = entry_plan(category, rank, host_timeout=host_timeout, **timing)
        setup.update(plan, source_party=source_party, input_plan_pending_source_read=False)
        write_json(work / "runner-setup.json", setup)
        result["target"] = selected
        record("target_selected", species_id=source_party["species_id"], owned_ribbon_bits=source_party["owned_ribbon_bits"],
               category=category, rank=rank)
        client.command("init", run_name, (work / "checkpoint-events.jsonl").as_posix(), category, rank,
                       str(plan["enrollment_check"]).lower())
        if plan["timing_probe"]:
            client.command("sampling", (work / "samples").as_posix(), plan["menu_frames"],
                           *(sample["intro_frame_offset"] for sample in plan["timing_samples"]))
        elif plan["enrollment_check"]:
            client.command("enrollment-sampling", (work / "samples").as_posix(), 0,
                           *(sample["entry_frame_offset"] for sample in plan["enrollment_samples"]))
        result["speed_setup"].update(emulator.set_frame_limit(False))
        record("entering", start_frame=initial["frame"], emulator_frames=plan["total_emulator_frames"],
               timing_sample_seconds=[sample["intro_game_seconds"] for sample in plan["timing_samples"]])
        inputs = emulator.command("combo", "inputs", *plan["inputs"], timeout=None,
                                  deadline=controller.live_deadline)
        write_json(work / "input-result.json", inputs)
        if inputs.get("stop_reason") != "combo_complete":
            raise RuntimeError("The contest-entry input sequence did not complete.")
        capture = client.command("probe-visit" if plan["timing_probe"] else "capture")
        image = work / "stage3.png"
        controller.screenshot(image)
        shutil.copy2(controller.config.mailbox_dir / "screen.gd", work / "stage3.gd")
        result.update(frame=capture["frame"], screenshot=str(image),
                      input_frames=capture["frame"] - inputs["command_start_frame"])
        if plan["timing_probe"]:
            result["timing_samples"], result["timing_contact_sheet"] = retain_images(
                controller, work, plan["timing_samples"], capture["checkpoint"]["snapshot"]["sampling"])
            pending = capture
            record("samples_retained", count=len(result["timing_samples"]), frame=capture["frame"])
        else:
            if plan["enrollment_check"]:
                result["enrollment_samples"], result["enrollment_contact_sheet"] = retain_images(
                    controller, work, plan["enrollment_samples"], capture["observation"]["sampling"])
            request = {"run_id": run_name, "frame": capture["frame"], "epoch": capture["epoch"],
                       "image": str(image), "review_path": str(work / "review.json"), "expected": plan["acceptance"],
                       "target": result["target"], "remaining_host_seconds": controller.remaining(),
                       "enrollment_check": plan["enrollment_check"],
                        "enrollment_samples": result.get("enrollment_samples", [])}
            request["automatic_check"] = capture["observation"]["automatic_check"]
            write_json(work / "review-request.json", request)
            if auto_review and not plan["enrollment_check"]:
                automatic_review(request, request["automatic_check"])
            record("awaiting_review", **request)
            review = await_review(controller, request)
            result["screen_evidence"] = review
            enrollment = [review[key] for key in ("selected_rank", "selected_category", "selected_party_slot")] if plan["enrollment_check"] else []
            pending = client.command("visit", review["verdict"], review["source"], image.as_posix(),
                                     review["note"], review["frame"], review["epoch"], *enrollment)
        touched.add(OUTPUT_SLOT)
        accepted = client.acknowledge(pending, slot=OUTPUT_SLOT,
            previous_state=str(backups[OUTPUT_SLOT]) if backups[OUTPUT_SLOT] else None)
        result.update(checkpoint=accepted["checkpoint"], save_state=accepted["host_savestate"])
        controller.remaining()
        result["outcome"] = "pass"
        result["stage3_accepted"] = not plan["timing_probe"]
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        result["outcome"] = "timeout" if isinstance(error, TimeoutError) or time.monotonic() >= controller.live_deadline else "fail"
        result["error"] = str(error) or type(error).__name__
        if isinstance(error, CheckpointStopped):
            result["checkpoint"] = error.record
        record("abort", outcome=result["outcome"], error=result["error"])
    finally:
        result["live_host_seconds"] = time.monotonic() - started
        controller.live_deadline = None
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
                    raise RuntimeError("Stage 3 finalization did not leave paused, released inputs.")
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
        if cleanup:
            result["outcome"] = "fail"
            result.setdefault("error", "; ".join(cleanup))
        for slot in sorted(touched, reverse=True):
            if slot == OUTPUT_SLOT and result["outcome"] == "pass" and not plan["timing_probe"] and not test_manifest and not cleanup:
                continue
            try:
                path = emulator.states.path(slot)
                if backups[slot] is not None:
                    shutil.copy2(backups[slot], path)
                    if not filecmp.cmp(backups[slot], path, shallow=False):
                        raise RuntimeError(f"Slot {slot} restoration did not match its backup.")
                else:
                    path.unlink(missing_ok=True)
            except (Exception, SystemExit) as error:
                cleanup.append(str(error))
        if cleanup:
            result["outcome"] = "fail"
            result.setdefault("error", "; ".join(cleanup))
        if result["outcome"] != "pass":
            result["stage3_accepted"] = False
        result["cleanup_errors"] = cleanup
        result["total_host_seconds"] = time.monotonic() - started
        write_json(work / "runner-result.json", result)
        record("complete", outcome=result["outcome"], error=result.get("error"),
               emulator_paused=result.get("emulator_paused"), checkpoint_slot=OUTPUT_SLOT)
    return result


def background(run_name, source_checkpoint):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_name):
        raise ValueError("Use a run name containing letters, digits, underscores, or hyphens.")
    accepted_source(source_checkpoint)
    work = ROOT / "tmp" / "contest" / run_name
    if work.exists():
        raise FileExistsError(f"Use a fresh run directory: {work}")
    log = work.parent / f"{run_name}.host.log"
    arguments = ["--execute" if argument == "--background" else argument for argument in sys.argv[1:]]
    with log.open("xb") as output:
        child = subprocess.Popen([sys.executable, "-u", "-B", str(Path(__file__).resolve()), *arguments],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    # Only the detached child controls the emulator. This launcher waits for its review boundary.
    while child.poll() is None:
        request = work / "review-request.json"
        if request.is_file():
            print(json.dumps({"host_pid": child.pid, "work_dir": str(work), "log": str(log),
                              "review_request": json.loads(request.read_text(encoding="utf-8"))}, indent=2), flush=True)
            return 0
        time.sleep(0.05)
    result_path = work / "runner-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    print(json.dumps({"host_pid": child.pid, "work_dir": str(work), "log": str(log),
                      "outcome": result.get("outcome", "launch_failed"), "error": result.get("error")}), flush=True)
    return child.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--background", action="store_true", help="Launch the sole host caller and return at its review boundary.")
    parser.add_argument("--run-name")
    parser.add_argument("--source-checkpoint")
    parser.add_argument("--ribbon-test", help="Explicit test-only modified-state manifest, used with --enrollment-check.")
    parser.add_argument("--category", choices=CATEGORIES)
    parser.add_argument("--rank", choices=RANKS)
    parser.add_argument("--host-timeout", type=float)
    parser.add_argument("--hold-frames", type=int, default=NORMAL_HOLD_FRAMES)
    parser.add_argument("--release-frames", type=int, default=NORMAL_RELEASE_FRAMES)
    parser.add_argument("--intro-seconds", type=float, default=INTRO_SECONDS)
    capture_mode = parser.add_mutually_exclusive_group()
    capture_mode.add_argument("--timing-probe", action="store_true",
                              help="Capture from 40 game seconds every 5 seconds, plus the endpoint; finish locally for later review.")
    capture_mode.add_argument("--enrollment-check", action="store_true",
                              help="Capture rank, category, party selection, and dress-up for the endpoint review.")
    args = parser.parse_args()
    if args.check:
        from tests.support.lua51 import Lua51, literal

        ast.parse(Path(__file__).read_text(encoding="utf-8"))
        lua = Lua51()
        try:
            loads = "; ".join(f"assert(loadfile({literal((HERE / name).as_posix())}))" for name in ("party_contest.lua", "stage3_enter.lua"))
            print(lua.run(loads + "; return 'Stage 3 Python/Lua syntax OK'"))
        finally:
            lua.close()
        return 0
    if args.host_timeout is None:
        parser.error("Supply the agreed --host-timeout in host seconds.")
    options = {key: getattr(args, key) for key in
               ("category", "rank", "host_timeout", "hold_frames", "release_frames", "intro_seconds", "timing_probe", "enrollment_check")}
    try:
        plan = preview_plan(**options)
        if args.plan_only:
            if args.source_checkpoint:
                _, source, previous = accepted_source(args.source_checkpoint)
                plan.update(source_state=str(source), source_frame=previous["frame"])
            print(json.dumps(plan, indent=2))
            return 0
        if not args.run_name or not args.source_checkpoint:
            parser.error("Execution requires --run-name and --source-checkpoint.")
        if args.background:
            return background(args.run_name, args.source_checkpoint)
        result = run(run_name=args.run_name, source_checkpoint=args.source_checkpoint, ribbon_test=args.ribbon_test, **options)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
    return 0 if result["outcome"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
