"""Execute the local Lua 5.1 DLL with independently constructed input literals."""

from __future__ import annotations

import ctypes
import json

from tests.support.paths import ROOT


class Lua51:
    def __init__(self):
        self.dll = ctypes.WinDLL(str(ROOT / "desmume-0.9.13-win64" / "lua51.dll"))
        signatures = {
            "luaL_newstate": ([], ctypes.c_void_p),
            "luaL_openlibs": ([ctypes.c_void_p], None),
            "luaL_loadbuffer": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p], ctypes.c_int),
            "lua_pcall": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int], ctypes.c_int),
            "lua_tolstring": ([ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_void_p),
            "lua_settop": ([ctypes.c_void_p, ctypes.c_int], None),
            "lua_close": ([ctypes.c_void_p], None),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = arguments, result
        self.state = self.dll.luaL_newstate()
        if not self.state:
            raise RuntimeError("Lua state allocation failed.")
        self.dll.luaL_openlibs(self.state)

    def run(self, source: str) -> str:
        self.dll.lua_settop(self.state, 0)
        code = source.encode("utf-8")
        error = self.dll.luaL_loadbuffer(self.state, code, len(code), b"@offline-verification")
        if not error:
            error = self.dll.lua_pcall(self.state, 0, 1, 0)
        length = ctypes.c_size_t()
        pointer = self.dll.lua_tolstring(self.state, -1, ctypes.byref(length))
        result = ctypes.string_at(pointer, length.value).decode("utf-8") if pointer else ""
        if error:
            raise AssertionError(result)
        return result

    def close(self):
        self.dll.lua_close(self.state)


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
