"""Fixed category/rank dance metrics and retained standalone timing profiles."""

import json
import math
from pathlib import Path


RANKS = ("Normal", "Super", "Hyper", "Master")
CATEGORIES = ("Cool", "Beauty", "Cute", "Smart", "Tough")
ALIASES = {"normal": "Normal", "super": "Super", "great": "Super", "hyper": "Hyper", "ultra": "Hyper", "master": "Master"}
PROFILE_PATH = Path(__file__).with_suffix(".json")
POINTER_CALIBRATION_FRAMES = 60
VISIBLE_HALF_PIXELS = 120
DEFAULT_OFFSET_FRAMES = -7

# Speed is pixels per emulator frame; gaps run from x=248 to the next x=8.
FIXED_METRICS = {
    "Normal/Super": (8 / 15, 30, 269),
    "Cool": (5 / 9, 0, 216),
    "Beauty": (1 / 3, 0, 357),
    "Cute": (2 / 3, 24, 214),
    "Smart": (7 / 12, 29, 245),
    "Tough": (5 / 8, 28, 232),
}


def canonical_rank(rank):
    if not isinstance(rank, str) or rank.lower() not in ALIASES:
        raise ValueError("Select Normal, Super/Great, Hyper/Ultra, or Master.")
    return ALIASES[rank.lower()]


def fixed_cue_spacing(category, rank):
    """Measured native cue separation for main and debug dancing."""
    rank = canonical_rank(rank)
    if category not in CATEGORIES:
        raise ValueError("Select Cool, Beauty, Cute, Smart, or Tough.")
    return 120 if category in ("Cool", "Beauty") and rank in RANKS[2:] else 128


def all_timings():
    document = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if document.get("schema") != 1 or set(document.get("profiles", {})) != set(RANKS):
        raise ValueError("The dance timing file must contain all four contest ranks.")
    result = {}
    for rank in RANKS:
        profile = document["profiles"][rank]
        cues, offset = profile["cues_per_dance"], profile["cue_side_offset_pixels"]
        if isinstance(cues, bool) or not isinstance(cues, int) or cues != (3 if rank == "Normal" else 4):
            raise ValueError(f"{rank} needs its rank-specific cue count.")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 < offset < 256:
            raise ValueError(f"{rank} needs a positive native cue-side pixel offset.")
        speed = profile["pixels_per_frame"]
        if isinstance(speed, bool) or not math.isfinite(speed) or speed <= 0:
            raise ValueError(f"{rank} needs a positive pointer speed.")
        for field in ("cycle_frames", "extra_switch_frames", "response_frames"):
            value = profile[field]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{rank}.{field} must be a positive emulator-frame count.")
        if profile["response_frames"] >= profile["cycle_frames"]:
            raise ValueError(f"{rank} needs a response shorter than its complete dance cycle.")
        result[rank] = {"rank": rank, **profile, "frames_per_pixel": (120 / speed) / 120,
            "visible_half_frames": 120 / speed,
            "lead_dances_frames": 2 * profile["cycle_frames"] + profile["extra_switch_frames"],
            "profile_source": "contest/dance_timings.json"}
    return result


def timing_for(rank):
    return all_timings()[canonical_rank(rank)]


def fixed_timing(category, rank, *, metrics=None):
    """Resolve base or caller-supplied metrics without an entry speed probe."""
    if category not in CATEGORIES:
        raise ValueError("Select Cool, Beauty, Cute, Smart, or Tough.")
    native = timing_for(rank)
    rank = native["rank"]
    family = "Normal/Super" if rank in RANKS[:2] else category
    speed, dance_gap, leader_gap = (FIXED_METRICS if metrics is None else metrics)[family]
    cycle, extra = math.floor(240 / speed + dance_gap), leader_gap - dance_gap
    return {"category": category, "rank": rank, "family": family,
        "cues_per_dance": native["cues_per_dance"], "cue_side_offset_pixels": fixed_cue_spacing(category, rank),
        "pixels_per_frame": speed, "frames_per_pixel": 1 / speed, "visible_half_frames": 120 / speed,
        "dance_gap_frames": dance_gap, "leader_gap_frames": leader_gap,
        "cycle_frames": cycle, "extra_switch_frames": extra, "response_frames": math.floor(120 / speed),
        "lead_dances_frames": 2 * cycle + extra, "profile_source": "contest/dance_timings.py:FIXED_METRICS",
        "timing_source": "fixed_normalized_contest_measurements", "derived_frame_rounding": "floor"}


