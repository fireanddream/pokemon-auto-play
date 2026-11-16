"""Retained dance pixels, counted recovery, and fresh paused-overlay evidence."""

from copy import deepcopy
from itertools import groupby
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from PIL import Image

from contest import dance_master, rank_dance, rank_speed_probe, stage5_dance
from contest.dance_timings import timing_for
from contest.pt_dance_read import EMPTY_PROFILE, FILLED_PROFILES
from contest.timing_profiles import load_profile
from tests.support.bridge import BridgeClock
from tests.support.lua51 import Lua51, literal
from tests.support.paths import ROOT, temporary_directory


CAPTURES = json.loads((ROOT / "tests/support/dance-captures.json").read_text(encoding="utf-8"))["captures"]


def retained_image(name):
    value = CAPTURES[name]
    compressed = bytes(byte for chunk in value["data"] for byte in chunk)
    crop = Image.frombytes("RGB", tuple(value["size"]), zlib.decompress(compressed))
    image = Image.new("RGB", (256, 384))
    image.paste(crop, tuple(value["origin"]))
    return image


def framebuffer_source(image):
    """Construct native GD bytes independently of the production image readers."""
    data = image.convert("RGB").tobytes()
    parts = []
    for color, run in groupby(zip(data[0::3], data[1::3], data[2::3])):
        count = sum(1 for _ in run)
        parts.append(f"string.rep(string.char(0,{color[0]},{color[1]},{color[2]}),{count})")
    return "string.char(255,254,1,0,1,128,1,0,0,0,0)..table.concat({" + ",".join(parts) + "})"


def feedback_image(name):
    image = retained_image("clear_cues")
    image.paste(retained_image(name).crop((0, 0, 256, 50)))
    return image


