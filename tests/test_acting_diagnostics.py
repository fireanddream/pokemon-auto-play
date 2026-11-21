"""Acting observations stay bounded and cannot change input, pause, or abort behavior."""

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from contest.acting_diagnostics import ActingDiagnostics, MAX_LOG_BYTES, MAX_SAMPLES
from contest.runtime import ContestRuntime
from contest.super_contest import chain_plan
from host.controller import Controller, ControllerConfig
from tests.support.lua51 import literal
from tests.support.paths import ROOT, temporary_directory
from tests.test_contest_runtime import ContestClock


class HostClock:
    def __init__(self):
        self.seconds = 0

    def __call__(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def publication(frame, *, done=False, identifier="1", start=100):
    elapsed = frame - start
    completed = elapsed // 30
    return {"command_id": identifier, "command_done": done, "frame": frame,
        "command_start_frame": start, "stop_reason": "combo_complete" if done else "", "error": "",
        "acting_publication": frame, "acting_progress": {"command_id": identifier, "elapsed_frames": elapsed,
            "step": min(completed + 1, 2), "step_elapsed_frames": 30 if done else elapsed % 30,
            "step_total_frames": 30},
        "combo_history": [{"elapsed_frames": 30} for _ in range(completed)]}


def begin(logger, *, round_number=2, start=100, end=160):
    logger("visit_begin", {"visit": 21, "started": 0, "deadline": 300})
    plan = {"round": round_number, "move": 2, "judge": 1, "start_frame": start, "end_frame": end,
        "pre_selection_frames": 0, "menu_frames": 30, "mash_frames": end - start - 30}
    logger("checkpoint", {"action": "round-visit", "arguments": (round_number,),
        "packet": {"checkpoint": {"snapshot": {"plan": plan}}}})


def issue(logger, steps=("A:10:20", "B:10:20")):
    logger("command_begin", {"action": "combo", "arguments": ["inputs", *steps]})
    logger("command_issued", {"command_id": "1", "action": "combo"})
    logger("pause_toggle_queued", {"wanted": False})


class DiagnosticStorageChecks(unittest.TestCase):
    def logger(self, work):
        clock = HostClock()
        return ActingDiagnostics(work / "acting-diagnostics.jsonl", 0, clock=clock), clock

    def test_normal_progress_is_sampled_at_five_host_seconds_and_not_written(self):
        with temporary_directory() as folder:
            work = Path(folder)
            logger, clock = self.logger(work)
            logger("command_begin", {"action": "advance", "arguments": ["10"]})
            self.assertFalse(logger.path.exists())
            begin(logger)
            issue(logger)
            initial_bytes = logger.path.stat().st_size
            for index in range(241):
                clock.seconds = index / 20
                logger("status", {"status": publication(100 + index // 4)})
            self.assertEqual([sample["acting_host_elapsed_seconds"] for sample in logger.samples], [5, 10])
            self.assertEqual(logger.path.stat().st_size, initial_bytes)
            logger("status", {"status": publication(160, done=True)})
            logger("completion_reported", {})
            logger("pause_observed", {"paused": True})
            logger("command_returned", {})
            logger("visit_end", {"outcome": "pass"})
            rows = records(logger.path)
            self.assertFalse(any(row["event"] == "progress_sample" for row in rows))
            outcome = next(row for row in rows if row["event"] == "command_outcome")
            self.assertEqual(outcome["counted_emulator_frames"], 60)
            self.assertTrue(outcome["completion_report_observed"] and outcome["host_pause_confirmed"])
            self.assertEqual(outcome["frames_to_expected_end"], 0)
            self.assertEqual(list(work.iterdir()), [logger.path])

    def test_stalled_progress_is_bounded_and_cleanup_does_not_replace_timeout_evidence(self):
        with temporary_directory() as folder:
            logger, clock = self.logger(Path(folder))
            begin(logger)
            issue(logger)
            for seconds in range(0, 401, 5):
                clock.seconds = seconds
                logger("status", {"status": publication(130)})
            self.assertEqual(len(logger.samples), MAX_SAMPLES)
            logger("deadline_expired", {})
            logger("pause_requested", {"wanted": True})
            logger("pause_observed", {"paused": True})
            logger("command_failed", {"error_type": "TimeoutError", "error": "timeout"})
            logger("visit_end", {"outcome": "timeout", "error": "timeout"})
            logger("cleanup_begin", {})
            logger("pause_observed", {"paused": True})
            logger("cleanup_end", {"result": {"frame": 131, "emulator_paused": True, "inputs_released": True},
                "error": "timeout"})
            rows = records(logger.path)
            samples = [row for row in rows if row["event"] == "progress_sample"]
            self.assertEqual(len(samples), MAX_SAMPLES)
            self.assertTrue(any(row.get("reported_progress_unchanged_host_seconds") for row in samples))
            outcome = next(row for row in rows if row["event"] == "command_outcome")
            self.assertEqual((outcome["last_reported_emulator_frame"], outcome["frames_to_expected_end"]), (130, 30))
            self.assertIsNone(outcome["native_pause_observed"])
            self.assertFalse(outcome["completion_report_observed"] or outcome["host_pause_confirmed"])
            pause = next(row for row in rows if row["event"] == "existing_pause_check")
            self.assertTrue(pause["paused"])
            self.assertEqual(pause["host_pause_toggles_since_deadline"], 0)
            self.assertEqual(rows[-1]["frame"], 131)
            self.assertLessEqual(logger.path.stat().st_size, MAX_LOG_BYTES)

    def test_wrong_ids_and_read_errors_are_logged_without_claiming_completion(self):
        with temporary_directory() as folder:
            logger, clock = self.logger(Path(folder))
            begin(logger)
            issue(logger)
            clock.seconds = 5
            logger("status", {"status": publication(160, done=True, identifier="old")})
            for error_type in ("JSONDecodeError", "JSONDecodeError", "PermissionError"):
                clock.seconds += 5
                logger("status_read_error", {"error_type": error_type})
            logger("deadline_expired", {})
            logger("command_failed", {"error_type": "TimeoutError", "error": "timeout"})
            logger("visit_end", {"outcome": "timeout", "error": "timeout"})
            outcome = next(row for row in records(logger.path) if row["event"] == "command_outcome")
            self.assertFalse(outcome["command_id_matches"] or outcome["completion_report_observed"])
            self.assertIsNone(outcome["counted_emulator_frames"])
            self.assertEqual(outcome["reported_command_id"], "old")
            self.assertEqual(outcome["status_read_error_counts"], {"JSONDecodeError": 2, "PermissionError": 1})

    def test_disk_cap_reserves_the_final_summary_and_write_failure_is_nonfatal(self):
        with temporary_directory() as folder:
            work = Path(folder)
            logger, clock = self.logger(work)
            with patch("contest.acting_diagnostics.MAX_LOG_BYTES", 4096), \
                    patch("contest.acting_diagnostics.FINAL_RESERVE_BYTES", 512):
                begin(logger)
                for _ in range(30):
                    logger._write("bounded_test", 0, text="diagnostic storage sample " * 20)
                logger("cleanup_end", {"result": {"frame": 100, "emulator_paused": True}, "error": None})
                self.assertLessEqual(logger.path.stat().st_size, 4096)
                self.assertGreater(logger.dropped_records, 0)
                self.assertEqual(records(logger.path)[-1]["event"], "existing_cleanup_endpoint")
            failed = ActingDiagnostics(work / "absent" / "acting-diagnostics.jsonl", 0, clock=clock)
            begin(failed)
            issue(failed)
            failed("status", {"status": publication(160, done=True)})
            failed("completion_reported", {})
            failed("pause_observed", {"paused": True})
            failed("command_returned", {})
            failed("visit_end", {"outcome": "pass"})
            self.assertTrue(failed.disabled)
            self.assertFalse((work / "absent").exists())

    def test_failure_trace_survives_a_full_routine_log(self):
        with temporary_directory() as folder:
            logger, clock = self.logger(Path(folder))
            with patch("contest.acting_diagnostics.MAX_LOG_BYTES", 32 * 1024), \
                    patch("contest.acting_diagnostics.FINAL_RESERVE_BYTES", 16 * 1024):
                begin(logger)
                issue(logger)
                for _ in range(40):
                    logger._write("routine_storage_test", 0, text="bounded routine diagnostic text " * 30)
                self.assertGreater(logger.dropped_records, 0)
                for seconds in (5, 10):
                    clock.seconds = seconds
                    logger("status", {"status": publication(130)})
                logger("deadline_expired", {})
                logger("command_failed", {"error_type": "TimeoutError", "error": "timeout"})
                logger("visit_end", {"outcome": "timeout", "error": "timeout"})
                logger("cleanup_end", {"result": {"frame": 130, "emulator_paused": True}, "error": "timeout"})
                rows = records(logger.path)
                self.assertTrue(any(row["event"] == "progress_sample" for row in rows))
                self.assertTrue(any(row["event"] == "acting_visit_end" for row in rows))
                self.assertEqual(rows[-1]["event"], "existing_cleanup_endpoint")
                self.assertLessEqual(logger.path.stat().st_size, 32 * 1024)

    def test_retry_counts_do_not_include_matched_pre_selection_delays(self):
        with temporary_directory() as folder:
            logger, clock = self.logger(Path(folder))
            logger("visit_begin", {"visit": 1, "started": 0, "deadline": 300})
            logger("checkpoint", {"action": "round-readiness", "arguments": (3,), "packet": {"observation": {
                "readiness": {"ready": False}, "pause_plan": {"start_frame": 100, "end_frame": 160}}}})
            issue(logger, ("B:10:20", "B:10:20"))
            logger("status", {"status": publication(160, done=True)})
            logger("completion_reported", {})
            logger("pause_observed", {"paused": True})
            logger("command_returned", {})
            logger("checkpoint", {"action": "round-readiness", "arguments": (3,), "packet": {"observation": {
                "readiness": {"ready": True}, "pause_plan": {"start_frame": 160, "end_frame": 220}}}})
            plan = {"round": 3, "start_frame": 160, "end_frame": 220,
                "pre_selection_frames": 30, "menu_frames": 30, "mash_frames": 0}
            logger("checkpoint", {"action": "round-visit", "arguments": (3,),
                "packet": {"checkpoint": {"snapshot": {"plan": plan}}}})
            issue(logger, ("B:10:20", "A:10:20"))
            logger("status", {"status": publication(220, start=160, done=True)})
            logger("completion_reported", {})
            logger("pause_observed", {"paused": True})
            logger("command_returned", {})
            self.assertEqual(logger.retries["round-3"], {"checks": 2, "mismatches": 1,
                "completed_retry_segments": 1, "completed_retry_frames": 60})


class ScriptedBridge:
    """Only a synthetic window and mailbox; the production host transport is unchanged."""

    def __init__(self, work, mode, observer=None):
        self.clock, self.mode, self.paused = HostClock(), mode, True
        self.controls, self.reads = [], 0
        api = SimpleNamespace(send=self.send, post_menu=self.post_menu,
            USER32=SimpleNamespace(GetMenu=lambda _: 12, GetMenuState=lambda *_: 8 if self.paused else 0))
        self.controller = Controller(ControllerConfig(root=work, mailbox_dir=work), windows_api=api)
        self.controller.session = Mock(return_value={"pid": 7, "window": 11})
        self.controller.STATUS = self
        self.controller.bridge_observer = observer

    def send(self, window, message, wparam=0, lparam=0, **options):
        self.controls.append(("send", window, message, wparam, lparam))
        return 0

    def post_menu(self, window, identifier):
        self.controls.append(("menu", window, identifier))
        self.paused = not self.paused

    def read_text(self, *, encoding):
        self.reads += 1
        if self.mode == "read_errors" and self.reads <= 2:
            return "{"
        if self.mode == "manual_pause":
            elapsed = min(int(self.clock.seconds * 30), 30)
            if elapsed == 30:
                self.paused = True
            item = publication(100 + elapsed)
        else:
            elapsed = min(int(self.clock.seconds * 60), 60)
            done = elapsed == 60
            if done and self.mode != "pause_not_confirmed":
                self.paused = True
            item = publication(100 + elapsed, done=done)
        return json.dumps(item)

    def run(self):
        with patch("host.controller.time.monotonic", self.clock), \
                patch("host.controller.time.time_ns", return_value=1), \
                patch("host.controller.time.sleep", self.clock.sleep):
            try:
                result = self.controller.bridge_command("combo", ["inputs", "A:10:20", "B:10:20"],
                    None, deadline=2)
                return result
            except TimeoutError:
                return "timeout"


class DiagnosticTransportChecks(unittest.TestCase):
    def test_enabled_logging_preserves_control_calls_poll_count_and_timeout_results(self):
        for mode in ("normal", "manual_pause", "pause_not_confirmed", "read_errors"):
            with self.subTest(mode=mode), temporary_directory() as folder:
                work = Path(folder)
                baseline_work, logged_work = work / "baseline", work / "logged"
                baseline_work.mkdir()
                logged_work.mkdir()
                baseline = ScriptedBridge(baseline_work, mode)
                baseline_result = baseline.run()
                observed = ScriptedBridge(logged_work, mode)
                logger = ActingDiagnostics(logged_work / "acting-diagnostics.jsonl", 0, clock=observed.clock)
                begin(logger)
                observed.controller.bridge_observer = logger
                logged_result = observed.run()
                logger("visit_end", {"outcome": "timeout" if logged_result == "timeout" else "pass",
                    "error": "timeout" if logged_result == "timeout" else None})
                self.assertEqual(logged_result, baseline_result)
                self.assertEqual(observed.controls, baseline.controls)
                self.assertEqual((observed.reads, observed.clock.seconds), (baseline.reads, baseline.clock.seconds))
                self.assertEqual(observed.controller.COMMAND.read_text(), baseline.controller.COMMAND.read_text())
                if mode == "manual_pause":
                    self.assertEqual([item for item in observed.controls if item[0] == "menu"], [("menu", 11, 113)])
                    checks = [row for row in records(logger.path) if row["event"] == "existing_pause_check"]
                    self.assertTrue(checks[0]["paused"])
                    self.assertEqual(checks[0]["host_pause_toggles_since_deadline"], 0)

    def test_observer_exceptions_cannot_change_successful_execution(self):
        with temporary_directory() as folder:
            bridge = ScriptedBridge(Path(folder), "normal", observer=Mock(side_effect=RuntimeError("logging failed")))
            result = bridge.run()
            self.assertTrue(result["command_done"] and result["emulator_paused"])
            self.assertEqual([item for item in bridge.controls if item[0] == "menu"], [("menu", 11, 113)])

    def test_runtime_enables_diagnostics_only_for_acting_without_native_calls(self):
        with temporary_directory() as folder:
            work = Path(folder)
            controller = SimpleNamespace(live_deadline=None, remaining=lambda: 300,
                paused=Mock(), set_pause=Mock())
            runtime = ContestRuntime("diagnostic-scope", work, chain_plan(debug=0), 0, controller=controller)
            runtime.acting_diagnostics.clock = lambda: 0
            with patch("contest.runtime.time.monotonic", return_value=0):
                runtime.begin_visit(1, 1)
                self.assertIsNone(controller.bridge_observer)
                self.assertFalse(runtime.acting_diagnostics.path.exists())
                runtime.begin_visit(6, 1)
                self.assertIs(controller.bridge_observer, runtime.acting_diagnostics)
                runtime.begin_visit(2, 1)
                self.assertIsNone(controller.bridge_observer)
            controller.paused.assert_not_called()
            controller.set_pause.assert_not_called()
            self.assertEqual(runtime.originals, {})
            self.assertFalse(runtime.settings["save_policy"]["enabled"])


class DiagnosticLuaChecks(unittest.TestCase):
    def harness(self):
        harness = ContestClock(debug=0)
        self.addCleanup(harness.close)
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        harness.act(rounds=0)
        harness.call("round-readiness", 1)
        harness.call("round-ready", 1)
        plan = harness.acknowledge(harness.call("round-visit", 1))["checkpoint"]["snapshot"]["plan"]
        harness.clock.reset_measurement()
        harness.clock.lua.run(f"""
            capture_frames = {{}}
            command_text = '700|combo|inputs|' .. table.concat({literal(plan['steps'])}, '|')
            mock_paused = false
            for index = 1, 45 do tick() end
            return 'ready'
        """)
        return harness, plan

    def test_progress_fields_use_existing_publications_without_extra_frames_or_captures(self):
        harness, plan = self.harness()
        packet = json.loads(harness.clock.lua.run("return latest_status"))
        progress = packet["acting_progress"]
        self.assertEqual(progress["command_id"], "700")
        self.assertEqual(progress["elapsed_frames"], packet["frame"] - plan["start_frame"])
        self.assertGreaterEqual(progress["elapsed_frames"], 30)
        self.assertLessEqual(progress["elapsed_frames"], 45)
        self.assertEqual((len(harness.clock.trace()), harness.clock.pauses()), (45, 0))
        self.assertEqual(harness.clock.lua.run("return tostring(#capture_frames)"), "0")
        packet = json.loads(harness.clock.lua.run(f"""
            for index = 46, {plan['total_frames']} do tick() end
            return latest_status
        """))
        self.assertTrue(packet["command_done"])
        self.assertEqual(packet["stop_reason"], "combo_complete")
        self.assertEqual(packet["frame"], plan["end_frame"])
        self.assertEqual(packet["acting_progress"]["elapsed_frames"], plan["total_frames"])
        self.assertEqual(len(packet["combo_history"]), len(plan["steps"]))
        self.assertEqual((len(harness.clock.trace()), harness.clock.pauses()), (plan["total_frames"], 1))
        self.assertEqual(harness.clock.lua.run("return tostring(#capture_frames)"), "1")
        registrations = json.loads(harness.clock.lua.run(
            f"return dofile({literal((ROOT / 'lua/json.lua').as_posix())}).encode(registrations)"))
        self.assertEqual(registrations, {"registerbefore": 1, "registerafter": 1, "registermenustart": 1})

    def test_abort_from_manual_pause_releases_inputs_without_finishing_pending_frames(self):
        harness, plan = self.harness()
        before = int(harness.clock.lua.run("mock_paused = true; return tostring(frame)"))
        result = harness.clock.checkpoint("abort", "timeout")
        harness.clock.checkpoint("terminal")
        self.assertEqual(result["frame"], before)
        self.assertLess(before, plan["end_frame"])
        self.assertEqual(harness.clock.lua.run("return tostring(frame) .. ':' .. tostring(mock_paused)"),
            f"{before}:true")
        packet = json.loads(harness.clock.lua.run("return latest_status"))
        self.assertEqual(packet["buttons"], "")
        self.assertEqual(packet["stop_reason"], "terminal")
        self.assertEqual(len(harness.clock.trace()), 45)


if __name__ == "__main__":
    unittest.main(verbosity=2)
