"""ROM-header identification; region acceptance is separate from live verification."""

from pathlib import Path


LANGUAGES = {"J": "Japanese", "E": "English"}
GAME_CODES = frozenset("CPU" + region for region in LANGUAGES)


def identify_rom(path):
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(0x160)
    code = header[12:16].decode("ascii", errors="replace")
    if len(header) != 0x160 or code not in GAME_CODES:
        raise ValueError("Select a Japanese (CPUJ) or English (CPUE) Pokemon Platinum .nds file.")
    return {"code": code, "language": LANGUAGES[code[3]], "revision": header[0x1E], "tested": code == "CPUJ"}
