"""Exercise the actual timer's visibility and lifetime on Windows."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time
import shutil

import pytest

from orbfarmer import config
from orbfarmer.faker import GameFaker, _timer_script_for


@pytest.fixture(scope="session")
def tk_environment(tmp_path_factory):
    import _tkinter
    root = tmp_path_factory.mktemp("tk-runtime")
    result = {}
    for variable, directory in (("TCL_LIBRARY", f"tcl{_tkinter.TCL_VERSION}"),
                                ("TK_LIBRARY", f"tk{_tkinter.TK_VERSION}")):
        shutil.copytree(Path(sys.base_prefix) / "tcl" / directory, root / directory)
        result[variable] = str(root / directory)
    return result


@pytest.mark.skipif(sys.platform != "win32", reason="Windows game detection")
@pytest.mark.parametrize("source_script", [False, True])
def test_timer_has_visible_window_and_exits_at_deadline(tmp_path, monkeypatch, source_script, tk_environment):
    # Windows venv launchers spawn a child; use the interpreter that owns the window.
    python = getattr(sys, "_base_executable", sys.executable)
    if source_script:
        monkeypatch.setattr(config, "TIMER_MINUTES", 0.04)
        faker = GameFaker()
        target = tmp_path / "Game.exe"
        faker.copy_exe_to(target)
        args = [python, str(_timer_script_for(target))]
    else:
        args = [python, "-c", "from orbfarmer.timer import run_timer; run_timer(0.04)"]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env.update(tk_environment)
    proc = subprocess.Popen(args, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    visible = []

    @callback_type
    def inspect_window(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == proc.pid and user32.IsWindowVisible(hwnd):
            visible.append(hwnd)
        return True

    try:
        deadline = time.monotonic() + 2
        while not visible and time.monotonic() < deadline and proc.poll() is None:
            user32.EnumWindows(inspect_window, 0)
            time.sleep(0.05)
        assert visible, "The running timer has no visible window for Discord to enumerate"
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(visible[0], title, len(title))
        assert "| Orbfarmer" in title.value, "The timer fell back to the unthemed window"
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)
