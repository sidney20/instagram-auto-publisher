from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import subprocess
from pathlib import Path

from config import DATA_DIR

user32 = ctypes.WinDLL("user32", use_last_error=True)

MARKER_FILE = DATA_DIR / "locked_window.json"

_CHROME_CLASSES = frozenset({"Chrome_WidgetWin_1", "Chrome_WidgetWin_0"})


def _enum_visible_chrome_hwnds() -> set[int]:
    hwnds: set[int] = set()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            if buf.value in _CHROME_CLASSES:
                hwnds.add(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return hwnds


def _get_pids_with_profile(user_data_dir: str) -> set[int]:
    dir_name = Path(user_data_dir).name
    safe = dir_name.replace("'", "''")
    ps = (
        f"(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{safe}*' }}).ProcessId"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=0x08000000,
        )
        pids: set[int] = set()
        for token in r.stdout.replace("\r", "").split("\n"):
            token = token.strip()
            if token.isdigit():
                pids.add(int(token))
        return pids
    except Exception:
        return set()


def find_hwnd(user_data_dir: str) -> int | None:
    pids = _get_pids_with_profile(user_data_dir)
    if not pids:
        return None
    for hwnd in _enum_visible_chrome_hwnds():
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            return int(hwnd)
    return None


def set_enabled(hwnd: int, enabled: bool) -> bool:
    return bool(user32.EnableWindow(hwnd, enabled))


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def save_marker(hwnd: int) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MARKER_FILE.write_text(json.dumps({"hwnd": hwnd}), encoding="utf-8")
    except Exception:
        pass


def clear_marker() -> None:
    try:
        MARKER_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def read_marker_hwnd() -> int | None:
    try:
        data = json.loads(MARKER_FILE.read_text(encoding="utf-8"))
        h = data.get("hwnd")
        return int(h) if h is not None else None
    except Exception:
        return None


def recover_orphans() -> str | None:
    hwnd = read_marker_hwnd()
    if hwnd is None:
        return None
    if is_window(hwnd):
        set_enabled(hwnd, True)
        clear_marker()
        return f"Janela órfã (HWND {hwnd}) destravada automaticamente"
    clear_marker()
    return None
