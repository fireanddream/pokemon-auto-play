"""Contest input grammar and whole-frame validation."""

from __future__ import annotations


def whole(value: int, minimum: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} requires a whole number of at least {minimum}.")
    return value


def taps(buttons: str | list[str], hold_frames: int, release_frames: int) -> list[str]:
    hold = whole(hold_frames, 1, "Held frames")
    release = whole(release_frames, 1, "Released frames")
    selected = [buttons] if isinstance(buttons, str) else list(buttons)
    if not selected or not all(isinstance(button, str) and button for button in selected):
        raise ValueError("Supply a button string or a nonempty ordered list of button strings.")
    return [f"{button}:{hold}:{release}" for button in selected]
