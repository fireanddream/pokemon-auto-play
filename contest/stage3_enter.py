"""Regional enrollment inputs and paused dress-up entry retries for the main runner."""

import math

from contest.inputs import taps
from contest.pt_dance_read import stacked_gd
from contest.timing_profiles import FRAME_RATE, load_profile, section, setting
from host.checkpoints import write_json
from host.guards import wait_visual_match
from host.paths import project_path


CATEGORIES = ("Cool", "Beauty", "Cute", "Smart", "Tough")
RANKS = ("Normal", "Super", "Hyper", "Master")
ENTRY_RETRY_GAME_SECONDS = 3


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
        "entry_readiness": {"retry_game_seconds": ENTRY_RETRY_GAME_SECONDS,
            "retry_frames": round(ENTRY_RETRY_GAME_SECONDS * FRAME_RATE),
            "inputs": "input_free_wait", "matched_frames": 0,
            "deadline": "existing_section_host_deadline"},
        "acceptance": "Dress-up entry for the selected contest.",
        "review_source": "local_reference_pixels", "memory_proven_phase": False}


def wait_dress_entry(emulator, client, plan, work):
    """Keep the first matched paused frame; only mismatches authorize input-free waits."""
    controller = emulator.controller
    checks = []
    manifest = work / "dress-entry-checks.json"

    def save_checks():
        write_json(manifest, {"retry_game_seconds": plan["entry_readiness"]["retry_game_seconds"], "checks": checks})

    def observe():
        response = client.command("capture")
        snapshot = response["observation"]
        image = snapshot["image"]
        path = project_path(image["path"])
        if (not path.is_relative_to(work) or image["frame"] != response["frame"]
                or image["epoch"] != response["epoch"] or image["direct_read_frames"] != 0):
            raise RuntimeError("The dress-up entry capture must belong to this fresh paused Stage 3 frame.")
        png = path.with_suffix(".png")
        stacked_gd(path).save(png)
        sample = {"frame": response["frame"], "epoch": response["epoch"], "observation": snapshot,
            "image": {**image, "png": str(png)}, "end_frame": response["frame"]}
        checks.append(sample)
        save_checks()
        return sample

    def retry(sample):
        if sample["observation"]["automatic_check"]["confirmed"]:
            return
        pause = sample["observation"]["pause_plan"]
        progressed = emulator.command("combo", "inputs", *pause["steps"],
            timeout=None, deadline=controller.live_deadline)
        if progressed.get("stop_reason") != "combo_complete" or progressed["frame"] != pause["end_frame"]:
            raise RuntimeError("The dress-up entry wait did not reach its configured endpoint.")
        sample["end_frame"] = progressed["frame"]
        save_checks()

    return wait_visual_match(controller, observe, retry,
        matches=lambda sample: sample["observation"]["automatic_check"]["confirmed"])
