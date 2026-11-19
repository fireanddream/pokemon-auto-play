"""Regional enrollment inputs for the main runner's accepted contest target."""

import math

from contest.inputs import taps
from contest.timing_profiles import FRAME_RATE, load_profile, section, setting


CATEGORIES = ("Cool", "Beauty", "Cute", "Smart", "Tough")
RANKS = ("Normal", "Super", "Hyper", "Master")
INTRO_SECONDS = section(None, "stage3_entry")["intro_a_mash_game_seconds"]


def entry_plan(category, rank, *, host_timeout, hold_frames=None,
               release_frames=None, intro_seconds=None, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    values = section(profile, "stage3_entry")
    hold_frames = setting(hold_frames, values, "menu_hold_frames")
    release_frames = setting(release_frames, values, "menu_release_frames")
    intro_seconds = setting(intro_seconds, values, "intro_a_mash_game_seconds")
    mash_hold, mash_release = values["intro_mash_hold_frames"], values["intro_mash_release_frames"]
    if category not in CATEGORIES or rank not in RANKS:
        raise ValueError("Supply a supported contest category and rank.")
    if not math.isfinite(host_timeout) or host_timeout <= 0:
        raise ValueError("The host timeout must be positive seconds.")
    if not math.isfinite(intro_seconds) or intro_seconds <= 0:
        raise ValueError("The introduction duration must be positive game seconds.")
    menu = ["A"] * 4 + ["down"] * RANKS.index(rank) + ["A"]
    menu += ["down"] * CATEGORIES.index(category) + ["A"] * 4
    steps = taps(menu, hold_frames, release_frames)
    intro_frames = round(intro_seconds * FRAME_RATE)
    if intro_frames < 1:
        raise ValueError("The introduction must cover at least one emulator frame.")
    cycle = mash_hold + mash_release
    count, tail = divmod(intro_frames, cycle)
    steps += taps(["A"] * count, mash_hold, mash_release) if count else []
    if tail > mash_hold:
        steps += taps("A", mash_hold, tail - mash_hold)
    elif tail:
        steps.append(f"wait:{tail}")
    total_frames = len(menu) * (hold_frames + release_frames) + intro_frames
    return {"stage": 3, "timing_profile_source": profile["source"], "target": {"category": category, "rank": rank, "party_slot": 1},
        "host_timeout_seconds": host_timeout, "attempts": 1, "uncapped": True,
        "timing": {"hold_frames": hold_frames, "release_frames": release_frames,
            "mash_hold_frames": mash_hold, "mash_release_frames": mash_release,
            "intro_game_seconds": intro_seconds, "intro_frames": intro_frames,
            "frame_conversion": "nearest frame at 67027965 / 1120380 frames per second"},
        "rank_down_presses": RANKS.index(rank), "category_down_presses": CATEGORIES.index(category),
        "inputs": steps, "menu_frames": len(menu) * (hold_frames + release_frames),
        "total_emulator_frames": total_frames,
        "acceptance": "Dress-up entry for the selected contest.",
        "review_source": "local_reference_pixels", "memory_proven_phase": False}
