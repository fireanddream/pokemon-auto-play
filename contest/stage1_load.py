"""Regional battery-save loading inputs for the main collection runner."""

from contest.timing_profiles import load_profile, section, setting


START_WAIT_FRAMES = section(None, "stage1_loading")["start_release_frames"]


def load_plan(*, boot_wait_frames=None, hold_frames=None, start_wait_frames=None,
              a_wait_frames=None, b_wait_frames=None, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    values = section(profile, "stage1_loading")
    boot_wait_frames = setting(boot_wait_frames, values, "boot_wait_frames")
    hold_frames = setting(hold_frames, values, "button_hold_frames")
    start_wait_frames = setting(start_wait_frames, values, "start_release_frames")
    a_wait_frames = setting(a_wait_frames, values, "load_save_a_release_frames")
    b_wait_frames = setting(b_wait_frames, values, "dismiss_prompt_b_release_frames")
    timing = {"boot_wait_frames": boot_wait_frames, "hold_frames": hold_frames,
              "start_wait_frames": start_wait_frames, "a_wait_frames": a_wait_frames,
              "b_wait_frames": b_wait_frames}
    for name, value in timing.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive whole emulator-frame count.")
    return {"stage": 1, "timing_profile_source": profile["source"], "timing": timing, "inputs": [f"wait:{boot_wait_frames}",
        f"start:{hold_frames}:{start_wait_frames}", f"start:{hold_frames}:{start_wait_frames}",
        f"A:{hold_frames}:{a_wait_frames}", f"B:{hold_frames}:{b_wait_frames}"],
        "total_emulator_frames": boot_wait_frames + 4 * hold_frames + 2 * start_wait_frames
            + a_wait_frames + b_wait_frames,
        "uncapped": True, "attempts": 1,
        "acceptance": "Loaded contest-hall overworld facing the middle receptionist.",
        "review_source": "local_reference_pixels", "memory_proven_phase": False}
