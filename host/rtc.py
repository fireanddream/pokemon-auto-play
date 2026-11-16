"""Full-RTC power-on dialog API shared by emulator operations."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
import time


KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL32.OpenProcess.restype = wintypes.HANDLE
KERNEL32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
KERNEL32.VirtualAllocEx.restype = ctypes.c_void_p
KERNEL32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
KERNEL32.VirtualFreeEx.restype = wintypes.BOOL
KERNEL32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
KERNEL32.WriteProcessMemory.restype = wintypes.BOOL
KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL32.CloseHandle.restype = wintypes.BOOL


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(name, wintypes.WORD) for name in (
        "year", "month", "day_of_week", "day", "hour", "minute", "second", "milliseconds",
    )]


def send_struct(controller, pid: int, window: int, value: SYSTEMTIME) -> None:
    process = KERNEL32.OpenProcess(0x28, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    remote = None
    try:
        remote = KERNEL32.VirtualAllocEx(process, None, ctypes.sizeof(value), 0x3000, 0x04)
        if not remote:
            raise ctypes.WinError(ctypes.get_last_error())
        written = ctypes.c_size_t()
        if not KERNEL32.WriteProcessMemory(process, remote, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(written)):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != ctypes.sizeof(value):
            raise RuntimeError("The RTC control value was not fully copied.")
        controller.send(window, 0x1002, 0, remote)  # DTM_SETSYSTEMTIME.
    finally:
        if remote:
            KERNEL32.VirtualFreeEx(process, remote, 0, 0x8000)
        KERNEL32.CloseHandle(process)


def start_recording(controller, when: datetime, movie: Path, sram: Path | None) -> None:
    data = controller.session()
    controller.set_pause(True)
    controller.post_menu(data["window"], 602)
    deadline = time.monotonic() + 5 if getattr(controller, "timeouts_enabled", True) else None
    controls, dialog = {}, 0
    while deadline is None or time.monotonic() < deadline:
        dialogs = [item for item in controller.windows(data["pid"]) if item["text"] == "Record movie"]
        if len(dialogs) == 1:
            dialog = dialogs[0]["handle"]
            controls = {number: controller.USER32.GetDlgItem(dialog, number) for number in (1, 200, 978, 979, 981, 1027, 1028)}
            if all(controls.values()):
                break
        dialog = 0
        time.sleep(0.05)
    if not dialog:
        raise TimeoutError("timeout")

    if controller.send(controls[981], 0xF0) == 0:
        controller.send(controls[981], 0xF5)  # Power-on rather than current state.
    if bool(controller.send(controls[978], 0xF0)) != (sram is not None):
        controller.send(controls[978], 0xF5)  # Start from SRAM.
    fields = [(200, str(movie))]
    if sram is not None:
        fields.append((979, str(sram)))
    for number, text in fields:
        buffer = ctypes.create_unicode_buffer(text)
        controller.send(controls[number], 0xC, 0, ctypes.addressof(buffer))
    chosen = SYSTEMTIME(when.year, when.month, (when.weekday() + 1) % 7, when.day,
                        when.hour, when.minute, when.second, 0)
    send_struct(controller, data["pid"], controls[1027], chosen)
    send_struct(controller, data["pid"], controls[1028], chosen)
    controller.send(dialog, 0x111, 1, controls[1])
    deadline = time.monotonic() + 5 if getattr(controller, "timeouts_enabled", True) else None
    while deadline is None or time.monotonic() < deadline:
        if not controller.USER32.IsWindow(dialog):
            return
        time.sleep(0.05)
    raise TimeoutError("timeout")
