"""Emulator and ROM file selection."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class OpenFilename(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("owner", wintypes.HWND), ("instance", wintypes.HINSTANCE),
        ("filter", wintypes.LPCWSTR), ("custom_filter", wintypes.LPWSTR),
        ("custom_size", wintypes.DWORD), ("filter_index", wintypes.DWORD),
        ("file", wintypes.LPWSTR), ("file_size", wintypes.DWORD),
        ("file_title", wintypes.LPWSTR), ("title_size", wintypes.DWORD),
        ("initial_dir", wintypes.LPCWSTR), ("title", wintypes.LPCWSTR),
        ("flags", wintypes.DWORD), ("file_offset", wintypes.WORD), ("extension_offset", wintypes.WORD),
        ("default_extension", wintypes.LPCWSTR), ("custom_data", wintypes.LPARAM),
        ("hook", ctypes.c_void_p), ("template", wintypes.LPCWSTR),
        ("reserved", ctypes.c_void_p), ("reserved_word", wintypes.DWORD), ("flags_ex", wintypes.DWORD),
    ]


def pick_file(title, folder, label, pattern):
    if os.name != "nt":
        from tkinter import Tk, filedialog

        root = Tk()
        root.withdraw()
        try:
            selected = filedialog.askopenfilename(title=title, parent=root,
                initialdir=str(folder if folder.is_dir() else folder.parent),
                filetypes=[(label, pattern), ("All files", "*")])
            return Path(selected).resolve() if selected else None
        finally:
            root.destroy()
    api = ctypes.WinDLL("comdlg32", use_last_error=True)
    api.GetOpenFileNameW.argtypes = [ctypes.POINTER(OpenFilename)]
    api.GetOpenFileNameW.restype = wintypes.BOOL
    api.CommDlgExtendedError.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    dialog = OpenFilename()
    dialog.size = ctypes.sizeof(dialog)
    dialog.filter = f"{label}\0{pattern}\0All files\0*.*\0\0"
    dialog.filter_index = 1
    dialog.file = ctypes.cast(buffer, wintypes.LPWSTR)
    dialog.file_size = len(buffer)
    dialog.initial_dir = str(folder if folder.is_dir() else folder.parent)
    dialog.title = title
    dialog.flags = 0x80000 | 0x1000 | 0x800 | 0x8  # Explorer, existing file/path, preserve working directory.
    if api.GetOpenFileNameW(ctypes.byref(dialog)):
        return Path(buffer.value).resolve()
    if api.CommDlgExtendedError():
        raise RuntimeError("The file selection window could not be opened.")
    return None