def all_fixed_timings():
    return {category: {rank: fixed_timing(category, rank) for rank in RANKS} for category in CATEGORIES}


def pointer_calibration(start, end):
    """Bind two readable demo positions to exactly 60 input-free emulator frames."""
    for sample in (start, end):
        for field in ("frame", "epoch", "pointer_x"):
            value = sample[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Pointer calibration needs a whole nonnegative {field}.")
        if not 8 <= sample["pointer_x"] < 128:
            raise ValueError("Both calibration positions must remain within Dance 1's demonstration.")
    frames = end["frame"] - start["frame"]
    pixels = end["pointer_x"] - start["pointer_x"]
    if frames != POINTER_CALIBRATION_FRAMES or start["epoch"] != end["epoch"]:
        raise ValueError("Pointer calibration requires exactly 60 emulator frames in the same state epoch.")
    if pixels <= 0:
        raise ValueError("Pointer calibration needs positive forward displacement.")
    return {"source": "fresh_paused_pointer_displacement", "start": dict(start), "end": dict(end),
        "emulator_frames": frames, "pointer_pixels": pixels, "input_policy": "none",
        "pixels_per_frame": pixels / frames, "frames_per_pixel": frames / pixels}


def calibrated_timing(rank, calibration):
    """Change travel time only; retained fixed corrections are stop-gap estimates."""
    measured = pointer_calibration(calibration["start"], calibration["end"])
    for key, value in measured.items():
        actual = calibration.get(key)
        # Lua JSON rounds floating-point text. Integer endpoint facts stay exact;
        # all applied timing is recomputed from them rather than the wire rate.
        matches = (isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and math.isclose(actual, value, rel_tol=1e-12, abs_tol=0)) if key in (
                "pixels_per_frame", "frames_per_pixel") else actual == value
        if not matches:
            raise ValueError("The pointer calibration differs from its captured endpoints.")
    profile = timing_for(rank)
    old_half = math.floor(VISIBLE_HALF_PIXELS / profile["pixels_per_frame"] + 0.5)
    half = math.floor(VISIBLE_HALF_PIXELS / measured["pixels_per_frame"] + 0.5)
    fixed = {"cycle_extra_frames": profile["cycle_frames"] - 2 * old_half,
        "response_tail_frames": profile["response_frames"] - old_half,
        "leader_switch_frames": profile["extra_switch_frames"],
        "source": "stored_rank_timing_minus_rounded_visible_travel", "measured_stop_gaps": False}
    cycle, response = 2 * half + fixed["cycle_extra_frames"], half + fixed["response_tail_frames"]
    if not 0 < response < cycle:
        raise ValueError("The calibrated travel time and fixed corrections cannot form a complete dance.")
    return {**profile, "stored_timing": {key: profile[key] for key in
            ("pixels_per_frame", "cycle_frames", "response_frames", "extra_switch_frames")},
        "timing_source": "per_contest_60_frame_pointer_calibration", "fixed_timing_estimates": fixed,
        "pixels_per_frame": measured["pixels_per_frame"], "frames_per_pixel": measured["frames_per_pixel"],
        "visible_half_frames": VISIBLE_HALF_PIXELS / measured["pixels_per_frame"], "travel_half_frames": half,
        "cycle_frames": cycle, "response_frames": response,
        "lead_dances_frames": 2 * cycle + fixed["leader_switch_frames"]}
