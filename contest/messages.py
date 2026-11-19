"""Readable terminal abort reasons, including failed Lua checkpoint predicates."""

import json
import re


class ContestCallerActive(RuntimeError):
    """Another script already owns the contest caller lock."""


CHECK_MESSAGES = {
    "cpuj": "The loaded game is not Pokemon Platinum.",
    "platinum": "The loaded game is not Pokemon Platinum.",
    "loaded_overworld_screenshot": "Stand on the blue mat facing the middle contest receptionist, then save the game and try again.",
    "validated_first_party": "The first Pokemon's data could not be read reliably.",
    "validated_party": "The first Pokemon's data could not be read reliably.",
    "first_not_egg": "The first party slot contains an Egg. Put the contest Pokemon first.",
    "first_hp": "The first Pokemon has fainted. Heal it before starting.",
    "first_sheen": "The first Pokemon needs Sheen greater than 0. Feed it a Poffin before starting.",
    "first_moves": "The first Pokemon needs at least two moves.",
    "readable_text_speed": "The game's text-speed setting could not be read reliably.",
    "fast_text_speed": "Set Options > Text Speed to Fast, save the game, and try again.",
    "same_first_party": "The first Pokemon changed during the run.",
    "scripted_section_complete": "The preparation inputs did not finish as expected.",
    "dress_up_entry_screenshot": "Contest entry did not reach the expected dress-up screen.",
    "readable_rank_indicator": "The dress-up accessory limit could not be read.",
    "green_progress_tip": "The start of the dancing stage could not be confirmed.",
    "demo_pointer": "The Dance 1 demonstration pointer could not be read.",
    "dance_1": "The dancing stage did not start at Dance 1.",
    "three_readable_judges": "The three judges' star counts could not be read.",
    "initial_empty_star_slots": "Acting did not start with all three judges' stars empty.",
    "four_readable_move_categories": "An available move's contest category could not be read.",
    "available_move_categories": "An available move's contest category could not be read.",
    "overworld": "The player did not return to the expected contest reception position.",
    "four_rounds_complete": "The four Acting rounds did not finish as expected.",
    "released": "The game inputs could not be released at the checkpoint.",
    "render_prepared": "The game screen was not ready to be checked.",
    "finish_tap_complete": "The dress-up Finish input did not complete.",
    "dressing_complete": "The dress-up inputs did not finish as expected.",
    "A_mashing_complete": "The transition from dress-up to dancing did not complete.",
    "B_transition_complete": "The transition from dancing to Acting did not complete.",
    "response_complete": "The dancing response did not finish as expected.",
    "sequence": "The dancing responses occurred in an unexpected order.",
    "rank_profile": "The dancing timing does not match the selected contest rank.",
    "midpoint": "The dancing response did not reach its expected starting point.",
    "plan": "The dancing input plan does not match the observed cues.",
    "before_input": "A dancing input started before its checkpoint was accepted.",
    "feedback_policy_met": "The required dancing feedback could not be confirmed.",
    "accepted_feedback": "The first dancing response did not meet the required feedback check.",
    "round": "Acting reached an unexpected round.",
    "zero_frame_move_read": "Reading the move categories advanced the game unexpectedly.",
    "midpoint_policy": "The dancing response did not reach its authorized starting point.",
    "planned_cues_bound": "The dancing input plan does not match its captured cues.",
    "rank_cues_visible": "The expected dancing cues could not be read.",
    "response_inputs": "The number of dancing inputs does not match the response plan.",
    "accounted_inputs": "A dancing input was neither completed nor recorded as skipped.",
    "verified_feedback_total": "The number of dancing feedback readings does not match the completed inputs.",
    "offset_verified": "The first dancing input's timing could not be confirmed.",
    "counted_response_endpoint": "The dancing response ended at an unexpected point.",
    "single_feedback_check": "The first dancing input's feedback was not recorded exactly once.",
    "before_first_input": "A dancing input started before its checkpoint was accepted.",
    "first_input_only": "More dancing inputs ran than were authorized for the first feedback check.",
    "six_answered_dances": "The six dancing responses did not all finish.",
    "response_input_total": "The total dancing input count is incomplete.",
    "first_press_feedback": "The first dancing input's feedback could not be confirmed.",
    "lead_dances_complete": "The two lead dances did not finish.",
    "lead_input_policy": "The lead dances were not configured to remain input-free.",
    "no_lead_inputs": "An unexpected input occurred during the lead dances.",
    "transition_endpoint": "The transition to Acting ended at an unexpected point.",
    "host_stop": "The run was stopped.",
}


def is_timeout(error):
    if isinstance(error, TimeoutError):
        return True
    text = str(error).strip().lower()
    return text == "timeout" or text.endswith(": timeout") or "timed out" in text or bool(re.search(
        r"(?:deadline|time limit).*\b(?:expired|exceeded|reached)\b|\btimeout\s*(?:expired|exceeded|reached)", text))


def _clean(text):
    text = re.sub(r"^.*?\.lua:\d+:\s*", "", str(text)).strip()
    return text or "The runner stopped unexpectedly. Check the retained run log."


def abort_message(error, *, stage=None):
    if is_timeout(error):
        return "timeout"
    if isinstance(error, KeyboardInterrupt):
        return "The run was cancelled."
    record = getattr(error, "record", None)
    if record:
        reasons = []
        for check in record.get("checks", []):
            if check.get("passed") is not False:
                continue
            name = check.get("id", "checkpoint")
            reason = "Required game data could not be read reliably." if check.get("error") else CHECK_MESSAGES.get(name)
            if reason is None:
                if any(word in name for word in ("frame", "epoch", "capture", "binding", "pixel_reader")):
                    reason = "The screenshot or memory reading no longer matches the paused game."
                else:
                    reason = "The game checkpoint could not confirm " + name.replace("_", " ") + "."
            if reason not in reasons:
                reasons.append(reason)
        if reasons:
            return "\n".join(reasons)
        return _clean(record.get("error", str(error)))
    if isinstance(error, FileNotFoundError):
        return f"A required file is missing: {error.filename}" if error.filename else "A required game or runner file is missing."
    if isinstance(error, PermissionError):
        return f"Windows denied access to: {error.filename}" if error.filename else "Windows denied access to an emulator or runner file."
    if isinstance(error, KeyError):
        return f"The game checkpoint is incomplete: missing {error.args[0]}."
    if isinstance(error, json.JSONDecodeError):
        return "The runner could not read the emulator's status or checkpoint data."
    if isinstance(error, UnicodeError):
        return "A game or configuration file could not be read."
    return _clean(error)
