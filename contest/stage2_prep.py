"""Select the next ribbon and plan scarf inputs from accepted party/Bag data."""

from contest.timing_profiles import load_profile, section


SCARVES = {260: "Red Scarf", 261: "Blue Scarf", 262: "Pink Scarf", 263: "Green Scarf", 264: "Yellow Scarf"}
BAG_DOWN_RELEASE_FRAMES = section(None, "stage2_preparation")["bag_down_release_frames"]


def prep_plan(snapshot, *, bag_state, cursor=1, target=None, timing_profile=None):
    profile = load_profile() if timing_profile is None else timing_profile
    values = section(profile, "stage2_preparation")
    party, entries = snapshot["party"], snapshot["bag"]["items"]
    target = party.get("next_target") if target is None else dict(target)
    required = target["required_scarf_id"] if target else None
    scarf = next((item for item in entries if item["item_id"] == required), None)
    held = party["held_item_id"]
    inputs = []
    after = cursor
    mode = "all_ribbons_owned" if target is None else "correct_scarf_held" if held == required else "scarf_unavailable"
    if target is not None and held != required and scarf is not None:
        mode = "equip_scarf"
        delta = scarf["position"] - cursor

        def press(button, release=None):
            if release is None:
                release = values["bag_down_release_frames"] if button == "down" else values["menu_release_frames"]
            return f"{button}:{values['menu_hold_frames']}:{release}"

        inputs = [press(button) for button in bag_state.opening_buttons()]
        inputs += [press("down" if delta > 0 else "up")] * abs(delta)
        inputs += [press("A"), press("A", values["give_confirmation_release_frames"])]
        inputs += [press("A")] * (4 if held else 2) + [press("B")] * 2
        after = scarf["position"]
        if scarf["quantity"] == 1 and after == len(entries):
            old_stack = any(item["item_id"] == held for item in entries)
            if not held or old_stack:
                after = max(1, after - 1)
    return {"stage": 2, "timing_profile_source": profile["source"], "mode": mode, "target": target, "source_species_id": party["species_id"],
        "expected_item_id": required if inputs else held, "required_scarf_id": required,
        "bag_opened_before": bag_state.opened_once, "cursor_before": cursor,
        "target_position": scarf["position"] if scarf else None,
        "target_quantity": scarf["quantity"] if scarf else None,
        "bag_context": {"opened_once": bag_state.opened_once or bool(inputs), "cursor_position": after},
        "inputs": inputs,
        "emulator_frames": sum(sum(map(int, step.split(":")[1:])) for step in inputs),
        "checkpoint_slot": 2, "memory_proven_phase": False}
