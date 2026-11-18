"""Native Windows UI checks scoped to the synthetic test process family."""

import ctypes
from ctypes import wintypes
import json
import subprocess
import time

from host import windows


windows.USER32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
windows.USER32.PostMessageW.restype = wintypes.BOOL
windows.KERNEL32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
windows.KERNEL32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
windows.KERNEL32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(windows.ProcessEntry)]
windows.KERNEL32.Process32FirstW.restype = wintypes.BOOL
windows.KERNEL32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(windows.ProcessEntry)]
windows.KERNEL32.Process32NextW.restype = wintypes.BOOL
windows.KERNEL32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
windows.KERNEL32.GetProcessTimes.restype = wintypes.BOOL


def creation_time(pid):
    process = windows.KERNEL32.OpenProcess(0x1000, False, pid)
    if not process:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not windows.KERNEL32.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return created.dwLowDateTime | created.dwHighDateTime << 32
    finally:
        windows.KERNEL32.CloseHandle(process)


def process_family(parent):
    started = creation_time(parent)
    if started is None:
        return {parent}
    snapshot = windows.KERNEL32.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    relations = {}
    try:
        entry = windows.ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        ready = windows.KERNEL32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ready:
            if entry.executable.lower() in {"python.exe", "pythonw.exe"}:
                relations[entry.pid] = entry.parent_pid
            ready = windows.KERNEL32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        windows.KERNEL32.CloseHandle(snapshot)
    family = {parent}
    while True:
        children = {pid for pid, owner in relations.items() if owner in family
            and (creation_time(pid) or 0) >= started}
        if children <= family:
            return family
        family.update(children)


def launch(checks, arguments):
    pythonw = checks.bundle / "contest/.venv/Scripts/pythonw.exe"
    return subprocess.Popen([str(pythonw), "-I", "-B", *map(str, arguments)], cwd=checks.bundle,
        env=checks.environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)


def cleanup(child, family):
    if child.poll() is None:
        for pid in family - {child.pid}:
            windows.terminate_process(pid)
        child.terminate()
        child.wait()


def check_error_dialog(checks):
    child = launch(checks, [checks.launcher, "--invalid-release-test-option"])
    family = {child.pid}
    handle = None
    try:
        limit = time.monotonic() + 15  # Test-only host seconds; no emulator timing is changed.
        while time.monotonic() < limit and child.poll() is None:
            family.update(process_family(child.pid))
            matches = [window for pid in family for window in windows.windows(pid)
                if window["class"] == "#32770" and "Pokemon Auto Play" in window["text"]]
            if matches:
                handle = matches[0]["handle"]
                break
            time.sleep(0.05)
        if handle is None:
            raise RuntimeError("Windowless startup did not expose its native error dialog.")
        text = "\n".join(window["text"] for pid in family for window in windows.windows(pid, handle))
        if "--invalid-release-test-option" not in text or "--help" not in text or "startup.log" not in text:
            raise RuntimeError("The native startup error dialog is missing actionable diagnostics.")
        if not windows.USER32.PostMessageW(handle, 0x10, 0, 0) or child.wait(timeout=15) != 1:
            raise RuntimeError("The native startup error dialog did not dismiss and exit cleanly.")
    finally:
        family.update(process_family(child.pid))
        cleanup(child, family)
    checks.log("Native windowless error dialog is visible, actionable, logged, and dismisses cleanly.")


def check_setup_progress(checks):
    checks.command([checks.python, "-B", "-m", "pip", "uninstall", "--yes", "Pillow"], cwd=checks.bundle)
    receipt = checks.work / "windowless-progress.json"
    code = "\n".join((
        "import ctypes, json, sys; from pathlib import Path; from contextlib import contextmanager",
        f"sys.path.insert(0, {str(checks.bundle)!r})", "import run_contest; from host import windows",
        "original = windows.setup_console; observed = {}; previous = sys.stdout, sys.stderr",
        "windows.USER32.IsWindowVisible.argtypes = [ctypes.c_void_p]",
        "windows.USER32.IsWindowVisible.restype = ctypes.c_int",
        "@contextmanager",
        "def monitor(quiet):",
        "    with original(quiet):",
        "        handle = windows.KERNEL32.GetConsoleWindow()",
        "        observed['quiet'] = quiet",
        "        observed['visible'] = bool(handle and windows.USER32.IsWindowVisible(handle))",
        "        observed['output_attached'] = sys.stdout is not None and sys.stderr is not None",
        "        yield",
        "    observed['released'] = not windows.KERNEL32.GetConsoleWindow()",
        "    observed['streams_restored'] = sys.stdout is previous[0] and sys.stderr is previous[1]",
        "windows.setup_console = monitor",
        "observed['exit_code'] = run_contest.entry_point(['--check'])",
        f"Path({str(receipt)!r}).write_text(json.dumps(observed) + '\\n', encoding='utf-8', newline='\\n')",
    ))
    child = launch(checks, ["-c", code])
    family = process_family(child.pid)
    try:
        if child.wait(timeout=45) != 0:
            raise RuntimeError("Windowless setup probe exited unsuccessfully.")
    finally:
        family.update(process_family(child.pid))
        cleanup(child, family)
    values = json.loads(receipt.read_text(encoding="utf-8"))
    expected = {"quiet": True, "visible": True, "output_attached": True,
        "released": True, "streams_restored": True, "exit_code": 0}
    if values != expected:
        raise RuntimeError(f"Windowless setup progress/cleanup failed: {values}")
    checks.log("Native offline setup progress is visible; the temporary console and streams are restored.")


def verify_windowless(checks):
    check_error_dialog(checks)
    check_setup_progress(checks)
