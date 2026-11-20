"""Post-Acting language-independent pixels, deadline-bound retries, and first-X ownership."""

from copy import deepcopy
from contextlib import redirect_stdout
import io
from itertools import groupby
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from contest import sections, stage6_act, super_contest
from host.guards import wait_visual_match
from tests.support.lua51 import Lua51, literal
from tests.support.paths import ROOT, temporary_directory
from tests.test_contest_runtime import ContestClock


class PostActingPixelChecks(unittest.TestCase):
    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)
        self.lua.run(f"checks=dofile({literal((ROOT / 'contest/screen_checks.lua').as_posix())}); "
            f"json=dofile({literal((ROOT / 'lua/json.lua').as_posix())}); return 'ready'")
        self.fixture = json.loads((ROOT / "tests/support/post-acting-capture.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(self.fixture["dialogue_top_bounds"]), stage6_act.DIALOGUE_TOP_BOUNDS)
        closed = self.image(open_dialogue=False)
        left, top, right, bottom = stage6_act.DIALOGUE_TOP_BOUNDS
        self.reference = {"player_points": self.fixture["player_points"],
            "dialogue_top": {"bounds": stage6_act.DIALOGUE_TOP_BOUNDS,
                "source": "reviewed_closed_hall_top_pixels",
                "points": [[x, y, *closed.getpixel((x, y))]
                    for y in range(top, bottom) for x in range(left, right)]}}

    def image(self, *, open_dialogue):
        image = Image.new("RGB", (256, 384), (23, 45, 67))
        for x, y, r, g, b in self.fixture["player_points"]:
            image.putpixel((x, y), (r, g, b))
        left, top, right, bottom = self.fixture["dialogue_top_bounds"]
        rows = self.fixture["open_dialogue_top_rows" if open_dialogue else "closed_dialogue_top_rows"]
        self.assertEqual(len(rows), bottom - top)
        for dy, row in enumerate(rows):
            self.assertEqual(len(row), right - left)
            for dx, code in enumerate(row):
                image.putpixel((left + dx, top + dy), tuple(self.fixture["palette"][code]))
        return image

    def read(self, image, reference=None):
        data, parts = image.convert("RGB").tobytes(), []
        for color, run in groupby(zip(data[0::3], data[1::3], data[2::3])):
            count = sum(1 for _ in run)
            parts.append(f"string.rep(string.char(0,{color[0]},{color[1]},{color[2]}),{count})")
        raw = f"string.char(255,254,1,0,{image.height // 256},{image.height % 256},1,0,0,0,0)..table.concat({{" \
            + ",".join(parts) + "})"
        return json.loads(self.lua.run(f"return json.encode(checks.post_acting({raw}, "
            f"{literal(self.reference if reference is None else reference)}))"))

    def test_three_language_dialogues_fail_even_when_the_player_position_matches(self):
        self.assertEqual(self.fixture["sources"], ["Chinese", "Japan", "USA"])
        for index, language in enumerate(self.fixture["sources"]):
            with self.subTest(language=language):
                image = self.image(open_dialogue=True)
                image.paste((index * 60, 100, 200), (0, 149, 256, 384))
                result = self.read(image)
                self.assertTrue(result["position"]["confirmed"])
                self.assertFalse(result["dialogue_closed"]["confirmed"])
                self.assertFalse(result["ready"])
                self.assertEqual(result["direct_read_frames"], 0)
                self.assertEqual(result["dialogue_closed"]["source"], "fresh_native_pixels_against_reviewed_reference")
                self.assertEqual(result["dialogue_closed"]["expected_pixels"], 21)

    def test_both_checks_are_required_but_text_and_the_lower_screen_are_ignored(self):
        closed = self.image(open_dialogue=False)
        self.assertTrue(self.read(closed)["ready"])
        for color in ((0, 0, 0), (255, 255, 255)):
            image = closed.copy()
            image.paste(color, (0, 149, 256, 384))
            self.assertTrue(self.read(image)["ready"])
        position_mismatch = closed.copy()
        position_mismatch.putpixel((124, 78), (0, 0, 0))
        result = self.read(position_mismatch)
        self.assertFalse(result["ready"])
        self.assertFalse(result["position"]["confirmed"])
        self.assertTrue(result["dialogue_closed"]["confirmed"])
        tip_mismatch = closed.copy()
        tip_mismatch.putpixel((128, 147), (0, 0, 0))
        result = self.read(tip_mismatch)
        self.assertFalse(result["ready"])
        self.assertTrue(result["position"]["confirmed"])
        self.assertFalse(result["dialogue_closed"]["confirmed"])
        self.assertFalse(self.read(Image.new("RGB", (256, 384)))["ready"])
        image = closed.copy()
        image.paste((0, 0, 0), (0, 0, 256, 78))
        self.assertTrue(self.read(image)["ready"])

    def test_player_reference_requires_acknowledged_startup_and_tip_stays_closed(self):
        with temporary_directory() as folder:
            folder = Path(folder)
            path = folder / "startup.png"
            self.image(open_dialogue=False).save(path)
            previous = {"status": "complete", "proceed": True, "frame": 100, "epoch": 2,
                "snapshot": {"screen": {"confirmed": True, "expected": "loaded_overworld",
                    "source": "local_reference_pixels", "path": str(path), "frame": 100, "epoch": 2}}}
            reference = stage6_act.overworld_reference_from_checkpoint(folder / "startup.json", previous)
            self.assertEqual(reference["player_points"], self.fixture["player_points"])
            self.assertEqual(reference["dialogue_top"], self.reference["dialogue_top"])
            self.assertTrue(self.read(self.image(open_dialogue=False), reference)["ready"])
            self.image(open_dialogue=True).save(path)
            reference = stage6_act.overworld_reference_from_checkpoint(folder / "startup.json", previous)
            self.assertFalse(self.read(self.image(open_dialogue=True), reference)["ready"])
            for field, value in (("confirmed", False), ("expected", "other"),
                    ("source", "cached"), ("frame", 99), ("epoch", 1)):
                with self.subTest(field=field):
                    invalid = deepcopy(previous)
                    invalid["snapshot"]["screen"][field] = value
                    with self.assertRaises(ValueError):
                        stage6_act.overworld_reference_from_checkpoint(folder / "startup.json", invalid)
            for field, value in (("status", "awaiting_ack"), ("proceed", False)):
                invalid = {**previous, field: value}
                with self.assertRaises(ValueError):
                    stage6_act.overworld_reference_from_checkpoint(folder / "startup.json", invalid)
            Image.new("RGB", (256, 192)).save(path)
            with self.assertRaisesRegex(ValueError, "native stacked"):
                stage6_act.overworld_reference_from_checkpoint(folder / "startup.json", previous)

    def test_invalid_framebuffers_and_empty_references_cannot_mean_dialogue_absent(self):
        with self.assertRaisesRegex(AssertionError, "native"):
            self.lua.run(f"return json.encode(checks.post_acting('unreadable', {literal(self.reference)}))")
        reference = deepcopy(self.reference)
        reference["dialogue_top"]["points"] = []
        with self.assertRaisesRegex(AssertionError, "startup"):
            self.read(self.image(open_dialogue=False), reference)
        reference["dialogue_top"]["points"] = [[300, 146, 239, 142, 158]]
        with self.assertRaisesRegex(AssertionError, "outside"):
            self.read(self.image(open_dialogue=False), reference)


class ReusableVisualGuardChecks(unittest.TestCase):
    def test_button_and_retry_settle_durations_are_owned_by_the_caller(self):
        detections = iter([False, False, True])
        controller = SimpleNamespace(remaining=Mock(return_value=300))
        taps, accepted = [], Mock()

        def observe():
            return {"matched": next(detections)}

        def tap(sample):
            taps.append(("A", 2 if sample["matched"] else 4))

        checks = wait_visual_match(controller, observe, tap,
            matches=lambda sample: sample["matched"], accept=accepted)
        self.assertEqual(taps, [("A", 4), ("A", 4), ("A", 2)])
        self.assertEqual([check["matched"] for check in checks], [False, False, True])
        accepted.assert_called_once_with(checks[-1])
        self.assertEqual(controller.remaining.call_count, 7)


class PostActingLoopChecks(unittest.TestCase):
    def loop(self, folder, detections, *, deadline=100):
        state = {"frame": 1000, "host_seconds": 0, "checks": 0, "settled": 0}
        timing = stage6_act.acting_plan(host_timeout=300)["post_acting_readiness"]
        self.assertEqual((timing["retry_game_seconds"], timing["settle_game_seconds"]), (3, 3))

        def remaining():
            if state["host_seconds"] >= deadline:
                raise TimeoutError("timeout")
            return deadline - state["host_seconds"]

        controller = SimpleNamespace(live_deadline=deadline, remaining=Mock(side_effect=remaining))

        def command(action, *arguments):
            self.assertEqual(arguments, ())
            if action == "post-acting-settled":
                state["settled"] += 1
                return {}
            self.assertEqual(action, "post-acting-readiness")
            state["checks"] += 1
            ready = detections[min(state["checks"] - 1, len(detections) - 1)]
            phase = "settle" if ready else "retry"
            frames = timing[phase + "_frames"]
            cycle, tail = divmod(frames, 30)
            steps = ["B:10:20"] * cycle + ([f"B:10:{tail - 10}"] if tail > 10 else [f"wait:{tail}"] if tail else [])
            path = folder / f"post-acting-readiness-{state['checks']:03d}.gd"
            path.write_bytes(bytes((255, 254, 1, 0, 1, 128, 1, 0, 0, 0, 0))
                + bytes((0, 81, 81, 81)) * (256 * 384))
            return {"observation": {"image": {"path": str(path), "frame": state["frame"], "epoch": 0,
                "source": "fresh_zero_frame_framebuffer", "direct_read_frames": 0},
                "readiness": {"ready": ready}, "pause_plan": {"phase": phase, "steps": steps,
                    "start_frame": state["frame"], "end_frame": state["frame"] + frames, "epoch": 0}}}

        def advance(action, name, *steps, timeout, deadline):
            self.assertEqual((action, name, timeout, deadline), ("combo", "inputs", None, controller.live_deadline))
            for step in steps:
                values = step.split(":")
                self.assertIn(values[0], ("B", "wait"))
                state["frame"] += sum(map(int, values[1:]))
            state["host_seconds"] += 1
            return {"frame": state["frame"], "stop_reason": "combo_complete"}

        return state, SimpleNamespace(controller=controller, command=Mock(side_effect=advance)), \
            SimpleNamespace(command=Mock(side_effect=command))

    def test_three_second_retries_then_settle_are_silent_and_retain_every_capture(self):
        with temporary_directory() as folder:
            folder = Path(folder)
            state, emulator, client = self.loop(folder, [False, False, True])
            output = io.StringIO()
            with redirect_stdout(output):
                checks = stage6_act.wait_post_acting_ready(emulator, client, folder)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual([check["pause_plan"]["phase"] for check in checks], ["retry", "retry", "settle"])
            self.assertEqual(state["settled"], 1)
            self.assertEqual(state["frame"], 1000 + 3 * round(3 * stage6_act.FRAME_RATE))
            self.assertEqual(len(list(folder.glob("post-acting-readiness-*.gd"))), 3)
            self.assertEqual(len(list(folder.glob("post-acting-readiness-*.png"))), 3)
            for check in checks:
                path = Path(check["image"]["path"])
                self.assertTrue(path.is_file() and path.with_suffix(".png").is_file())
                saved = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                self.assertEqual(saved["end_frame"], check["pause_plan"]["end_frame"])

    def test_timeout_retains_the_latest_mismatch_without_visits_or_a_new_deadline(self):
        with temporary_directory() as folder:
            folder = Path(folder)
            state, emulator, client = self.loop(folder, [False], deadline=2)
            with self.assertRaises(TimeoutError):
                stage6_act.wait_post_acting_ready(emulator, client, folder)
            self.assertEqual((state["checks"], state["settled"], emulator.controller.live_deadline), (2, 0, 2))
            self.assertEqual(len(list(folder.glob("post-acting-readiness-*.gd"))), 2)
            self.assertEqual(len(list(folder.glob("post-acting-readiness-*.png"))), 2)
            self.assertTrue(all(call.args == ("post-acting-readiness",) for call in client.command.call_args_list))
            last = json.loads((folder / "post-acting-readiness-002.json").read_text(encoding="utf-8"))
            self.assertFalse(last["readiness"]["ready"])

    def test_timeout_during_settling_does_not_authorize_x(self):
        with temporary_directory() as folder:
            state, emulator, client = self.loop(Path(folder), [True], deadline=1)
            with self.assertRaises(TimeoutError):
                stage6_act.wait_post_acting_ready(emulator, client, Path(folder))
            self.assertEqual(state["settled"], 0)
            self.assertEqual(client.command.call_count, 1)


class PostActingRuntimeChecks(unittest.TestCase):
    def harness(self, debug=0):
        harness = ContestClock(debug=debug)
        self.addCleanup(harness.close)
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        return harness

    def test_main_host_guards_before_x_read_x_and_never_opens_x_on_guard_timeout(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout), temporary_directory() as folder:
                events = []
                chosen = {"steps": ["A:12:90", "B:10:20"], "move": 1, "judge": 1,
                    "move_category": "Cool", "move_hearts": 2, "acting_position": 1,
                    "unrecognized_fallback": False, "matching_category_override": False}
                runtime = SimpleNamespace(settings=super_contest.chain_plan(debug=0), stage=6, run_id="host-guard",
                    section_visit=1, active_work=Path(folder), overworld={}, unavailable_move_slots=[],
                    party={}, target={}, emulator=Mock(), start_section=Mock(), json=Mock(), record=Mock())

                def command(action, *arguments):
                    events.append(action)
                    if action == "round-visit":
                        snapshot = {"stars": {"counts": [0, 0, 0]}, "acting_order": {"position": 1},
                            "move_categories": {}, "plan": chosen}
                        return {"checkpoint": {"snapshot": snapshot, "visit": arguments[0], "frame": 100}}
                    if action == "result-visit":
                        return {"checkpoint": {"snapshot": {"party": {}}, "decision": "hit", "frame": 100}}
                    return {}

                def acknowledge(pending, *, slot):
                    runtime.boundary = {"checkpoint": pending["checkpoint"], "save_state": None, "receipt": "accepted.json"}
                    return pending

                def action(action, name, *steps, expected):
                    events.append(steps)
                    return {"frame": 100}

                def guard(*_):
                    events.append("guard")
                    if timeout:
                        raise TimeoutError("timeout")
                    return []

                runtime.command, runtime.acknowledge, runtime.action = command, acknowledge, action
                with patch.object(stage6_act, "prepare_render", return_value={}), \
                        patch.object(stage6_act, "wait_round_ready", return_value=[]), \
                        patch.object(stage6_act, "retain_capture", return_value={}), \
                        patch.object(stage6_act, "wait_post_acting_ready", side_effect=guard):
                    if timeout:
                        with self.assertRaises(TimeoutError):
                            sections.act(runtime, attempt=1)
                    else:
                        result = sections.act(runtime, attempt=1)
                        self.assertTrue(result["stage6_accepted"])
                x = (stage6_act.acting_plan(host_timeout=300)["memory_x_input"],)
                if timeout:
                    self.assertNotIn(x, events)
                    self.assertNotIn("inspect", events)
                    self.assertNotIn("result-visit", events)
                else:
                    self.assertEqual(events.count(x), 2)
                    self.assertLess(events.index("guard"), events.index(x))
                    self.assertEqual(events[-4:], [x, "inspect", x, "result-visit"])

    def test_visual_mismatches_retry_without_saves_visits_reads_or_an_early_x(self):
        for debug in (0, 1):
            with self.subTest(debug=debug):
                harness = self.harness(debug)
                detections = [(True, False), (False, True), (False, False), (True, True)]
                counts = {}

                def before_check(value, number):
                    if number == 1:
                        value.clock.reset_measurement()
                        counts.update(saved=len(value.saved_checkpoints), logged=len(value.log_checkpoints),
                            messages=value.clock.lua.run("return table.concat(lua_messages, '\\n')"))
                    self.assertEqual(value.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"), "1:1")
                    position, dialogue_closed = detections[number - 1]
                    value.clock.lua.run(f"pixel(1,1,{literal((121,247,215) if position else (0,0,0))}); "
                        f"pixel(128,146,{literal((121,247,215) if dialogue_closed else (255,255,255))}); return 'ready'")

                def before_read(value):
                    self.assertEqual(len(value.saved_checkpoints), counts["saved"])
                    self.assertEqual(len(value.log_checkpoints), counts["logged"])
                    self.assertEqual(value.clock.lua.run("return table.concat(lua_messages, '\\n')"), counts["messages"])
                    self.assertEqual(value.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"), "1:1")
                    self.assertEqual(value.clock.trace().index("X"), 4 * round(3 * stage6_act.FRAME_RATE))

                plans = harness.act(win=True, before_post_acting=before_check, before_result=before_read)
                self.assertFalse(any(step.startswith("X:") for step in plans[-1]["steps"]))
                checks = harness.act_post_acting
                self.assertEqual([check["pause_plan"]["phase"] for check in checks], ["retry"] * 3 + ["settle"])
                for check, detection in zip(checks, detections):
                    readiness, image = check["readiness"], check["image"]
                    self.assertEqual(readiness["position"]["confirmed"], detection[0])
                    self.assertEqual(readiness["dialogue_closed"]["confirmed"], detection[1])
                    self.assertEqual(readiness["ready"], all(detection))
                    self.assertEqual((readiness["frame"], readiness["epoch"], readiness["path"]),
                        (image["frame"], image["epoch"], image["path"]))
                    self.assertEqual((image["source"], image["direct_read_frames"]),
                        ("fresh_zero_frame_framebuffer", 0))
                trial = harness.boundary["snapshot"]["trial"]
                self.assertTrue(trial["post_acting_ready"])
                self.assertEqual((trial["post_acting_readiness_checks"], trial["post_acting_settle_frames"]),
                    (4, round(3 * stage6_act.FRAME_RATE)))
                self.assertEqual(len(harness.saved_checkpoints), counts["saved"] + debug)
                self.assertEqual(len(harness.log_checkpoints), counts["logged"] + 1 - debug)
                self.assertEqual(harness.clock.trace()[-204:], (["X"] * 12 + [""] * 90) * 2)
                self.assertEqual(harness.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"), "2:2")

    def test_first_x_is_blocked_before_the_guard_match_and_completed_settle(self):
        for phase in ("baseline", "mismatch", "matched", "settle_not_acknowledged"):
            with self.subTest(phase=phase):
                harness = self.harness()
                harness.act(finish_results=False)
                if phase != "baseline":
                    if phase == "mismatch":
                        harness.clock.lua.run("pixel(128,146,{255,255,255}); return 'ready'")
                    check = harness.call("post-acting-readiness")["observation"]
                    if phase == "settle_not_acknowledged":
                        harness.clock.command("combo", "inputs", *check["pause_plan"]["steps"])
                frame = int(harness.clock.lua.run("return tostring(frame)"))
                harness.clock.reset_measurement()
                rejected = harness.clock.command("combo", "inputs", "X:12:90")
                self.assertIn("did not authorize", rejected["error"])
                self.assertEqual(rejected["frame"], frame)
                self.assertNotIn("X", harness.clock.trace())
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
                self.assertEqual(harness.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"), "1:1")

    def test_result_memory_cannot_be_read_before_the_settled_guard_and_first_x(self):
        for settled in (False, True):
            with self.subTest(settled=settled):
                harness = self.harness()
                harness.act(finish_results=False)
                check = harness.call("post-acting-readiness")["observation"]
                if settled:
                    harness.clock.command("combo", "inputs", *check["pause_plan"]["steps"])
                    harness.call("post-acting-settled")
                frame = int(harness.clock.lua.run("return tostring(frame)"))
                harness.clock.reset_measurement()
                rejected = harness.clock.checkpoint("inspect")
                self.assertIn("post-Acting guard and open X", rejected["error"])
                self.assertEqual(rejected["frame"], frame)
                self.assertEqual(harness.clock.trace(), [])
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
                self.assertEqual(harness.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"), "1:1")
