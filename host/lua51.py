"""Explicit-path Lua 5.1 loading and input-free source compilation."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path


class Lua51:
    def __init__(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError(2, "The matching Lua 5.1 DLL is missing", str(path))
        try:
            self.dll = ctypes.CDLL(str(path))
        except OSError as error:
            raise RuntimeError(f"Cannot load {path}. Use matching Python and Lua DLL architectures and the emulator's required DLLs. {error}") from error
        self.state = None
        self._loaded = True
        signatures = {
            "luaL_newstate": ([], ctypes.c_void_p),
            "luaL_openlibs": ([ctypes.c_void_p], None),
            "luaL_loadbuffer": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p], ctypes.c_int),
            "lua_pcall": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int], ctypes.c_int),
            "lua_tolstring": ([ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_void_p),
            "lua_settop": ([ctypes.c_void_p, ctypes.c_int], None),
            "lua_close": ([ctypes.c_void_p], None),
        }
        try:
            for name, (arguments, result) in signatures.items():
                function = getattr(self.dll, name)
                function.argtypes, function.restype = arguments, result
        except AttributeError as error:
            self.close()
            raise RuntimeError(f"{path} does not expose the required Lua 5.1 functions.") from error
        self.state = self.dll.luaL_newstate()
        if not self.state:
            self.close()
            raise RuntimeError("Lua state allocation failed.")
        self.dll.luaL_openlibs(self.state)

    def _text(self):
        length = ctypes.c_size_t()
        pointer = self.dll.lua_tolstring(self.state, -1, ctypes.byref(length))
        return ctypes.string_at(pointer, length.value).decode("utf-8") if pointer else ""

    def run(self, source: str) -> str:
        if not self.state:
            raise RuntimeError("The Lua state is closed.")
        self.dll.lua_settop(self.state, 0)
        code = source.encode("utf-8")
        error = self.dll.luaL_loadbuffer(self.state, code, len(code), b"@offline-verification")
        if not error:
            error = self.dll.lua_pcall(self.state, 0, 1, 0)
        result = self._text()
        if error:
            raise AssertionError(result)
        return result

    def compile_file(self, path):
        """Read through Python for Unicode paths; compile without executing Lua."""
        if not self.state:
            raise RuntimeError("The Lua state is closed.")
        path = Path(path).resolve()
        code = path.read_bytes()
        self.dll.lua_settop(self.state, 0)
        try:
            if self.dll.luaL_loadbuffer(self.state, code, len(code), ("@" + path.as_posix()).encode("utf-8")):
                raise RuntimeError(self._text())
        finally:
            self.dll.lua_settop(self.state, 0)

    def close(self):
        if self.state:
            self.dll.lua_close(self.state)
            self.state = None
        if self._loaded:
            import _ctypes

            self._loaded = False
            if os.name == "nt":
                _ctypes.FreeLibrary(self.dll._handle)
            else:
                _ctypes.dlclose(self.dll._handle)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