class AdapterReplay:
    """Real dance adapters/readers; emulator callbacks and file transport are mocked."""

    run_id = "retained-dance-check"
    work = (ROOT / "tmp/offline-dance-captures").as_posix()

    def __init__(self, kind, *, calibrate_speed=False):
        self.kind, self.config_number = kind, 0
        self.clock = BridgeClock()
        self.clock.lua.run(f"""
            native_raw = {framebuffer_source(retained_image('clear_cues'))}
            capture_frames, configurations, binary_bytes = {{}}, {{}}, {{}}
            stylus = {{set=function() end}}
            gui.gdscreenshot = function()
                capture_frames[#capture_frames + 1] = frame
                return native_raw
            end
            local old_open, old_loadfile = io.open, loadfile
            io.open = function(path, mode)
                if mode == 'rb' then
                    if not mock_files[path] then return nil end
                    return {{read=function() return mock_files[path] end,close=function() return true end}}
                elseif path:match('%.bin$') then
                    binary_bytes[path] = mode == 'ab' and (binary_bytes[path] or 0) or 0
                    return {{write=function(self, raw)
                        binary_bytes[path] = binary_bytes[path] + #raw
                        return self
                    end,close=function() return true end}}
                end
                return old_open(path, mode == 'ab' and 'a' or mode)
            end
            loadfile = function(path)
                if configurations[path] then return function() return configurations[path] end end
                return old_loadfile(path)
            end
            return 'ready'
        """)
        if kind in ("master", "stage5-master"):
            self.dances = dance_master.dance_schedule(demo_start_frame=0, count=8 if kind == "stage5-master" else 6)
            for dance in self.dances[:6]:
                dance.update(work=f"{self.work}/dance-{dance['index']}", presses=[])
            config = {"run_id": self.run_id, "work": self.work, "source_frame": 0, "refresh_frames": 0,
                "dances": self.dances, "hold_frames": 2, "offset_frames": -7,
                "feedback_release_frames": 10, "cues_per_dance": 4}
            if kind == "stage5-master":
                config.update(stage=5, transition={"game_seconds": 18, "mash_frames": 1077,
                    "mash_hold_frames": 10, "mash_release_frames": 20},
                    end_frame=self.dances[-1]["end_frame"] + 1077)
            self.clock.lua.run(f"assert(loadfile({literal((ROOT / 'contest/dance_master.lua').as_posix())}))"
                f"({literal(config)}); return 'ready'")
            self.call("init")
        elif kind == "rank":
            self.options = rank_dance.settings(rank="Master", input_slot=1, host_timeout=300,
                calibrate_speed=calibrate_speed)
            self.clock.lua.run(f"dofile({literal((ROOT / 'contest/rank_dance.lua').as_posix())}); return 'ready'")
            self.call("init-run", self.run_id, self.work, "Master", self.configuration(self.options))
        else:
            self.options = rank_speed_probe.probe_plan()
            self.source = next(value for value in self.options["sources"] if value["rank"] == "Hyper")
            self.clock.lua.run(f"dofile({literal((ROOT / 'contest/rank_speed_probe.lua').as_posix())}); return 'ready'")
            self.call("init-run", self.run_id, self.work, self.configuration(self.options))
            self.call("select-source", "Hyper", self.work, 4, "trial")

    def close(self):
        self.clock.close()

    def configuration(self, value, *, path=None):
        self.config_number += 1
        path = path or f"{self.work}/config-{self.config_number}.lua"
        self.clock.lua.run(f"configurations[{literal(path)}]={literal(value)}; return 'ready'")
        return path

    def call(self, action, *args):
        result = self.clock.checkpoint(action, *args)
        if result.get("error") or result.get("recovery_required"):
            raise AssertionError(result.get("error") or result.get("warning"))
        return {**result, "emulator_paused": True}

    def acknowledge(self, name):
        frame = int(self.clock.lua.run("return tostring(frame)"))
        epoch = self.call("capture", "acknowledgment-source")["epoch"]
        if self.kind in ("master", "stage5-master"):
            pending = self.call("visit", name)
        else:
            pending = self.call("visit", name, self.configuration({"frame": frame, "epoch": epoch}))
        record = pending["checkpoint"]
        args = [self.run_id, record["visit"], record["frame"], record["epoch"],
            f"{self.work}/visit-{record['visit']}.ds0", "true"]
        if not record["save_required"]:
            args.append("false")
        completed = self.call("complete", *args)
        if not completed["checkpoint"]["proceed"]:
            raise AssertionError(completed["checkpoint"].get("error"))
        return completed

    def set_image(self, image):
        self.clock.lua.run(f"native_raw={framebuffer_source(image)}; return 'ready'")

    def arm_response(self, skipped=()):
        if self.kind == "master":
            dance = self.dances[0]
            self.call("begin-dance", 1)
            self.clock.command("observe-demo")
            cues = rank_speed_probe.cue_positions(retained_image("clear_cues"), "left")
            presses = [{"button": cue["button"], "demo_x": cue["x"],
                "response_x": cue["x"] + dance["cue_side_offset_pixels"],
                "frame": dance["boundary_frame"] + math.floor((cue["x"] - 8) * dance["frames_per_pixel"] + 0.5) - 7}
                for cue in cues]
            plan = {"dance": 1, "boundary_frame": dance["boundary_frame"], "end_frame": dance["end_frame"],
                "hold_frames": 2, "offset_frames": -7, "presses": presses, "visual_evidence": {
                    "frame": dance["boundary_frame"], "source": "retained_native_cue_pixels", "complete": True,
                    "conflict_policy": "error", "timeline_confirmed": True,
                    "tracked_progress": {"frame_range": [dance["boundary_frame"], dance["boundary_frame"]]},
                    "buttons": [{"button": cue["button"], "demo_x": cue["x"]} for cue in cues]}}
            for index in skipped:
                plan["presses"][index - 1]["skipped"] = True
            self.configuration(plan, path=dance["work"] + "/plan.lua")
            self.call("plan")
            self.acknowledge("start")
        else:
            if self.kind == "rank":
                self.call("select-dance", 1, self.work, 0)
            image = retained_image("clear_cues")
            captured = self.call("capture", "planned-midpoint")
            plan = rank_speed_probe.response_plan({"frame": captured["frame"], "epoch": captured["epoch"],
                "position": rank_speed_probe.pointer_position(image)}, image, [], None,
                rank="Master" if self.kind == "rank" else "Hyper", expected_cues=4,
                speed=timing_for("Master")["pixels_per_frame"], hold_frames=2, offset_frames=-7,
                side_offset_pixels=120, tolerant_inputs=True)
            for index in skipped:
                plan["presses"][index - 1]["skipped"] = True
            self.call("plan", self.configuration(plan))
            self.acknowledge("start" if self.kind == "rank" else "trial-start")
        return plan

    def feedback_events(self, events):
        self.clock.lua.run(f"""
            local before_raw = {framebuffer_source(feedback_image('english_before'))}
            local excellent_raw = {framebuffer_source(feedback_image('english_initial'))}
            local events = {literal(events)}
            gui.gdscreenshot = function()
                capture_frames[#capture_frames + 1] = frame
                for _, event in ipairs(events) do
                    if frame >= event[1] and frame <= event[2] then return excellent_raw end
                end
                return before_raw
            end
            return 'ready'
        """)

    def finish_response(self):
        responded = self.clock.command("mirror")
        expected = {"master": "mirror_complete", "rank": "dance_response_complete",
            "probe": "dance_1_response_complete"}[self.kind]
        if responded.get("stop_reason") != expected or responded.get("error"):
            raise AssertionError(responded.get("error") or responded.get("stop_reason"))
        finished = self.acknowledge("trial-finish" if self.kind == "probe" else "finish")
        return responded, finished["checkpoint"]["snapshot"]["trial"]


