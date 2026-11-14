"""Native-image CPUJ contest dance recognition from PNG or GD screenshots.

Only image pixels identify progress and demonstrated buttons. The caller supplies
the dance number (1..6); a still picture cannot distinguish the two dances with
the same leader. Pillow is the only external dependency.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import struct

from PIL import Image, ImageDraw


BUTTONS = ("A", "B", "X", "Y")
GRID_X = tuple(range(18, 119, 10))
CUE_SIDE_OFFSET = 120
FILLED_PROFILES = (
    ((56, 255, 134), (190, 255, 255), (56, 255, 134), (24, 207, 113)),
    ((207, 255, 134), (255, 255, 223), (207, 255, 134), (174, 223, 97)),
    ((24, 199, 24), (97, 215, 97), (24, 199, 24), (16, 158, 16)),
)
EMPTY_PROFILE = ((56, 65, 81), (134, 142, 158), (134, 142, 158), (134, 142, 158))

# Native 16-pixel sprites. Spaces are transparent/background, never evidence.
_LEFT_COLOR_ROWS = (
    "     ######     ", "   ##cccccc##   ", "  #cc......cc#  ",
    " #c..........c# ", " #c..........c# ", " #de........ed# ",
    " #deeeiiiieeed# ", " ##dceeeeeecd## ", "  a#dddddddd#a  ",
    "  abb######bba  ", "  agbbbbbbbbga  ", "  afgbbbbbbgfa  ",
    "   afgajjagfa   ", "   affggggffa   ", "    aaffffaa    ",
    "      aaaa      ",
)
_LEFT_BLUE_ROWS = (
    "     ......     ", "   ..aaaaaa..   ", "  .aa######aa.  ",
    " .a##########a. ", " .############. ", " .c##########c. ",
    " .acc######cca. ", " ..acccccccca.. ", " .b.aaaaaaaa... ",
    "  .bb......bb.  ", "  .dbbbbbbbbd.  ", "  .ddbbbbbbd..  ",
    "   .dd.aa.dd.   ", "   ...dddd...   ", "    ........    ",
    "      ....      ",
)
_LEFT_COMMON = {
    "a": (65, 97, 190), "b": (48, 65, 81), "f": (89, 142, 231),
    "g": (97, 166, 255), "j": (134, 207, 255),
}
_LEFT_PALETTES = {
    "A": {**_LEFT_COMMON, ".": (255, 239, 158), "#": (231, 158, 0),
          "c": (255, 255, 73), "d": (255, 231, 40), "e": (255, 255, 190), "i": (255, 247, 174)},
    "X": {**_LEFT_COMMON, ".": (255, 190, 207), "#": (199, 16, 32),
          "c": (255, 166, 239), "d": (255, 105, 215), "e": (255, 231, 255), "i": (255, 207, 247)},
    "Y": {**_LEFT_COMMON, ".": (190, 247, 223), "#": (0, 174, 0),
          "c": (105, 247, 134), "d": (48, 255, 73), "e": (215, 255, 247), "i": (207, 255, 231)},
    "B": {".": (65, 97, 190), "#": (207, 223, 247), "a": (134, 207, 255),
          "b": (48, 65, 81), "c": (223, 247, 255), "d": (97, 166, 255)},
}
_RING_ROWS = (
    "                ", "      #####     ", "    ##a...a##   ",
    "   #a.......a#  ", "  #a.........a# ", "  #...a###a...# ",
    " #a..a#   #a..a#", " #...#     #...#", " #...#     #...#",
    " #a..a#   #a..a#", "  #...a###a...# ", "  #a.........a# ",
    "   #a.......a#  ", "    ##a...a##   ", "      #####     ",
)
_RING_COLORS = {
    "A": ((247, 199, 8), (255, 255, 73)),
    "B": ((89, 142, 231), (134, 207, 255)),
    "X": ((247, 56, 105), (255, 166, 239)),
    "Y": ((24, 223, 65), (105, 247, 134)),
}
_POINTER_ROWS = (
    "       ##       ", " eeaa#aeeefaaee ", " # a# feea##a # ",
    " a ## ffeea## a ", "  a#g  ffagh#a  ", "   gql  agqlhc  ",
    "   glhhaaglhhc  ", "   #gh####gh#c  ", "    ###nn###c   ",
    "  f#cc#an#cc#f  ", "    aaccccaa    ", "     aaaaaa     ",
    "    a      a    ",
)
_POINTER_STEP_ROWS = (
    "       ##       ", " eeaa#aeeefaaee ", " # a# feea##a # ",
    " a ## ffeea## a ", "   #g affagh#a  ", "   gql  agqlhc  ",
    "   glhhaaglhhc  ", "   cgh####gh#c  ", "    c##nn###c   ",
    "   fcc##n#ccf   ", "    aaccccaa    ", "      aaaa      ",
    "     a    a     ", "     a    a     ",
)
_POINTER_PALETTE = {
    "#": (255, 134, 239), "a": (223, 77, 182), "c": (247, 101, 207),
    "e": (255, 215, 255), "f": (255, 166, 239), "n": (247, 60, 109),
    "g": (28, 215, 142), "h": (28, 174, 101), "l": (0, 117, 44),
    "q": (255, 255, 255),
}


@dataclass(frozen=True)
class Sprite:
    points: tuple
    probes: tuple
    minimum_match_fraction: float = 0.94
    strict_probes: bool = True


def _sprite(rows, palette, probes, *, minimum_match_fraction=0.94, strict_probes=True):
    points = tuple((x, y, palette[code]) for y, row in enumerate(rows)
                   for x, code in enumerate(row) if code != " ")
    colors = {(x, y): color for x, y, color in points}
    return Sprite(points, tuple((x, y, colors[x, y]) for x, y in probes), minimum_match_fraction, strict_probes)


LEFT_SPRITES = {
    button: _sprite(_LEFT_BLUE_ROWS if button == "B" else _LEFT_COLOR_ROWS,
                    _LEFT_PALETTES[button], ((5, 0), (7, 2), (2, 5), (8, 10)),
                    minimum_match_fraction=0.80, strict_probes=False)
    for button in BUTTONS
}
RIGHT_SPRITES = {
    button: _sprite(_RING_ROWS, {".": colors[0], "a": colors[1], "#": (223, 247, 255)},
                    ((8, 3), (3, 7), (12, 11), (8, 1)), minimum_match_fraction=0.80, strict_probes=False)
    for button, colors in _RING_COLORS.items()
}
POINTER_SPRITES = tuple(_sprite(rows, _POINTER_PALETTE, ((7, 0), (2, 1), (4, 5), (10, 5)))
                        for rows in (_POINTER_ROWS, _POINTER_STEP_ROWS))
POINTER_LOWER_SPRITES = tuple(_sprite(tuple(" " * len(row) if y < 7 else row
                                           for y, row in enumerate(rows)), _POINTER_PALETTE,
                                     ((7, 8), (4, 7), (8, 10), (7, 11)))
                              for rows in (_POINTER_ROWS, _POINTER_STEP_ROWS))


def load_image(path: Path | str) -> Image.Image:
    """Read native true-color GD or a PNG, without any emulator dependency."""
    path = Path(path)
    if path.suffix.lower() == ".gd":
        return gd_image(path.read_bytes())
    with Image.open(path) as source:
        if source.format != "PNG":
            raise ValueError("Use a lossless PNG or native GD screenshot")
        image = source.convert("RGB")
    return native_top(image)


def gd_image(raw: bytes) -> Image.Image:
    if len(raw) < 11:
        raise ValueError("Truncated GD framebuffer")
    magic, width, height = struct.unpack_from(">HHH", raw)
    if magic != 65534 or raw[6] != 1 or width != 256 or height not in (192, 384):
        raise ValueError("Expected a native 256x192 or 256x384 true-color GD framebuffer")
    if len(raw) != 11 + width * height * 4:
        raise ValueError("GD framebuffer length does not match its dimensions")
    channels = Image.frombytes("RGBA", (width, height), raw[11:]).split()
    return native_top(Image.merge("RGB", channels[1:]))


def native_top(image: Image.Image) -> Image.Image:
    if image.size not in ((256, 192), (256, 384)):
        raise ValueError("Use the native 256x192 top screen or 256x384 stacked screens")
    return image.convert("RGB").crop((0, 0, 256, 192))


def _fade_scale(pairs):
    numerator = denominator = 0
    for expected, observed in pairs:
        for base, actual in zip(expected, observed):
            dark = 255 - base
            numerator += dark * (255 - actual)
            denominator += dark * dark
    return numerator / denominator if denominator else 1.0


def _near(expected, actual, scale, tolerance=8):
    return all(abs(observed - (255 - (255 - base) * scale)) <= tolerance
               for base, observed in zip(expected, actual))


def _fade_interval(expected, actual, tolerance=8):
    lower, upper = 0.35, 1.02
    for base, observed in zip(expected, actual):
        dark = 255 - base
        if dark == 0:
            if abs(observed - 255) > tolerance:
                return None
        else:
            lower = max(lower, (255 - observed - tolerance) / dark)
            upper = min(upper, (255 - observed + tolerance) / dark)
            if lower > upper:
                return None
    return lower, upper


def _match_partial(px, x, y, sprite):
    required = math.ceil(len(sprite.points) * sprite.minimum_match_fraction)
    allowed_misses = len(sprite.points) - required
    pairs, intervals = [], []
    native_matches = impossible = 0
    for dx, dy, color in sprite.points:
        actual = px[x + dx, y + dy]
        pairs.append((color, actual))
        if all(abs(base - observed) <= 8 for base, observed in zip(color, actual)):
            native_matches += 1
            intervals.append(None)
        else:
            interval = _fade_interval(color, actual)
            intervals.append(interval)
            impossible += interval is None
            if impossible > allowed_misses:
                return None
    matches, scale = native_matches, 1.0
    if matches < required:
        # Fit one shared fade from the full cue, including cues with altered probes.
        intervals = [interval or _fade_interval(*pair) for pair, interval in zip(pairs, intervals)]
        events = [(bound, delta) for interval in intervals if interval is not None
                  for bound, delta in ((interval[0], 1), (interval[1], -1))]
        count = best = 0
        for bound, delta in sorted(events, key=lambda event: (event[0], -event[1])):
            count += delta
            if count > best:
                best, scale = count, bound
        if best < required:
            return None
        matches = sum(interval is not None and interval[0] <= scale <= interval[1] for interval in intervals)
    return {"match_fraction": round(matches / len(sprite.points), 4), "contrast": round(min(1.0, scale), 4),
            "matched_pixels": matches, "template_pixels": len(sprite.points)}


def _match(px, x, y, sprite):
    if not sprite.strict_probes:
        return _match_partial(px, x, y, sprite)
    pairs = [(color, px[x + dx, y + dy]) for dx, dy, color in sprite.probes]
    scale = _fade_scale(pairs)
    if not 0.35 <= scale <= 1.02 or any(not _near(a, b, scale) for a, b in pairs):
        return None
    matches = sum(_near(color, px[x + dx, y + dy], scale) for dx, dy, color in sprite.points)
    score = matches / len(sprite.points)
    if score < sprite.minimum_match_fraction:
        return None
    return {"match_fraction": round(score, 4), "contrast": round(min(1.0, scale), 4),
            "matched_pixels": matches, "template_pixels": len(sprite.points)}


def _profile_label(column, x):
    if column in FILLED_PROFILES:
        return "filled"
    if column == EMPTY_PROFILE:
        return "empty"
    if x % 30 in (8, 9) and column == ((255, 255, 223),) * 4:
        return "empty"
    if x % 10 == 8 and column == ((182, 190, 190),) * 4:
        return "empty"
    # During the screen flash fade, the four-row shape survives even though its
    # RGB values change. A flat green costume patch lacks this bright inner row.
    if column[0] == column[2] and column[1] != column[0] and column[3] != column[0]:
        for profile in FILLED_PROFILES:
            scale = _fade_scale(zip(profile, column))
            if 0.35 <= scale <= 1.01 and all(_near(a, b, scale, 4) for a, b in zip(profile, column)):
                return "filled"
    if column[1] == column[2] == column[3] and column[0] != column[1]:
        scale = _fade_scale(zip(EMPTY_PROFILE, column))
        if 0.35 <= scale <= 1.01 and all(_near(a, b, scale, 4) for a, b in zip(EMPTY_PROFILE, column)):
            return "empty"
    return "unknown"


def read_bar(image):
    px = image.load()
    labels = {x: _profile_label(tuple(px[x, y] for y in range(155, 159)), x) for x in range(8, 248)}
    filled = [x for x, label in labels.items() if label == "filled"]
    empty = [x for x, label in labels.items() if label == "empty"]
    lower, upper = max(filled, default=None), min(empty, default=248) - 1
    result = {"source": "four_row_fill_and_empty_profiles", "filled_columns": len(filled),
              "empty_columns": len(empty), "unknown_columns": 240 - len(filled) - len(empty),
              "first_empty_x": min(empty, default=None),
              "position_bounds": None, "status": "unreadable"}
    if lower is not None and upper < lower:
        result["status"] = "conflict"
    elif lower is not None:
        result.update(position_bounds=[lower, upper], status="exact" if lower == upper else "occluded")
    elif len(empty) >= 180 and upper == 7:
        result["status"] = "empty"
    return result


def read_pointer(image):
    px = image.load()
    matches = []
    for y in range(116, 154):
        for x in range(8, 248):
            r, g, b = px[x, y]
            if y < 146 and r >= 240 and r - g >= 35 and b - g >= 30:
                for pose, sprite in enumerate(POINTER_SPRITES):
                    found = _match(px, x - 7, y, sprite)
                    if found:
                        matches.append({"x": x, "top_y": y, "pose": pose, "visible_part": "full", **found})
            # A save/load caption can cover the ears while the distinctive
            # eyes, nose and feet remain intact. Require the entire lower shape.
            if y >= 124 and r >= 200 and r - g >= 60 and b - g >= 14 and r - b >= 40:
                for pose, sprite in enumerate(POINTER_LOWER_SPRITES):
                    found = _match(px, x - 7, y - 8, sprite)
                    if found:
                        matches.append({"x": x, "top_y": y - 8, "pose": pose, "visible_part": "lower", **found})
    positions = sorted({match["x"] for match in matches})
    return {"source": "pink_pointer_sprite", "matches": matches,
            "status": "exact" if len(positions) == 1 else "ambiguous" if positions else "unreadable",
            "position_bounds": [positions[0], positions[0]] if len(positions) == 1 else None}


def layout_for(dance):
    if isinstance(dance, bool) or not isinstance(dance, int) or dance not in range(1, 7):
        raise ValueError("Dance must be an integer from 1 through 6")
    pair = (dance - 1) // 2
    return {"dance": dance, "player_position": ("top_right", "top_middle", "top_left")[pair],
            "player_head_region": ((168, 0, 248, 60), (88, 0, 168, 60), (8, 0, 88, 60))[pair],
            "right_marker_y": (184, 176, 168)[pair],
            "preferred_side": ("left", "right", "right")[pair]}


def read_side(image, side, layout):
    px = image.load()
    result = []
    sprites = LEFT_SPRITES if side == "left" else RIGHT_SPRITES
    y = 168 if side == "left" else layout["right_marker_y"] - 8
    for demo_x in GRID_X:
        center = demo_x if side == "left" else demo_x + CUE_SIDE_OFFSET
        candidates = []
        for button, sprite in sprites.items():
            match = _match(px, center - 8, y, sprite)
            if match:
                candidates.append({"button": button, **match})
        if candidates:
            result.append({"demo_x": demo_x, "screen_x": center,
                           "status": "readable" if len(candidates) == 1 else "ambiguous",
                           "candidates": candidates})
    return result


def merge_buttons(left, right, preferred_side, conflict_policy):
    if conflict_policy not in ("error", "master"):
        raise ValueError("Conflict policy must be error or master")
    sides = {"left": {item["demo_x"]: item for item in left},
             "right": {item["demo_x"]: item for item in right}}
    if preferred_side == "best_visible":
        preferred_side = max(("left", "right"), key=lambda side: sum(
            item["status"] == "readable" for item in sides[side].values()))
    accepted, conflicts = [], []
    for x in sorted(set(sides["left"]) | set(sides["right"])):
        readings = {side: entries[x]["candidates"][0] for side, entries in sides.items()
                    if x in entries and entries[x]["status"] == "readable"}
        if not readings:
            continue
        different = len({item["button"] for item in readings.values()}) > 1
        if different:
            conflicts.append({"demo_x": x, "readings": readings})
            if conflict_policy == "error" or preferred_side not in readings:
                continue
        selected = preferred_side if preferred_side in readings else next(iter(readings))
        accepted.append({"demo_x": x, "button": readings[selected]["button"], "source_side": selected,
                         "fallback": selected != preferred_side, "cross_checked": len(readings) == 2 and not different,
                         "evidence": readings})
    return {"master_side": preferred_side, "conflict_policy": conflict_policy,
            "accepted": accepted, "conflicts": conflicts, "left": left, "right": right}


def read_progress(bar, pointer, *, frames_per_pixel=9 / 5):
    bounds = bar["position_bounds"]
    point = pointer["position_bounds"]
    source = "bar" if bounds is not None else None
    if bar["status"] == "conflict":
        return {"status": "conflict", "phase": "unknown", "position_bounds": None, "frame_range": None}
    if bar["status"] == "empty":
        return {"status": "inactive", "phase": "unknown", "position_bounds": None, "frame_range": None}
    if point is not None:
        if bounds is not None and not bounds[0] <= point[0] <= bounds[1]:
            return {"status": "conflict", "phase": "unknown", "position_bounds": None, "frame_range": None}
        if bounds is None and bar["first_empty_x"] is not None and point[0] >= bar["first_empty_x"]:
            return {"status": "conflict", "phase": "unknown", "position_bounds": None, "frame_range": None}
        bounds, source = point, "bar_and_pointer" if bounds else "pointer"
    if bounds is None:
        return {"status": "unreadable", "phase": "unknown", "position_bounds": None, "frame_range": None}
    lower, upper = bounds
    phase = "demo" if upper < 128 else "response" if lower >= 128 else "boundary_uncertain"
    # The retained replay includes two-frame
    # display holds and small timing offsets, especially early in odd dances.
    # These are conservative measured envelopes, not exact emulator timestamps.
    before, after = (1, 5) if phase == "demo" else (3, 3) if phase == "response" else (3, 5)
    half_frames = 120 * frames_per_pixel
    frames = [max(0, math.ceil((lower - 8) * frames_per_pixel - before)),
              min(math.ceil(2 * half_frames) - 1, math.floor((upper - 8) * frames_per_pixel + after))]
    return {"status": "readable" if lower == upper else "bounded", "phase": phase,
            "position_bounds": bounds, "position_source": source, "frame_range": frames,
            "frame_unit": "emulator_frames_from_dance_sweep_start",
            "nominal_frame": round(((lower + upper) / 2 - 8) * frames_per_pixel, 2),
            "frames_to_response_range": [half_frames - frames[1], half_frames - frames[0]],
            "timing_source": "pixel_position_with_measured_tempo_envelope"}


def analyze(image, dance, *, conflict_policy="error"):
    image = native_top(image)
    layout = layout_for(dance)
    bar, pointer = read_bar(image), read_pointer(image)
    progress = read_progress(bar, pointer)
    buttons = merge_buttons(read_side(image, "left", layout), read_side(image, "right", layout),
                            layout["preferred_side"], conflict_policy)
    errors = []
    if progress["status"] == "conflict":
        errors.append("Pointer and timing-strip evidence disagree")
    if buttons["conflicts"] and conflict_policy == "error":
        errors.append("The two button sources disagree")
    if len(buttons["accepted"]) > 4:
        errors.append("More than four demonstrated buttons were identified")
    if errors:
        status = "error"
    elif progress["status"] == "inactive":
        status = "inactive"
    elif progress["frame_range"] is None:
        status = "uncertain"
    else:
        status = "ok" if len(buttons["accepted"]) == 4 else "partial"
    return {"status": status, "errors": errors, "source": "offline_image_pixels", "layout": layout,
            "progress": progress, "bar": bar, "pointer": pointer, "buttons": buttons}


class ProgressTracker:
    """Intersect visual frame ranges with explicitly counted emulator advances.

    Create a new tracker after a load/reset or for a new dance. Counts are
    emulator frames between observations, never host seconds. An obscured image
    can carry an earlier anchor forward, with its nonvisual source made explicit.
    """

    def __init__(self, dance):
        layout_for(dance)
        self.dance = dance
        self.frame_range = None

    def update(self, reading, *, advanced_frames):
        if isinstance(advanced_frames, bool) or not isinstance(advanced_frames, int) or advanced_frames < 0:
            raise ValueError("Supply a nonnegative integer count of advanced emulator frames")
        if reading["layout"]["dance"] != self.dance:
            raise ValueError("Start a new progress tracker for a different dance")
        if reading["status"] == "error":
            raise ValueError("; ".join(reading["errors"]))
        observed = reading["progress"]["frame_range"]
        predicted = [value + advanced_frames for value in self.frame_range] if self.frame_range is not None else None
        if predicted is not None and observed is not None:
            bounds = [max(predicted[0], observed[0]), min(predicted[1], observed[1])]
            if bounds[0] > bounds[1]:
                raise ValueError("Visual progress disagrees with counted frames; inspect capture freshness or a state change")
            source = "visual_range_and_counted_advances"
        else:
            bounds = observed if observed is not None else predicted
            source = "visual_range" if observed is not None else "counted_advances_from_visual_anchor" if predicted else "unreadable"
        self.frame_range = bounds
        return {"dance": self.dance, "frame_range": bounds, "source": source,
                "visually_confirmed": observed is not None,
                "frame_unit": "emulator_frames_from_dance_sweep_start"}


class DemoAccumulator:
    """Remember actual observations within one explicitly selected demo.

    Missing/covered pixels never erase a previously read cue. Start a new object
    for each dance, including the second dance led by the same opponent.
    """

    def __init__(self, dance):
        layout_for(dance)
        self.dance = dance
        self.seen = {}
        self.last_position = None

    def add(self, reading):
        if reading["layout"]["dance"] != self.dance:
            raise ValueError("Start a new accumulator for a different dance")
        if reading["status"] == "error":
            raise ValueError("; ".join(reading["errors"]))
        progress = reading["progress"]
        if progress["phase"] != "demo":
            return self.result()
        bounds = progress["position_bounds"]
        if self.last_position is not None and bounds[1] < self.last_position:
            raise ValueError("Demo progress moved backward; check capture freshness or reset the accumulator")
        self.last_position = max(self.last_position or 8, bounds[0])
        for cue in reading["buttons"]["accepted"]:
            x = cue["demo_x"]
            if x in self.seen and self.seen[x]["button"] != cue["button"]:
                raise ValueError(f"The demonstrated button at x={x} changed")
            self.seen[x] = cue
        if len(self.seen) > 4:
            raise ValueError("More than four demonstrated buttons accumulated")
        return self.result()

    def result(self):
        return {"dance": self.dance, "complete": len(self.seen) == 4,
                "buttons": [self.seen[x] for x in sorted(self.seen)]}


def annotate(image, reading):
    image = native_top(image)
    draw = ImageDraw.Draw(image)
    progress = reading["progress"]
    if progress["position_bounds"]:
        left, right = progress["position_bounds"]
        draw.rectangle((left, 153, right, 160), outline="red")
    for cue in reading["buttons"]["accepted"]:
        for side in cue["evidence"]:
            x = cue["demo_x"] + (120 if side == "right" else 0)
            y = 168 if side == "left" else reading["layout"]["right_marker_y"] - 8
            draw.rectangle((x - 8, y, x + 7, y + 15), outline="lime" if side == cue["source_side"] else "cyan")
    return image


def summarize(reading):
    progress = reading["progress"]
    buttons = reading["buttons"]
    return {
        "status": reading["status"], "errors": reading["errors"],
        "dance": reading["layout"]["dance"], "player_position": reading["layout"]["player_position"],
        "phase": progress["phase"], "position_bounds": progress["position_bounds"],
        "frame_range": progress["frame_range"], "frame_unit": "emulator_frames_from_dance_sweep_start",
        "position_source": progress.get("position_source"), "master_side": buttons["master_side"],
        "visible_buttons_complete": len(buttons["accepted"]) == 4,
        "buttons": [{key: cue[key] for key in ("demo_x", "button", "source_side", "fallback", "cross_checked")}
                    for cue in buttons["accepted"]],
        "conflicts": buttons["conflicts"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--dance", type=int, choices=range(1, 7), required=True)
    parser.add_argument("--conflict-policy", choices=("error", "master"), default="error")
    parser.add_argument("--details", action="store_true", help="Include individual pixel-match evidence")
    parser.add_argument("--annotate", type=Path)
    args = parser.parse_args(argv)
    try:
        image = load_image(args.image)
        result = analyze(image, args.dance, conflict_policy=args.conflict_policy)
        if args.annotate:
            if not args.annotate.parent.is_dir():
                raise ValueError("The annotation output directory must already exist")
            annotate(image, result).save(args.annotate)
    except (OSError, ValueError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(result if args.details else summarize(result), indent=2))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
