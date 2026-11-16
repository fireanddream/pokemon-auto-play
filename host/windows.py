"""Native Windows process, window, menu, and notification primitives."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
        ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
        ("parent_pid", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
        ("executable", wintypes.WCHAR * 260),
    ]


USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL32.OpenProcess.restype = wintypes.HANDLE
KERNEL32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL
KERNEL32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL32.CloseHandle.restype = wintypes.BOOL
KERNEL32.GetConsoleWindow.argtypes = []
KERNEL32.GetConsoleWindow.restype = wintypes.HWND
KERNEL32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
KERNEL32.GetConsoleProcessList.restype = wintypes.DWORD
KERNEL32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
KERNEL32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
KERNEL32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
KERNEL32.Process32FirstW.restype = wintypes.BOOL
KERNEL32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
KERNEL32.Process32NextW.restype = wintypes.BOOL
KERNEL32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
KERNEL32.TerminateProcess.restype = wintypes.BOOL
ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
USER32.EnumWindows.argtypes = [ENUMPROC, wintypes.LPARAM]
USER32.EnumChildWindows.argtypes = [wintypes.HWND, ENUMPROC, wintypes.LPARAM]
USER32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
USER32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
USER32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER32.GetDlgCtrlID.argtypes = [wintypes.HWND]
USER32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
USER32.GetDlgItem.restype = wintypes.HWND
USER32.GetMenu.argtypes = [wintypes.HWND]
USER32.GetMenu.restype = wintypes.HMENU
USER32.GetMenuItemCount.argtypes = [wintypes.HMENU]
USER32.GetSubMenu.argtypes = [wintypes.HMENU, ctypes.c_int]
USER32.GetSubMenu.restype = wintypes.HMENU
USER32.GetMenuItemID.argtypes = [wintypes.HMENU, ctypes.c_int]
USER32.GetMenuItemID.restype = wintypes.UINT
USER32.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
USER32.GetMenuState.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
USER32.GetMenuState.restype = wintypes.UINT
USER32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
USER32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
USER32.SendMessageTimeoutW.restype = wintypes.LPARAM
USER32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
USER32.SendMessageW.restype = wintypes.LPARAM
USER32.IsWindow.argtypes = [wintypes.HWND]
USER32.SetForegroundWindow.argtypes = [wintypes.HWND]
USER32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


def window_text(hwnd: int) -> str:
    size = USER32.GetWindowTextLengthW(hwnd) + 1
    buffer = ctypes.create_unicode_buffer(size)
    USER32.GetWindowTextW(hwnd, buffer, size)
    return buffer.value


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    USER32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def control_text(hwnd: int) -> str:
    length = send(hwnd, 0xE)
    buffer = ctypes.create_unicode_buffer(length + 1)
    send(hwnd, 0xD, length + 1, ctypes.addressof(buffer))
    return buffer.value


def windows(pid: int, parent: int | None = None) -> list[dict]:
    result = []

    @ENUMPROC
    def collect(hwnd: int, unused: int) -> bool:
        process_id = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid:
            result.append({
                "handle": hwnd, "class": window_class(hwnd),
                "id": USER32.GetDlgCtrlID(hwnd),
                "text": window_text(hwnd) if parent is None else control_text(hwnd),
            })
        return True

    if parent is None:
        USER32.EnumWindows(collect, 0)
    else:
        USER32.EnumChildWindows(parent, collect, 0)
    return result


def menu_items(menu: int, prefix: str = "") -> list[dict]:
    result = []
    if not menu:
        return result
    for position in range(USER32.GetMenuItemCount(menu)):
        text = ctypes.create_unicode_buffer(512)
        USER32.GetMenuStringW(menu, position, text, len(text), 0x400)
        path = f"{prefix}/{text.value}" if prefix else text.value
        submenu = USER32.GetSubMenu(menu, position)
        if submenu:
            result.extend(menu_items(submenu, path))
        else:
            result.append({
                "id": USER32.GetMenuItemID(menu, position), "text": path,
                "checked": bool(USER32.GetMenuState(menu, position, 0x400) & 0x8),
            })
    return result


def send(hwnd: int, message: int, wparam: int = 0, lparam: int = 0, *, timeout: float | None = 3) -> int:
    if timeout is None:
        return USER32.SendMessageW(hwnd, message, wparam, lparam)
    result = ctypes.c_size_t()
    ctypes.set_last_error(0)
    ok = USER32.SendMessageTimeoutW(hwnd, message, wparam, lparam, 0x2, int(timeout * 1000), ctypes.byref(result))
    if not ok:
        error = ctypes.get_last_error()
        if error in (0, 1460):
            raise TimeoutError("timeout")
        raise RuntimeError(f"The emulator window command failed (Windows error {error}).")
    return result.value


def post_menu(hwnd: int, identifier: int) -> None:
    if not USER32.PostMessageW(hwnd, 0x111, identifier, 0):
        raise ctypes.WinError(ctypes.get_last_error())


def process_alive(pid: int) -> bool:
    process = KERNEL32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    try:
        code = wintypes.DWORD()
        if not KERNEL32.GetExitCodeProcess(process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259  # STILL_ACTIVE; terminated processes can retain stale windows.
    finally:
        KERNEL32.CloseHandle(process)


def hide_private_console() -> bool:
    """Hide a direct Python launch's console when all attached clients are Python."""
    console = KERNEL32.GetConsoleWindow()
    if not console:
        return False
    identifiers = (wintypes.DWORD * 16)()
    count = KERNEL32.GetConsoleProcessList(identifiers, len(identifiers))
    if not 0 < count <= len(identifiers):
        return False
    for pid in identifiers[:count]:
        process = KERNEL32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            image = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(image))
            if not KERNEL32.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
                return False
            if image.value.rsplit("\\", 1)[-1].lower() not in {"py.exe", "pyw.exe", "python.exe", "pythonw.exe"}:
                return False
        finally:
            KERNEL32.CloseHandle(process)
    USER32.ShowWindow(console, 0)
    return True