class RetainedDancePixelChecks(unittest.TestCase):
    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)
        self.lua.run(f"pixels=dofile({literal((ROOT / 'contest/dance_pixels.lua').as_posix())}); "
            f"json=dofile({literal((ROOT / 'lua/json.lua').as_posix())}); return 'ready'")

    def feedback(self, image, center):
        return json.loads(self.lua.run(f"native_raw={framebuffer_source(image)}; "
            f"return json.encode(pixels.feedback(native_raw,{center}))"))

    def test_retained_english_and_jpn_excellent_palettes_are_recognized(self):
        with Image.open(ROOT / "contest/evidence/completed.png") as source:
            jpn = Image.new("RGB", (256, 384))
            jpn.paste(source.convert("RGB").crop((0, 0, 256, 50)))
        cases = [("english_initial", retained_image("english_initial"), 204),
            ("english_settled", retained_image("english_settled"), 204)]
        cases.extend(("jpn", jpn, center) for center in (44, 124, 204))
        for name, image, center in cases:
            with self.subTest(capture=name, center=center):
                feedback = self.feedback(image, center)
                self.assertEqual(feedback["label"], "Excellent")
                self.assertEqual(feedback["source"], "excellent_bubble_palette")
                self.assertGreaterEqual(feedback["matched_rows"], 2)
                self.assertTrue(center - 36 <= feedback["x"] <= center + 36)
                self.assertTrue(0 <= feedback["y"] < 50)

    def test_good_failure_pre_input_and_overlay_captures_have_no_excellent_palette(self):
        for name, center in (("english_good", 44), ("english_failed", 124), ("english_before", 204),
                ("saved_overlay", 204), ("clear_cues", 204)):
            with self.subTest(capture=name):
                self.assertIsNone(self.feedback(retained_image(name), center))

    def test_obscured_midpoint_skips_unconfirmed_cues_and_clear_capture_keeps_four(self):
        for name, counts, planned in (("saved_overlay", (0, 2), 0), ("clear_cues", (4, 4), 4)):
            with self.subTest(capture=name):
                image = retained_image(name)
                left = rank_speed_probe.cue_positions(image, "left")
                right = rank_speed_probe.cue_positions(image, "right")
                self.assertEqual((len(left), len(right)), counts)
                response = rank_speed_probe.response_plan({"frame": 10079, "epoch": 0,
                    "position": rank_speed_probe.pointer_position(image)}, image, [], None,
                    rank="Hyper", expected_cues=4, speed=5 / 9, hold_frames=2, offset_frames=-7,
                    side_offset_pixels=120, tolerant_inputs=True)
                self.assertEqual((len(response["presses"]), response["skipped_input_count"]), (planned, 4 - planned))
                if not planned:
                    self.assertEqual(response["cues"]["selection_policy"], "confirmed_pairs_only")
                    self.assertEqual(response["cues"]["accepted"], [])


