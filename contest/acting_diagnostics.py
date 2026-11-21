"""Bounded Acting-only observations; never issue emulator commands or change deadlines."""

from collections import Counter, deque
import json
from pathlib import Path
import re
import time


SAMPLE_HOST_SECONDS = 5
MAX_SAMPLES = 64
MAX_LOG_BYTES = 512 * 1024
FINAL_RESERVE_BYTES = 128 * 1024


def _text(value):
    return re.sub(r"\b[0-9a-fA-F]{32,}\b", "[redacted]", str(value))[:160]


def _input_totals(action, arguments):
    try:
        if action == "advance":
            return int(arguments[0]), 1
        if action == "combo" and arguments[0] == "inputs":
            frames = 0
            for item in arguments[1:]:
                parts = item.split(":")
                frames += int(parts[1]) if parts[0] == "wait" else int(parts[1]) + int(parts[2])
            return frames, len(arguments) - 1
    except (IndexError, TypeError, ValueError):
        pass
    return None, None


class ActingDiagnostics:
    """One append-only log, with low-rate progress retained in memory until abnormal."""

    def __init__(self, path, run_started, *, clock=None):
        self.path, self.run_started = Path(path), run_started
        self.clock = time.monotonic if clock is None else clock
        self.enabled = self.disabled = False
        self.bytes_written = self.dropped_records = 0
        self.samples = deque(maxlen=MAX_SAMPLES)
        self.command = self.last_command = None
        self.context = {}
        self.command_number = 0
        self.cleaning = False

    def _row(self, event, now, **details):
        return {"event": event, "acting_visit": self.visit,
            "host_elapsed_seconds": round(now - self.run_started, 3),
            "acting_host_elapsed_seconds": round(now - self.started, 3),
            "acting_remaining_host_seconds": round(max(0, self.deadline - now), 3), **details}

    def _append(self, row, *, final=False):
        if self.disabled:
            return
        limit = MAX_LOG_BYTES if final else MAX_LOG_BYTES - FINAL_RESERVE_BYTES
        content = (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        if self.bytes_written + len(content) > limit:
            self.dropped_records += 1
            return
        try:
            with self.path.open("ab") as stream:
                self.bytes_written = stream.tell()
                if self.bytes_written + len(content) > limit:
                    self.dropped_records += 1
                    return
                stream.write(content)
                self.bytes_written += len(content)
        except OSError:
            self.disabled = True

    def _write(self, event, now, *, critical=False, **details):
        self._append(self._row(event, now, **details), final=critical)

    def _snapshot(self, now):
        command = self.command
        status = command.get("status") or {}
        matched = status.get("command_id") == command.get("id") and command.get("id") is not None
        progress = status.get("acting_progress") or {}
        if not matched or not isinstance(progress, dict) or progress.get("command_id") != command.get("id"):
            progress = {}
        frame, start = status.get("frame"), status.get("command_start_frame")
        expected_end = command["context"].get("end_frame")
        if expected_end is None and matched and isinstance(start, int) and command["frames"] is not None:
            expected_end = start + command["frames"]
        pause = command.get("pause")
        history = status.get("combo_history")
        context, elapsed = command["context"], progress.get("elapsed_frames")
        segment = None
        if context.get("purpose") == "round_combo" and isinstance(elapsed, int):
            prefix, menus = context.get("pre_selection_frames"), context.get("menu_frames")
            if isinstance(prefix, int) and isinstance(menus, int):
                segment = "pre_selection_b" if elapsed < prefix else "move_judge_selection" \
                    if elapsed < prefix + menus else "base_b"
        phase = "awaiting_command_acceptance" if not matched and command["phase"] == "awaiting_execution" \
            else command["phase"]
        return {"command_number": command["number"], "command_id": command.get("id"),
            "purpose": command["context"].get("purpose"), "round": command["context"].get("round"),
            "input_segment": segment,
            "wait_phase": phase, "command_host_elapsed_seconds": round(now - command["started"], 3),
            "command_id_matches": matched, "reported_command_id": _text(status.get("command_id", "")),
            "last_reported_emulator_frame": frame, "frame_source": "last_lua_status",
            "lua_command_start_frame": start if matched else None, "expected_end_frame": expected_end,
            "frame_delta": frame - start if matched and isinstance(frame, int) and isinstance(start, int) else None,
            "frames_to_expected_end": expected_end - frame
                if matched and isinstance(expected_end, int) and isinstance(frame, int) else None,
            "planned_emulator_frames": command["frames"], "planned_steps": command["steps"],
            "counted_emulator_frames": progress.get("elapsed_frames"),
            "completed_steps": len(history) if matched and isinstance(history, list) else None,
            "step": progress.get("step"), "step_elapsed_frames": progress.get("step_elapsed_frames"),
            "step_total_frames": progress.get("step_total_frames"),
            "lua_command_done": status.get("command_done") if matched else None,
            "lua_stop_reason": _text(status.get("stop_reason", "")) if matched and status.get("command_done") else None,
            "status_read_error_counts": dict(command["read_errors"]),
            "last_valid_status_read_age_host_seconds": round(now - command["valid_at"], 3)
                if command.get("valid_at") is not None else None,
            "last_observed_publication_age_host_seconds": round(now - command["publication_at"], 3)
                if command.get("publication_at") is not None else None,
            "native_pause_observed": pause["paused"] if pause else None,
            "native_pause_observation_age_host_seconds": round(now - pause["at"], 3) if pause else None,
            "native_pause_source": "existing_native_menu_check" if pause else "not_observed_during_command"}

    def _sample(self, now, *, force=False):
        if self.command is None or self.command.get("frozen"):
            return
        if not force and now < self.next_sample:
            return
        if now >= self.next_sample:
            self.next_sample = now + SAMPLE_HOST_SECONDS
        sample = self._snapshot(now)
        signature = tuple(sample[key] for key in ("command_id_matches", "last_reported_emulator_frame",
            "counted_emulator_frames", "completed_steps", "step_elapsed_frames", "lua_command_done"))
        previous = self.command.get("progress_signature")
        if previous == signature and now - self.command["progress_at"] >= SAMPLE_HOST_SECONDS:
            sample["reported_progress_unchanged_host_seconds"] = round(now - self.command["progress_at"], 3)
            self.abnormal = True
        elif previous != signature:
            self.command["progress_signature"], self.command["progress_at"] = signature, now
        self.samples.append(self._row("progress_sample", now, **sample))

    def _finish_command(self, now, *, error_type=None, error=None):
        if self.command is None:
            return
        command = self.command
        if error_type:
            self.abnormal = True
            self._sample(now, force=True)
        lua_error = (command.get("status") or {}).get("error")
        if lua_error:
            self.abnormal = True
        snapshot = command.get("frozen") or self._snapshot(now)
        snapshot = {**snapshot, "command_host_elapsed_seconds": round(now - command["started"], 3),
            "completion_report_observed": command.get("completion_at") is not None,
            "completion_report_host_elapsed_seconds": round(command["completion_at"] - self.run_started, 3)
                if command.get("completion_at") is not None else None,
            "host_pause_confirmed": command.get("returned", False),
            "outcome": "host_error" if error_type else "lua_error" if lua_error else "returned",
            "error_type": error_type, "error": _text(error or lua_error) if error or lua_error else None}
        self._write("command_outcome", now, critical=bool(error_type or lua_error), **snapshot)
        context = command["context"]
        if (context.get("retry") and not error_type and snapshot["lua_stop_reason"] == "combo_complete"
                and snapshot["command_id_matches"]
                and snapshot["last_reported_emulator_frame"] == context.get("end_frame")):
            self.retries[context["guard"]]["completed_retry_segments"] += 1
            self.retries[context["guard"]]["completed_retry_frames"] += command["frames"]
        self.last_command, self.command = snapshot, None

    def _checkpoint(self, action, arguments, packet):
        if action in ("round-readiness", "post-acting-readiness"):
            observation = packet["observation"]
            readiness, pause = observation["readiness"], observation["pause_plan"]
            number = int(arguments[0]) if arguments else None
            guard = f"round-{number}" if number is not None else "post-acting"
            counts = self.retries.setdefault(guard, {"checks": 0, "mismatches": 0,
                "completed_retry_segments": 0, "completed_retry_frames": 0})
            counts["checks"] += 1
            counts["mismatches"] += not readiness["ready"]
            purpose = "readiness_retry" if not readiness["ready"] else "matched_round_paused" \
                if number is not None else "post_acting_settle"
            self.context = {"purpose": purpose,
                "round": number, "guard": guard, "retry": not readiness["ready"],
                "start_frame": pause["start_frame"], "end_frame": pause["end_frame"]}
        elif action == "round-visit":
            snapshot = (packet.get("checkpoint") or {}).get("snapshot") or {}
            plan = snapshot.get("plan") or {}
            self.context = {"purpose": "round_combo", "round": int(arguments[0]),
                **{key: plan.get(key) for key in ("start_frame", "end_frame", "pre_selection_frames", "menu_frames",
                    "mash_frames", "move", "judge")}}
        elif action == "begin-attempt":
            self.context = {"purpose": "render_preparation"}
        elif action == "post-acting-settled":
            self.context = {"purpose": "result_menu_open"}
        elif action == "inspect":
            self.context = {"purpose": "result_menu_close"}
        elif action == "result-visit":
            self.context = {"purpose": "result_checkpoint"}

    def __call__(self, event, details):
        if self.disabled:
            return
        now = self.clock()
        if event == "visit_begin":
            self.enabled, self.cleaning = True, False
            self.visit, self.started, self.deadline = details["visit"], details["started"], details["deadline"]
            self.samples.clear()
            self.command = self.last_command = self.last_checkpoint = None
            self.context, self.retries = {}, {}
            self.abnormal = self.ended = self.failed = False
            self.next_sample = self.started + SAMPLE_HOST_SECONDS
            self.pause_toggles, self.deadline_pause_toggles = 0, None
            self._write("acting_visit_begin", now, host_budget_seconds=round(self.deadline - self.started, 3),
                sample_host_seconds=SAMPLE_HOST_SECONDS, max_buffered_samples=MAX_SAMPLES,
                max_log_bytes=MAX_LOG_BYTES)
            return
        if not self.enabled:
            return
        if event == "section_setup":
            config = details["config"]
            target = config.get("target") or {}
            self._write("acting_setup", now, attempt=config.get("attempt"), category=target.get("category"),
                rank=target.get("rank"), source_frame=config.get("source_frame"))
        elif event == "checkpoint_request":
            self.last_checkpoint = {"action": details["action"], "arguments": list(details["arguments"])[:1],
                "host_elapsed_seconds": round(now - self.run_started, 3), "response_observed": False}
        elif event == "checkpoint":
            if self.last_checkpoint:
                self.last_checkpoint["response_observed"] = True
            self._checkpoint(details["action"], details["arguments"], details["packet"])
        elif event == "checkpoint_accepted":
            checkpoint = details["packet"]["checkpoint"]
            if checkpoint.get("checkpoint") == "stage-6-round":
                plan = (checkpoint.get("snapshot") or {}).get("plan") or {}
                self._write("round_plan_accepted", now, round=plan.get("round"), move=plan.get("move"),
                    judge=plan.get("judge"), start_frame=plan.get("start_frame"), end_frame=plan.get("end_frame"),
                    pre_selection_frames=plan.get("pre_selection_frames"), menu_frames=plan.get("menu_frames"),
                    mash_frames=plan.get("mash_frames"))
        elif event == "command_begin":
            frames, steps = _input_totals(details["action"], details["arguments"])
            self.command_number += 1
            self.command = {"number": self.command_number, "started": now, "id": None,
                "context": dict(self.context), "frames": frames, "steps": steps, "read_errors": Counter(),
                "phase": "preparing_command", "status": None}
        elif event == "command_issued":
            self.command["id"], self.command["phase"] = details["command_id"], "awaiting_execution"
            self._write("command_issued", now, command_number=self.command["number"],
                command_id=details["command_id"], action=details["action"],
                planned_emulator_frames=self.command["frames"], planned_steps=self.command["steps"],
                **self.command["context"])
        elif event in ("status", "status_read_error"):
            if self.command is None or self.command.get("frozen"):
                return
            if event == "status_read_error":
                self.command["read_errors"][details["error_type"]] += 1
            else:
                status = details["status"]
                self.command["status"], self.command["valid_at"] = status, now
                publication = (status.get("command_id"), status.get("acting_publication"),
                    status.get("frame"), status.get("command_done"))
                if publication != self.command.get("publication"):
                    self.command["publication"], self.command["publication_at"] = publication, now
            self._sample(now)
        elif event == "completion_reported":
            self.command["completion_at"], self.command["phase"] = now, "awaiting_native_pause"
            status = self.command.get("status") or {}
            self._write("completion_reported", now, command_number=self.command["number"],
                command_id=self.command["id"], frame=status.get("frame"),
                stop_reason=_text(status.get("stop_reason", "")), wait_phase="awaiting_native_pause")
        elif event == "command_returned":
            self.command["returned"] = True
            self._finish_command(now)
        elif event == "deadline_expired":
            self.abnormal = self.failed = True
            self._sample(now, force=True)
            self.command["frozen"] = {**self._snapshot(now),
                "observations_frozen_host_elapsed_seconds": round(now - self.run_started, 3)}
            self.command["phase"] = "enforcing_timeout_pause"
            self.deadline_pause_toggles = self.pause_toggles
            self._write("host_deadline_expired", now, critical=True, **self.command["frozen"])
        elif event == "command_failed":
            self.failed = True
            self._finish_command(now, error_type=details["error_type"], error=details.get("error"))
        elif event == "pause_toggle_queued":
            self.pause_toggles += 1
            if self.command is not None and not details["wanted"]:
                self.command["pause"] = None
            self._write("host_pause_toggle_queued", now, critical=self.failed, wanted=details["wanted"],
                command_id=self.command.get("id") if self.command else None,
                control_phase="cleanup" if self.cleaning else self.command["phase"] if self.command else "checkpoint")
        elif event == "pause_observed":
            if self.command is not None and not self.command.get("frozen"):
                self.command["pause"] = {"paused": details["paused"], "at": now}
                self._sample(now)
            if self.cleaning or self.command is not None and self.command.get("frozen"):
                self._write("existing_pause_check", now, critical=self.failed, paused=details["paused"],
                    source="existing_native_menu_check", control_phase="cleanup" if self.cleaning else "timeout_pause",
                    host_pause_toggles_since_deadline=self.pause_toggles - self.deadline_pause_toggles
                        if self.deadline_pause_toggles is not None else None)
        elif event == "pause_requested":
            if self.cleaning or self.command is not None and self.command.get("frozen"):
                self._write("host_pause_requested", now, critical=self.failed, wanted=details["wanted"],
                    control_phase="cleanup" if self.cleaning else "timeout_pause")
        elif event == "visit_end":
            if self.ended:
                return
            self.ended = True
            if details.get("error"):
                self.abnormal = self.failed = True
            if self.abnormal:
                for sample in self.samples:
                    self._append(sample, final=self.failed)
            self._write("acting_visit_end", now, critical=self.failed, outcome=details["outcome"],
                error=_text(details["error"]) if details.get("error") else None,
                abnormal_progress_observed=self.abnormal, buffered_samples=len(self.samples),
                readiness=dict(self.retries), last_checkpoint=self.last_checkpoint,
                last_command=self.last_command, dropped_records=self.dropped_records)
        elif event == "cleanup_begin":
            self.cleaning = True
        elif event == "cleanup_end":
            result = details["result"]
            self._append(self._row("existing_cleanup_endpoint", now, frame=result.get("frame"),
                emulator_paused=result.get("emulator_paused"), inputs_released=result.get("inputs_released"),
                source="existing_cleanup_confirmation", dropped_records=self.dropped_records,
                error=_text(details["error"]) if details.get("error") else None), final=True)
            self.enabled = False
