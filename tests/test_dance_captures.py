"""Retained dance pixels, main-engine recovery, and fresh paused-overlay evidence."""

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

from contest import dance_response, rank_dance, runtime, super_contest
from contest.timing_profiles import load_profile
from tests.support.lua51 import Lua51, literal
from tests.support.paths import ROOT, fixture_path, temporary_directory


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
    """Reach Stage 5 through the actual main dispatcher, then use retained native pixels."""

    def __init__(self):
        from tests.test_contest_runtime import ContestClock

        self.harness = ContestClock(debug=0)
        self.clock = self.harness.clock
        self.harness.prepare_entry()
        self.harness.dress(0, finish=True)
        source = self.harness.boundary
        target = source["snapshot"]["target"]
        self.options = rank_dance.stage5_plan(rank=target["rank"], category=target["category"],
            source_frame=source["frame"], entry_pointer_x=source["snapshot"]["screen"]["pointer"]["position_bounds"][0],
            host_timeout=300)
        self.clock.lua.run("local previous=dofile; dofile=function(path) "
            "if path:match('/dance_pixels%.lua$') then return assert(loadfile(path))() end "
            "return previous(path) end; return 'ready'")
        config, _ = self.harness.begin(5, self.options)
        self.work, self.run_id = config["work"], config["run_id"]
        self.call("init")
        self.set_image(retained_image("clear_cues"))
        self.clock.reset_measurement()

    def close(self):
        self.harness.close()

    def configuration(self, value):
        return self.harness.config(value)

    def call(self, action, *args):
        result = self.clock.checkpoint(action, *args)
        if result.get("error") or result.get("recovery_required"):
            raise AssertionError(result.get("error") or result.get("warning"))
        return {**result, "emulator_paused": True}

    def acknowledge(self, name):
        frame = int(self.clock.lua.run("return tostring(frame)"))
        epoch = self.call("capture", "acknowledgment-source")["epoch"]
        pending = self.call("visit", name, self.configuration({"frame": frame, "epoch": epoch}))
        record = pending["checkpoint"]
        completed = self.call("complete", self.run_id, record["visit"], record["frame"], record["epoch"],
            f"{self.work}/visit-{record['visit']}.json", "true", "false")
        if not completed["checkpoint"]["proceed"]:
            raise AssertionError(completed["checkpoint"].get("error"))
        return completed

    def set_image(self, image):
        self.clock.lua.run(f"native_raw={framebuffer_source(image)}; return 'ready'")

    def arm_response(self, skipped=()):
        self.call("select-dance", 1, self.work, 0)
        self.clock.command("observe-demo")
        image = retained_image("clear_cues")
        captured = self.call("capture", "planned-midpoint")
        plan = dance_response.response_plan({"frame": captured["frame"], "epoch": captured["epoch"],
            "position": dance_response.pointer_position(image)}, image, rank="Master", expected_cues=4,
            speed=self.options["rank_timing"]["pixels_per_frame"], hold_frames=2, offset_frames=-7,
            side_offset_pixels=120)
        plan["presses"] = [press for index, press in enumerate(plan["presses"], 1) if index not in skipped]
        plan["cues"]["accepted"] = [cue for index, cue in enumerate(plan["cues"]["accepted"], 1) if index not in skipped]
        plan["cues"]["unresolved_cues"] = plan["skipped_input_count"] = len(skipped)
        plan["response_end_frame"] = captured["frame"] + self.options["rank_timing"]["response_frames"]
        plan.update(rank_dance.feedback_schedule(plan, delay_frames=11, rank_timing=self.options["rank_timing"]))
        self.call("plan", self.configuration(plan))
        self.acknowledge("rank-dance-start")
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
        if responded.get("stop_reason") != "offset_feedback_ready":
            raise AssertionError(responded.get("error") or responded.get("stop_reason"))
        self.acknowledge("rank-dance-offset")
        responded = self.clock.command("mirror-rest")
        if responded.get("stop_reason") != "dance_response_complete":
            raise AssertionError(responded.get("error") or responded.get("stop_reason"))
        finished = self.acknowledge("rank-dance-finish")
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
        with Image.open(fixture_path("completed.png")) as source:
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

    def test_obscured_demo_uses_readable_response_cues_and_clear_capture_keeps_four(self):
        for name, counts, planned in (("saved_overlay", (0, 4), 4), ("clear_cues", (4, 4), 4)):
            with self.subTest(capture=name):
                image = retained_image(name)
                left = dance_response.cue_positions(image, "left")
                right = dance_response.cue_positions(image, "right")
                self.assertEqual((len(left), len(right)), counts)
                response = dance_response.response_plan({"frame": 10079, "epoch": 0,
                    "position": dance_response.pointer_position(image)}, image,
                    rank="Hyper", expected_cues=4, speed=5 / 9, hold_frames=2, offset_frames=-7, side_offset_pixels=120)
                self.assertEqual((len(response["presses"]), response["skipped_input_count"]), (planned, 4 - planned))
                if name == "saved_overlay":
                    self.assertEqual(response["cues"]["selection_policy"], "complete_side_positions")
                    self.assertEqual([(cue["demo_x"], cue["button"], cue["source_side"])
                        for cue in response["cues"]["accepted"]],
                        [(28, "B", "right"), (48, "Y", "right"), (68, "B", "right"), (88, "Y", "right")])


