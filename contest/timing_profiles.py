"""Readable regional gameplay timings, selected once and passed to stage plans."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from contest.platinum import GAME_CODES


PROFILE_DIRECTORY = Path(__file__).with_suffix("")
FRAME_RATE = 67027965 / 1120380
SCALAR_SECTIONS = {
    "stage1_loading": ("boot_wait_frames", "button_hold_frames", "start_release_frames",
        "load_save_a_release_frames", "dismiss_prompt_b_release_frames"),
    "stage2_preparation": ("menu_hold_frames", "menu_release_frames", "bag_down_release_frames",
        "give_confirmation_release_frames"),
    "stage3_entry": ("menu_hold_frames", "menu_release_frames", "intro_a_mash_game_seconds",
        "intro_mash_hold_frames", "intro_mash_release_frames"),
    "stage4_dress_up": ("accessory_budget_game_seconds", "accessory_pickup_hold_frames",
        "accessory_drag_frames", "accessory_release_frames", "page_tap_hold_frames",
        "page_tap_release_frames", "page_render_wait_frames", "finish_tap_hold_frames",
        "finish_tap_release_frames", "loaded_entry_render_frames", "dance_transition_a_mash_game_seconds",
        "transition_mash_hold_frames", "transition_mash_release_frames", "dance_entry_poll_game_seconds"),
    "stage5_dancing": ("response_hold_frames", "feedback_release_frames", "input_offset_frames",
        "demo_sample_interval_frames", "native_overlay_settle_host_seconds",
        "acting_transition_b_mash_game_seconds", "transition_mash_hold_frames", "transition_mash_release_frames"),
    "stage6_acting": ("menu_hold_frames", "menu_release_frames", "rounds_1_to_3_b_mash_game_seconds",
        "final_round_and_results_b_mash_game_seconds", "mash_hold_frames", "mash_release_frames",
        "handoff_render_frames", "loaded_state_render_host_seconds"),
    "standalone_dancing": ("first_handoff_offset_frames", "loaded_source_refresh_frames"),
    "diagnostics": ("entry_probe_first_capture_game_seconds", "entry_probe_capture_interval_game_seconds",
        "dance_entry_poll_game_seconds", "dance_capture_interval_game_seconds", "rank_state_render_host_seconds",
        "rank_measurement_host_seconds", "pointer_calibration_frames", "pointer_sample_min_wait_frames",
        "pointer_sample_max_dance_fraction"),
}
METRIC_SECTIONS = {
    "fixed_dance_metrics": (("Normal/Super", "Cool", "Beauty", "Cute", "Smart", "Tough"),
        ("pointer_speed_numerator", "pointer_speed_denominator", "same_leader_gap_frames", "leader_change_gap_frames",
         "cue_side_offset_pixels")),
}
NONNEGATIVE = {"page_render_wait_frames", "native_overlay_settle_host_seconds",
               "same_leader_gap_frames", "leader_change_gap_frames"}


def _keys(value, expected, label, *, help_text=False):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    actual = set(value) - ({"_help"} if help_text else set())
    missing, unknown = set(expected) - actual, actual - set(expected)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise ValueError(f"{label}: {'; '.join(details)}.")
    if help_text and "_help" in value and not isinstance(value["_help"], str):
        raise ValueError(f"{label}._help must be text.")


def _numbers(value, keys, label):
    _keys(value, keys, label, help_text=True)
    for key in keys:
        number = value[key]
        field = f"{label}.{key}"
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError(f"{field} must be a finite number.")
        if key.endswith(("_frames", "_pixels")) and not isinstance(number, int):
            unit = "native-pixel" if key.endswith("_pixels") else "emulator-frame"
            raise ValueError(f"{field} must be a whole {unit} count.")
        if key == "input_offset_frames":
            continue
        invalid = number < 0 if key in NONNEGATIVE else number <= 0
        if invalid:
            raise ValueError(f"{field} must be {'nonnegative' if key in NONNEGATIVE else 'positive'}.")
        if key.endswith("_game_seconds") and round(number * FRAME_RATE) < 1:
            raise ValueError(f"{field} must cover at least one emulator frame.")
        if key == "pointer_sample_max_dance_fraction" and number >= 1:
            raise ValueError(f"{field} must be less than one complete dance.")
        if key == "cue_side_offset_pixels" and number >= 256:
            raise ValueError(f"{field} must fit the native 256-pixel screen.")


def validate_profile(document, game_code):
    _keys(document, ("schema", "game_code", "name", "sections"), game_code)
    if type(document["schema"]) is not int or document["schema"] != 1:
        raise ValueError(f"{game_code}: timing profile schema must be 1.")
    if document["game_code"] != game_code or not isinstance(document["name"], str) or not document["name"].strip():
        raise ValueError(f"{game_code}: supply its matching game code and a readable profile name.")
    groups = document["sections"]
    _keys(groups, (*SCALAR_SECTIONS, *METRIC_SECTIONS), f"{game_code}.sections")
    for name, keys in SCALAR_SECTIONS.items():
        _numbers(groups[name], keys, f"{game_code}.{name}")
    for name, (families, keys) in METRIC_SECTIONS.items():
        _keys(groups[name], families, f"{game_code}.{name}", help_text=True)
        for family in families:
            metric = groups[name][family]
            _numbers(metric, keys, f"{game_code}.{name}.{family}")
            if name == "fixed_dance_metrics" and metric["leader_change_gap_frames"] < metric["same_leader_gap_frames"]:
                raise ValueError(f"{game_code}.{name}.{family}: leader-change gap must cover the same-leader gap.")
            speed = metric["pointer_speed_numerator"] / metric["pointer_speed_denominator"]
            if not math.isfinite(speed) or speed <= 0:
                raise ValueError(f"{game_code}.{name}.{family}: pointer speed must be finite and positive.")
            if not math.isfinite(240 / speed) or math.floor(120 / speed) < 1:
                raise ValueError(f"{game_code}.{name}.{family}: visible response must cover at least one frame.")
    return document


def load_profile(game_code="CPUJ"):
    if game_code not in GAME_CODES:
        raise ValueError("Select a Japanese (CPUJ) or English (CPUE) Platinum timing profile.")
    return _read_profile(game_code)


@lru_cache(maxsize=len(GAME_CODES))
def _read_profile(game_code):
    path = PROFILE_DIRECTORY / f"{game_code}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        validate_profile(document, game_code)
    except (OSError, ValueError) as error:
        raise ValueError(f"Timing profile {path.name}: {error}") from error
    return {**document, "source": f"contest/timing_profiles/{path.name}"}


def section(profile, name):
    return (load_profile() if profile is None else profile)["sections"][name]


def setting(override, values, name):
    """Only an explicit override replaces the selected profile, including zero values."""
    return values[name] if override is None else override


def check_profiles():
    for code in sorted(GAME_CODES):
        load_profile(code)
    print(f"Regional timing profile schemas OK: {len(GAME_CODES)} ROM codes; JPN CPUJ and English CPUE (USA/EU).")
