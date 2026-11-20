"""Bag-opening state shared across Stage 2 calls within one loaded game."""

from dataclasses import dataclass


@dataclass
class Stage2BagState:
    """Retain this state with its checkpoint; a fresh Stage 1 load starts false."""

    opened_once: bool = False

    def opening_buttons(self) -> tuple[str, ...]:
        """Plan the opening without consuming it when the Bag is skipped."""
        if self.opened_once:
            return ("X", "A")
        return ("X", "down", "down", "A")