class DanceCaptureRecoveryChecks(unittest.TestCase):
    def replay(self):
        value = AdapterReplay()
        self.addCleanup(value.close)
        return value

    def test_skipped_cues_do_not_end_the_first_feedback_window_or_duplicate_judgments(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                replay = self.replay()
                full = replay.options["rank_timing"]
                plan = replay.arm_response(skipped=(2, 4))
                first, third = plan["presses"]
                first_feedback = first["frame"] + round(20 / full["pixels_per_frame"]) + 3
                third_feedback = third["frame"] + 11
                events = [] if missing else [[first_feedback, first_feedback + 5]]
                replay.feedback_events(events + [[third_feedback, third_feedback + 5]])
                _, trial = replay.finish_response()
                self.assertEqual((trial["inputs"], trial["skipped_input_count"]), (2, 2))
                self.assertEqual([value["index"] for value in trial["judgments"]], [1])
                self.assertEqual([value["label"] for value in trial["judgments"]], ["Miss" if missing else "Excellent"])
                judgment = trial["judgments"][0]
                self.assertEqual(judgment["feedback_frame"], third["frame"] - 1 if missing else first_feedback)
                self.assertEqual(judgment["input_frame"], first["frame"])
                self.assertEqual(judgment["image"]["frame"], judgment["feedback_frame"])
                self.assertEqual(judgment["image"]["direct_read_frames"], 0)

    def test_a_retained_stale_bubble_produces_one_miss_for_the_executed_input(self):
        replay = self.replay()
        replay.arm_response(skipped=(2, 3, 4))
        replay.set_image(feedback_image("english_settled"))
        _, trial = replay.finish_response()
        self.assertEqual((trial["inputs"], trial["skipped_input_count"]), (1, 3))
        self.assertEqual((trial["judgment_count"], trial["excellent_count"], trial["miss_count"]), (1, 0, 1))
        self.assertFalse(trial["judgments"][0]["fresh_message"])

    def test_counted_display_hold_cannot_use_an_unverified_pointer_origin(self):
        replay = self.replay()
        replay.call("select-dance", 1, replay.work, 0)
        image = retained_image("clear_cues")
        replay.clock.lua.run("frame=1002; return 'ready'")
        captured = replay.call("capture", "held-display")
        anchor = {"frame": 1000, "epoch": captured["epoch"], "pointer_x": 127,
            "source": "fresh_paused_midpoint_before_counted_correction"}
        planned = dance_response.response_plan({"frame": 1002, "epoch": captured["epoch"],
            "position": {"x": 127, "phase": "demo"}}, image, rank="Master", expected_cues=4,
            speed=5 / 9, hold_frames=2, offset_frames=-7, side_offset_pixels=120, counted_midpoint_anchor=anchor)
        result = replay.clock.checkpoint("plan", replay.configuration(planned))
        self.assertTrue(result["recovery_required"])
        self.assertIn("verified pointer origin", result["warning"])
        self.assertTrue(all(not buttons for buttons in replay.clock.trace()))

    def test_main_skips_six_responses_and_repeated_completed_handoffs_add_no_frames(self):
        replay = self.replay()
        replay.set_image(retained_image("saved_overlay"))
        for number in range(1, 7):
            gap = replay.options["leader_switch_gap_frames"] if number in (3, 5) else 0
            replay.call("select-dance", number, replay.work, gap)
            if gap:
                replay.clock.command("wait-gap")
            captured = replay.call("capture", "unreadable-midpoint")
            skipped = rank_dance.skipped_response({"frame": captured["frame"], "epoch": captured["epoch"],
                "midpoint_timing": captured["observation"]["midpoint_timing"]}, number, replay.options, "retained_obscured_cues")
            skipped.update(rank_dance.feedback_schedule(skipped, delay_frames=11, rank_timing=replay.options["rank_timing"]))
            recovered = replay.call("recover-response", number, replay.configuration(skipped), "retained_obscured_cues")
            replay.acknowledge("rank-dance-start")
            if number == 1:
                self.assertEqual(recovered["observation"]["offset_check"]["image"]["capture_callback"], "paused_response_recovery")
                replay.acknowledge("rank-dance-offset")
            responded = replay.clock.command("mirror")
            self.assertEqual((responded["frame"], responded["stop_reason"]), (skipped["response_end_frame"], "dance_response_complete"))
            finished = replay.acknowledge("rank-dance-finish")["checkpoint"]["snapshot"]["trial"]
            self.assertEqual((finished["completed_dances"], finished["acknowledged_dances"]), (number, number - 1))
            self.assertEqual((finished["total_input_count"], finished["total_skipped_inputs"], finished["total_judgment_count"]), (0, number * 4, 0))
        self.assertTrue(all(not buttons for buttons in replay.clock.trace()))
        lead_end = skipped["response_end_frame"] + replay.options["rank_timing"]["lead_dances_frames"]
        for action, expected, endpoint in (("lead-dances", "lead_dances_complete", lead_end),
                ("acting-transition", "B_transition_complete", lead_end + 1077)):
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
        self.replay = AdapterReplay()
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
        with temporary_directory() as folder, patch("contest.runtime.time.monotonic", side_effect=lambda: self.wall), \
                patch("contest.runtime.time.sleep", side_effect=self.sleep):
            caller = SimpleNamespace(controller=self.controller, command=Mock(side_effect=self.replay.call), record=Mock())
            saved = {"slot": 4, "retained_path": str(Path(folder) / "accepted.ds4")}
            result = rank_dance.settle_saved_overlay(caller, Path(folder), 1, saved, "demo", 3)
            self.assertEqual((result["requested_host_seconds"], result["elapsed_host_seconds"], result["emulator_frames"]), (3, 3, 0))
            self.assertEqual((result["frame"], result["epoch"], result["input_policy"]), (10079, 0, "none"))
            self.assertTrue(result["emulator_paused"])
            self.assertEqual(result["native_save"], saved)
            self.assertEqual(json.loads((Path(folder) / "overlay-demo.json").read_text(encoding="utf-8")), result)
            self.assertEqual([call.args for call in caller.command.call_args_list],
                [("capture", "overlay-before-demo"), ("capture", "overlay-settled-demo")])
            caller.record.assert_called_once_with("dance_overlay_settled", **result)
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
            with self.subTest(changes=changes), patch("contest.runtime.time.monotonic", side_effect=lambda: self.wall), \
                    patch("contest.runtime.time.sleep", side_effect=self.sleep):
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
                    runtime.settle_native_overlay(self.controller, Mock(side_effect=[before, after]), 3, name="demo")
                self.assertEqual(self.replay.clock.trace(), [])

    def test_lost_native_pause_is_reestablished_and_keeps_only_the_actual_before_capture(self):
        def interrupted_sleep(seconds):
            self.wall += seconds
            self.replay.clock.lua.run("mock_paused=false; return 'ready'")

        capture = Mock(side_effect=self.replay.call)
        with patch("contest.runtime.time.monotonic", side_effect=lambda: self.wall), \
                patch("contest.runtime.time.sleep", side_effect=interrupted_sleep):
            with self.assertRaisesRegex(RuntimeError, "Native pause was lost"):
                runtime.settle_native_overlay(self.controller, lambda name: capture("capture", name), 3, name="demo")
        capture.assert_called_once_with("capture", "overlay-before-demo")
        self.assertTrue(self.controller.paused(self.controller.session()))
        self.assertEqual(self.controller.set_pause.call_count, 2)
        self.assertEqual(self.replay.clock.trace(), [])

    def test_shared_dance_defaults_keep_the_three_second_wait_in_host_time(self):
        for code in ("CPUJ", "CPUE"):
            with self.subTest(game=code):
                profile = load_profile(code)
                plan = rank_dance.stage5_plan(rank="Master", source_frame=1000, entry_pointer_x=64,
                    host_timeout=300, timing_profile=profile)
                self.assertEqual((plan["native_osd_settle_host_seconds"], plan["hold_frames"],
                    plan["feedback_release_frames"], plan["offset_frames"]), (3, 2, 10, -7))
                self.assertEqual(super_contest.chain_plan(timing_profile=profile)["native_osd_settle_host_seconds"], 3)
