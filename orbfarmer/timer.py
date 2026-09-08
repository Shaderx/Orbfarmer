"""Standalone timer with a visible Windows window for game detection."""

import sys
import time


def run_timer(minutes: int = 15, theme: dict | None = None) -> None:
    """Count elapsed time until stopped; minutes is retained for old callers.

    File deletion is intentionally owned by ``GameFaker.cleanup`` in the main
    process, where hashes and creation records can be checked safely.
    """
    seconds = 0
    if sys.platform != "win32":
        while True:
            time.sleep(1)

    try:
        import tkinter as tk
    except ImportError:
        _run_native_timer(seconds)
        return
    try:
        _run_themed_timer(seconds, theme if isinstance(theme, dict) else {})
    except tk.TclError:
        # A missing Tcl runtime must not turn the game back into a headless process.
        _run_native_timer(seconds)


def _run_themed_timer(seconds: float, theme: dict) -> None:
    import re
    from pathlib import Path
    import tkinter as tk
    from tkinter import font as tkfont

    root = tk.Tk()
    root.withdraw()
    root.tk.call("tk", "scaling", 4 / 3)
    title = str(theme.get("name") or Path(sys.executable).stem)[:160]
    root.title(f"{title} | Orbfarmer")
    root.geometry("820x512")
    root.resizable(False, False)
    root.configure(bg="#11151b")
    accent = theme.get("accent", "#aac9b5")
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = "#aac9b5"
    canvas = tk.Canvas(root, width=820, height=512, bg="#11151b", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    photos = []
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(sys.argv[0]).resolve().parent

    def photo(kind):
        filename = theme.get(kind)
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        try:
            result = tk.PhotoImage(master=root, file=str(base / filename))
            if result.width() > 2048 or result.height() > 2048:
                return None
            photos.append(result)
            return result
        except (tk.TclError, OSError):
            return None

    hero = photo("hero")
    if hero:
        canvas.create_image(0, 0, image=hero, anchor="nw")
    else:
        # A restrained gradient keeps the same layout when Steam artwork is unavailable.
        for y in range(320):
            strength = (1 - y / 320) * 0.18
            rgb = [round(int(accent[i:i + 2], 16) * strength + dark * (1 - strength))
                   for i, dark in zip((1, 3, 5), (17, 21, 27))]
            canvas.create_line(0, y, 820, y, fill="#" + "".join(f"{c:02x}" for c in rgb))

    canvas.create_text(36, 32, text="O R B F A R M E R", fill="#ffffff", font=("Segoe UI", 10, "bold"), anchor="w")
    canvas.create_rectangle(652, 20, 784, 47, fill="#11151b", outline="")
    canvas.create_oval(665, 30, 671, 36, fill=accent, outline="")
    canvas.create_text(681, 33, text="SESSION ACTIVE", fill="#e5e9e9", font=("Segoe UI", 8, "bold"), anchor="w")

    icon = photo("icon")
    canvas.create_rectangle(35, 209, 101, 275, fill="#1c222b", outline="#46505a")
    if icon:
        root.iconphoto(True, icon)
        canvas.create_image(36, 210, image=icon, anchor="nw")
    else:
        canvas.create_text(68, 242, text=title[:1].upper(), fill=accent, font=("Segoe UI", 24, "bold"))
    canvas.create_text(120, 210, text="STEAM SESSION" if theme.get("steam_appid") else "GAME SESSION",
                       fill=accent, font=("Segoe UI", 9, "bold"), anchor="nw")
    title_font = tkfont.Font(root=root, family="Segoe UI", size=25, weight="bold")
    while title_font.measure(title) > 655 and title_font.cget("size") > 16:
        title_font.configure(size=title_font.cget("size") - 1)
    canvas.create_text(118, 233, text=title, fill="#f7f8fa", font=title_font, anchor="nw", width=655)
    canvas.create_text(36, 298, text="Keep this window open while your session runs.",
                       fill="#a5acb7", font=("Segoe UI", 10), anchor="w")

    canvas.create_line(36, 326, 784, 326, fill="#2b323c")
    canvas.create_text(36, 351, text="TIME ELAPSED", fill="#9ca6b4", font=("Segoe UI", 9, "bold"), anchor="w")
    countdown = canvas.create_text(32, 389, text="", fill="#f5f7fa", font=("Consolas", 38, "bold"), anchor="w")
    duration = "Runs until you stop it"
    canvas.create_text(784, 373, text=duration, fill="#e6e9ed", font=("Segoe UI", 11), anchor="e")
    canvas.create_text(784, 397, text="Check quest progress in Discord", fill="#939eac", font=("Segoe UI", 9), anchor="e")
    canvas.create_rectangle(36, 431, 784, 435, fill="#2b323c", outline="")
    canvas.create_text(36, 462, text="Press Enter in the main app to stop and clean up Steam sessions.",
                       fill="#9ca6b4", font=("Segoe UI", 9), anchor="w")
    canvas.create_text(36, 490, text="Improvements by shaderx", fill=accent, font=("Segoe UI", 9), anchor="w")
    canvas.create_text(784, 490, text="Artwork from Steam" if hero or icon else "ORBFARMER",
                       fill="#788493", font=("Segoe UI", 8), anchor="e")

    # Keep native window controls and a taskbar entry for reliable game detection.
    root.update_idletasks()
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        hwnd = user32.GetParent(root.winfo_id())
        dark = ctypes.c_int(1)
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
    except (AttributeError, OSError):
        pass

    started = time.monotonic()

    def tick():
        total = max(0, int(time.monotonic() - started))
        canvas.itemconfigure(countdown, text=f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}")
        root.after(200, tick)

    root.deiconify()
    tick()
    try:
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def _run_native_timer(seconds: float) -> None:

    import ctypes
    from ctypes import wintypes
    from pathlib import Path

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND,
        wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.argtypes = [wintypes.HWND]

    # A windowless sleeping process was omitted from Discord's application list.
    # Use a real top-level window and pump messages so it stays responsive.
    window = user32.CreateWindowExW(
        0, "STATIC", f"Orbfarmer timer: {Path(sys.executable).stem}",
        0x10CF0000, 160, 160, 520, 150, None, None, None, None,
    )  # WS_VISIBLE | WS_OVERLAPPEDWINDOW
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())

    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    started = time.monotonic()
    message = wintypes.MSG()
    try:
        while user32.IsWindow(window):
            total = max(0, int(time.monotonic() - started))
            user32.SetWindowTextW(window, f"Orbfarmer | Time elapsed {total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}")
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(0.05)
    finally:
        if user32.IsWindow(window):
            user32.DestroyWindow(window)