class DanceCaptureRecoveryChecks(unittest.TestCase):
    def replay(self, kind, **options):
        value = AdapterReplay(kind, **options)
        self.addCleanup(value.close)
        return value

    def test_skipped_cues_do_not_end_the_previous_feedback_window_or_duplicate_judgments(self):
        for kind in ("master", "rank", "probe"):
            for missing in (False, True):
                with self.subTest(adapter=kind, missing=missing):
                    replay = self.replay(kind)
                    plan = replay.arm_response(skipped=(2, 4))
                    first, skipped, third = plan["presses"][:3]
                    first_feedback = skipped["frame"] + 3
                    third_feedback = third["frame"] + 11
                    events = [] if missing else [[first_feedback, first_feedback + 5]]
                    replay.feedback_events(events + [[third_feedback, third_feedback + 5]])
                    _, trial = replay.finish_response()
                    self.assertEqual(trial.get("input_count", trial.get("inputs")), 2)
                    self.assertEqual(trial["skipped_input_count"], 2)
                    self.assertEqual([value["index"] for value in trial["judgments"]], [1, 3])
                    self.assertEqual([value["label"] for value in trial["judgments"]],
                        ["Miss" if missing else "Excellent", "Excellent"])
                    self.assertEqual(trial["miss_count"], int(missing))
                    self.assertEqual(trial["judgments"][0]["feedback_frame"], third["frame"] - 1 if missing else first_feedback)
                    self.assertEqual(trial["judgments"][1]["feedback_frame"], third_feedback)
                    self.assertEqual(trial["judgments"][0]["input_frame"], first["frame"])
                    self.assertTrue(all(value["image"]["frame"] == value["feedback_frame"]
                        and value["image"]["direct_read_frames"] == 0 for value in trial["judgments"]))

    def test_a_retained_stale_bubble_produces_one_miss_for_the_executed_input(self):
        for kind in ("master", "rank", "probe"):
            with self.subTest(adapter=kind):
                replay = self.replay(kind)
                replay.arm_response(skipped=(2, 3, 4))
                replay.set_image(feedback_image("english_settled"))
                _, trial = replay.finish_response()
                self.assertEqual((trial.get("input_count", trial.get("inputs")), trial["skipped_input_count"]), (1, 3))
                self.assertEqual((trial["judgment_count"], trial["excellent_count"], trial["miss_count"]), (1, 0, 1))
                self.assertFalse(trial["judgments"][0]["fresh_message"])

    def test_unreadable_prepared_entry_finishes_its_anchored_demo_and_response_without_inputs(self):
        replay = self.replay("rank")
        replay.set_image(retained_image("english_before"))
        replay.clock.lua.run("frame=1000; return 'ready'")
        speed, half = replay.options["rank_timing"]["pixels_per_frame"], replay.options["rank_timing"]["response_frames"]
        anchor = {"frame": 1000, "epoch": 0, "pointer_x": None, "speed": speed, "first_midpoint_frame": 1000 + half}
        replay.call("anchor-midpoints", replay.configuration(anchor))
        replay.call("select-dance", 1, replay.work, 0)
        captured = replay.call("capture", "unreadable-entry")
        timing = captured["observation"]["midpoint_timing"]
        self.assertEqual(timing["source"], "counted_prepared_demo_budget")
        skipped = rank_dance.skipped_response({"frame": 1000, "epoch": 0, "midpoint_timing": timing},
            1, replay.options, "retained_unreadable_entry")
        recovered = replay.call("recover-response", 1, replay.configuration(skipped), "retained_unreadable_entry")
        replay.acknowledge("start")
        responded, trial = replay.finish_response()
        self.assertEqual(responded["frame"], anchor["first_midpoint_frame"] + math.ceil(1 / speed) + half)
        self.assertEqual(recovered["response_plan"]["response_end_frame"], responded["frame"])
        self.assertEqual((trial["completed_dances"], trial["acknowledged_dances"], trial["total_skipped_inputs"]), (1, 0, 4))
        self.assertEqual(trial["judgments"], [])
        self.assertTrue(all(not buttons for buttons in replay.clock.trace()))

    def test_prepared_calibration_uses_the_entry_speed_for_a_fresh_early_midpoint_correction(self):
        replay = self.replay("rank", calibrate_speed=True)
        replay.clock.lua.run("frame=1000; return 'ready'")
        captured = replay.call("capture", "prepared-entry")
        pointer = captured["observation"]["bar"]["last_x"]
        speed = 1 / 3
        first = 1000 + max(1, math.ceil((128 - pointer) / speed))
        replay.call("anchor-midpoints", replay.configuration({"frame": 1000, "epoch": 0,
            "pointer_x": pointer, "speed": speed, "first_midpoint_frame": first}))
        replay.call("select-dance", 1, replay.work, 0)
        self.assertEqual(replay.clock.command("observe-demo")["frame"], first)
        early = retained_image("clear_cues")
        for x in range(8, 248):
            for row, color in enumerate(FILLED_PROFILES[0] if x <= 126 else EMPTY_PROFILE, 155):
                early.putpixel((x, row), color)
        replay.set_image(early)
        captured = replay.call("capture", "early-midpoint")
        next_frame = first + math.ceil((128 - 126) / speed)
        replay.call("retime-midpoint", replay.configuration({"frame": first, "epoch": captured["epoch"],
            "pointer_x": 126, "next_frame": next_frame, "path": captured["observation"]["image"]["path"]}))
        self.assertEqual(int(replay.clock.lua.run("return tostring(frame)")), first)
        replay.set_image(retained_image("clear_cues"))
        corrected = replay.clock.command("observe-demo")
        self.assertEqual((corrected["frame"], corrected["stop_reason"]), (next_frame, "demo_midpoint"))
        self.assertTrue(all(not buttons for buttons in replay.clock.trace()))

    def test_probe_unreadable_recovery_uses_the_remaining_counted_response_budget(self):
        for frame in (1300, 1500):
            with self.subTest(recovery_frame=frame):
                replay = self.replay("probe")
                replay.clock.lua.run("frame=1000; return 'ready'")
                replay.clock.command("observe-demo", 1216)
                replay.set_image(retained_image("english_before"))
                replay.clock.lua.run(f"frame={frame}; return 'ready'")
                captured = replay.call("capture", "unreadable-response")
                timing = captured["observation"]["midpoint_timing"]
                self.assertEqual(timing["source"], "counted_prepared_demo_endpoint")
                speed = replay.source["rank_timing"]["pixels_per_frame"]
                skipped = rank_speed_probe.skipped_trial({"frame": frame, "epoch": 0,
                    "position": {"x": None}, "midpoint_timing": timing}, replay.source,
                    speed=speed, hold_frames=2, offset_frames=-7, reason="retained_unreadable_midpoint")
                replay.call("recover-response", replay.configuration(skipped), "retained_unreadable_midpoint")
                replay.acknowledge("trial-start")
                responded, trial = replay.finish_response()
                self.assertEqual(responded["frame"], max(frame + 1, 1216 + math.ceil(120 / speed)))
                self.assertEqual((trial["inputs"], trial["skipped_input_count"], trial["judgment_count"]), (0, 4, 0))
                self.assertTrue(all(not buttons for buttons in replay.clock.trace()))

    def test_master_skips_six_responses_and_repeated_completed_handoffs_add_no_frames(self):
        replay = self.replay("stage5-master")
        replay.set_image(retained_image("saved_overlay"))
        for dance in replay.dances[:6]:
            replay.call("begin-dance", dance["index"])
            frame = int(replay.clock.lua.run("return tostring(frame)"))
            skipped = dance_master.skipped_demonstration(dance, frame=frame,
                reason="retained_obscured_cues", hold_frames=2, offset_frames=-7)
            recovered = replay.call("recover-response", replay.configuration(skipped), "retained_obscured_cues")
            replay.acknowledge("start")
            if dance["index"] == 1:
                self.assertEqual(recovered["observation"]["offset_check"]["image"]["capture_callback"], "paused_response_recovery")
                replay.acknowledge("offset-check")
            responded = replay.clock.command("mirror-rest" if dance["index"] == 1 else "mirror")
            self.assertEqual((responded["frame"], responded["stop_reason"]), (dance["end_frame"], "mirror_complete"))
            finished = replay.acknowledge("finish")["checkpoint"]["snapshot"]["trial"]
            self.assertEqual((finished["completed_dances"], finished["acknowledged_dances"]), (dance["index"], dance["index"] - 1))
            self.assertEqual((finished["total_input_count"], finished["total_skipped_inputs"], finished["total_judgment_count"]),
                (0, dance["index"] * 4, 0))
        self.assertTrue(all(not buttons for buttons in replay.clock.trace()))
        for action, expected, endpoint in (("lead-dances", "lead_dances_complete", replay.dances[-1]["end_frame"]),
                ("acting-transition", "B_transition_complete", replay.dances[-1]["end_frame"] + 1077)):
            replay.clock.lua.run(f"""
                local screenshot, failed = gui.gdscreenshot, false
                gui.gdscreenshot = function()
                    if frame == {endpoint} and not failed then failed=true; error('Retained capture failed') end
                    return screenshot()
                end
                return 'ready'
            """)
            interrupted = replay.clock.command(action)
            self.assertEqual((interrupted["frame"], interrupted["stop_reason"]), (endpoint, "dance_recovery_ready"))
            before = len(replay.clock.trace())
            repeated = replay.clock.command(action)
            self.assertEqual((repeated["frame"], repeated["stop_reason"]), (endpoint, expected))
            self.assertEqual(len(replay.clock.trace()), before)


