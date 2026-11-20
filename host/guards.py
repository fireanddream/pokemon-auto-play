"""Reusable visual-match retries under the caller's existing host deadline."""


def wait_visual_match(controller, observe, tap, *, matches, accept=None):
    """Retry until matched; callers own buttons, durations, captures, and pause validation.

    ``tap(sample)`` supplies the mismatch retry or matched settling period.
    ``accept(sample)`` runs only after matched settling completes within the deadline.
    No checkpoint visits, saves, deadline resets, or extra retry limits are added here.
    """
    checks = []
    while True:
        controller.remaining()
        sample = observe()
        checks.append(sample)
        matched = matches(sample)
        controller.remaining()
        tap(sample)
        if matched:
            controller.remaining()
            if accept is not None:
                accept(sample)
            return checks
