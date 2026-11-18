"""Execute the local Lua 5.1 DLL with independently constructed input literals."""

from __future__ import annotations

import json

from host.lua51 import Lua51 as RuntimeLua51
from tests.support.paths import lua_dll_path


class Lua51(RuntimeLua51):
    def __init__(self):
        super().__init__(lua_dll_path())


def literal(value) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(f"[{literal(key)}]={literal(item)}" for key, item in value.items()) + "}"
    if isinstance(value, (tuple, list)):
        return "{" + ",".join(literal(item) for item in value) + "}"
    if isinstance(value, str):
        escapes = {'"': '\\"', "\\": "\\\\"}
        text = "".join(
            escapes.get(character, f"\\{ord(character):03d}" if ord(character) < 32 else character)
            for character in value
        )
        return '"' + text + '"'
    if value is None:
        return "nil"
    return json.dumps(value, ensure_ascii=False)