def emulator_process_ids() -> list[int]:
    """Include terminating processes until Windows removes them from its process table."""
    snapshot = KERNEL32.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        ready = KERNEL32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ready:
            name = entry.executable.lower()
            if name.startswith("desmume") and name.endswith(".exe"):
                result.append(entry.pid)
            ready = KERNEL32.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES.
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        KERNEL32.CloseHandle(snapshot)
    return sorted(result)


def terminate_process(pid: int) -> None:
    process = KERNEL32.OpenProcess(0x1 | 0x1000, False, pid)
    if not process:
        if not process_alive(pid):
            return
        raise RuntimeError("Could not close an existing DeSmuME process. Close it manually and try again.")
    try:
        if not KERNEL32.TerminateProcess(process, 0) and process_alive(pid):
            raise RuntimeError("Could not close an existing DeSmuME process. Close it manually and try again.")
    finally:
        KERNEL32.CloseHandle(process)


def emulator_window_process_ids() -> list[int]:
    """Also retain a launch barrier for emulator windows left behind during shutdown."""
    result = set()

    @ENUMPROC
    def collect(hwnd: int, unused: int) -> bool:
        if window_class(hwnd).lower().startswith("desmume"):
            process_id = wintypes.DWORD()
            USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value:
                result.add(process_id.value)
        return True

    if not USER32.EnumWindows(collect, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return sorted(result)


def alert(window: int) -> None:
    import winsound

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.UINT), ("window", wintypes.HWND),
            ("flags", wintypes.DWORD), ("count", wintypes.UINT), ("timeout", wintypes.DWORD),
        ]

    flash = FLASHWINFO(ctypes.sizeof(FLASHWINFO), window, 2, 6, 0)
    USER32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
    USER32.FlashWindowEx(ctypes.byref(flash))
    winsound.MessageBeep(winsound.MB_ICONASTERISK)
