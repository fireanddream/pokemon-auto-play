"""Fixed regional dance metrics; historical motion evidence is not a dependency."""

import math

from contest.timing_profiles import load_profile, section


RANKS = ("Normal", "Super", "Hyper", "Master")
CATEGORIES = ("Cool", "Beauty", "Cute", "Smart", "Tough")
ALIASES = {"normal": "Normal", "super": "Super", "great": "Super", "hyper": "Hyper", "ultra": "Hyper", "master": "Master"}


def metrics_for(timing_profile=None):
    # Speed is pixels per emulator frame; gaps run from x=248 to the next x=8.
    return {family: (values["pointer_speed_numerator"] / values["pointer_speed_denominator"],
        values["same_leader_gap_frames"], values["leader_change_gap_frames"])
        for family, values in section(timing_profile, "fixed_dance_metrics").items() if family != "_help"}


def canonical_rank(rank):
    if not isinstance(rank, str) or rank.lower() not in ALIASES:
        raise ValueError("Select Normal, Super/Great, Hyper/Ultra, or Master.")
    return ALIASES[rank.lower()]


def fixed_cue_spacing(category, rank, *, timing_profile=None):
    rank = canonical_rank(rank)
    if category not in CATEGORIES:
        raise ValueError("Select Cool, Beauty, Cute, Smart, or Tough.")
    family = "Normal/Super" if rank in RANKS[:2] else category
    return section(timing_profile, "fixed_dance_metrics")[family]["cue_side_offset_pixels"]


def fixed_timing(category, rank, *, timing_profile=None):
    """Resolve the selected regional profile without loading historical evidence."""
    if category not in CATEGORIES:
        raise ValueError("Select Cool, Beauty, Cute, Smart, or Tough.")
    profile = load_profile() if timing_profile is None else timing_profile
    rank = canonical_rank(rank)
    family = "Normal/Super" if rank in RANKS[:2] else category
    speed, dance_gap, leader_gap = metrics_for(profile)[family]
    cycle, extra = math.floor(240 / speed + dance_gap), leader_gap - dance_gap
    return {"category": category, "rank": rank, "family": family,
        "cues_per_dance": 3 if rank == "Normal" else 4,
        "cue_side_offset_pixels": fixed_cue_spacing(category, rank, timing_profile=profile),
        "pixels_per_frame": speed, "frames_per_pixel": 1 / speed, "visible_half_frames": 120 / speed,
        "dance_gap_frames": dance_gap, "leader_gap_frames": leader_gap,
        "cycle_frames": cycle, "extra_switch_frames": extra, "response_frames": math.floor(120 / speed),
        "lead_dances_frames": 2 * cycle + extra, "profile_source": profile["source"] + "#fixed_dance_metrics",
        "timing_source": "fixed_normalized_contest_measurements", "derived_frame_rounding": "floor"}


def all_fixed_timings(*, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    return {category: {rank: fixed_timing(category, rank, timing_profile=profile) for rank in RANKS} for category in CATEGORIES}