class PausedDanceOverlayChecks(unittest.TestCase):
    def setUp(self):
        self.replay = AdapterReplay("rank")
        self.addCleanup(self.replay.close)
        self.replay.set_image(retained_image("saved_overlay"))
        self.replay.clock.lua.run("frame=10079; capture_frames={}; return 'ready'")
        self.wall = 0
        self.controller = SimpleNamespace(session=lambda: {"pid": 7, "window": 11},
            paused=lambda _: self.replay.clock.lua.run("return tostring(mock_paused)") == "true",
            remaining=lambda: 300 - self.wall, set_pause=Mock(side_effect=self.pause))

    def pause(self, paused):
        self.assertTrue(paused)
        self.replay.clock.lua.run("mock_paused=true; return 'ready'")

    def sleep(self, seconds):
        self.wall += seconds
        self.replay.set_image(retained_image("clear_cues"))

    def test_saved_overlay_wait_retains_fresh_captures_at_one_paused_frame_and_epoch(self):
        with temporary_directory() as folder, patch("contest.dance_master.time.monotonic", side_effect=lambda: self.wall), \
                patch("contest.dance_master.time.sleep", side_effect=self.sleep):
            runtime = SimpleNamespace(controller=self.controller, command=Mock(side_effect=self.replay.call), record=Mock())
            saved = {"slot": 9, "retained_path": str(Path(folder) / "accepted.ds9")}
            result = rank_dance.settle_saved_overlay(runtime, Path(folder), 1, saved, "demo", 3)
            self.assertEqual((result["requested_host_seconds"], result["elapsed_host_seconds"], result["emulator_frames"]), (3, 3, 0))
            self.assertEqual((result["frame"], result["epoch"], result["input_policy"]), (10079, 0, "none"))
            self.assertTrue(result["emulator_paused"])
            self.assertEqual(result["native_save"], saved)
            self.assertEqual(json.loads((Path(folder) / "overlay-demo.json").read_text(encoding="utf-8")), result)
            self.assertEqual([call.args for call in runtime.command.call_args_list],
                [("capture", "overlay-before-demo"), ("capture", "overlay-settled-demo")])
            runtime.record.assert_called_once_with("dance_overlay_settled", **result)
            for image in (result["before"], result["capture"]):
                self.assertEqual((image["frame"], image["epoch"], image["direct_read_frames"]), (10079, 0, 0))
                self.assertEqual((image["source"], image["capture_callback"]), ("fresh_zero_frame_framebuffer", "paused_lua_menu"))
            self.assertEqual(json.loads(self.replay.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix()
                + "').encode(capture_frames)")), [10079, 10079])
            self.assertEqual(self.replay.clock.trace(), [])

    def test_overlay_wait_rejects_drift_or_stale_capture_evidence(self):
        before = self.replay.call("capture", "before")
        cases = ({"frame": 10080}, {"epoch": 1}, {"emulator_paused": False},
            {"released": False}, {"source": "cached_framebuffer"}, {"capture_callback": "after_frame"}, {"direct_read_frames": 1})
        for changes in cases:
            with self.subTest(changes=changes), patch("contest.dance_master.time.monotonic", side_effect=lambda: self.wall), \
                    patch("contest.dance_master.time.sleep", side_effect=self.sleep):
                after = deepcopy(before)
                for key, value in changes.items():
                    if key in ("frame", "epoch", "emulator_paused"):
                        after[key] = value
                        if key != "emulator_paused":
                            after["observation"]["image"][key] = value
                    elif key == "released":
                        after["observation"][key] = value
                    else:
                        after["observation"]["image"][key] = value
                with self.assertRaisesRegex(RuntimeError, "same paused frame and epoch"):
                    dance_master.settle_native_overlay(self.controller, Mock(side_effect=[before, after]), 3, name="demo")
                self.assertEqual(self.replay.clock.trace(), [])

    def test_lost_native_pause_is_reestablished_and_keeps_only_the_actual_before_capture(self):
        def interrupted_sleep(seconds):
            self.wall += seconds
            self.replay.clock.lua.run("mock_paused=false; return 'ready'")

        capture = Mock(side_effect=self.replay.call)
        with patch("contest.dance_master.time.monotonic", side_effect=lambda: self.wall), \
                patch("contest.dance_master.time.sleep", side_effect=interrupted_sleep):
            with self.assertRaisesRegex(RuntimeError, "Native pause was lost"):
                dance_master.settle_native_overlay(self.controller, lambda name: capture("capture", name), 3, name="demo")
        capture.assert_called_once_with("capture", "overlay-before-demo")
        self.assertTrue(self.controller.paused(self.controller.session()))
        self.assertEqual(self.controller.set_pause.call_count, 2)
        self.assertEqual(self.replay.clock.trace(), [])

    def test_shared_dance_defaults_keep_the_three_second_wait_in_host_time(self):
        for code in ("CPUJ", "CPUE"):
            with self.subTest(game=code):
                profile = load_profile(code)
                plans = (rank_dance.settings(rank="Master", input_slot=1, host_timeout=300, timing_profile=profile),
                    rank_dance.stage5_plan(rank="Master", source_frame=1000, host_timeout=300, timing_profile=profile),
                    stage5_dance.dancing_plan(host_timeout=300, timing_profile=profile),
                    rank_speed_probe.probe_plan(timing_profile=profile))
                for plan in plans:
                    self.assertEqual((plan["native_osd_settle_host_seconds"], plan["hold_frames"],
                        plan["feedback_release_frames"], plan["offset_frames"]), (3, 2, 10, -7))
