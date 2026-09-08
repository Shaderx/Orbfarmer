#!/usr/bin/env python3
"""
Backward-compatible entry point.

Usage:
    python orbfarmer.py         (main menu)
    python -m orbfarmer         (package-style)

When launched with --timer-mode (by a renamed copy of itself),
it runs the 15-minute timer instead of the main menu.
"""

import sys
import os
from pathlib import Path

# ── Safety: redirect stdio to devnull when running without a console ──────────
# PyInstaller --noconsole (or pythonw) sets sys.stdout/stderr/stdin to None.
# Redirect to devnull so print() / input() don't crash the whole app.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r")


def is_faked_game() -> bool:
    """Check if the currently running executable/script is a faked game copy."""
    if getattr(sys, "frozen", False):
        # Normalize both separator styles so release behavior can be tested on
        # every runner, and recognize the extensionless Unix executable.
        name = str(sys.executable).replace("\\", "/").rsplit("/", 1)[-1].lower()
        return name not in ("orbfarmer", "orbfarmer.exe")
    else:
        name = Path(sys.argv[0]).name.lower()
        return name not in ("orbfarmer.py", "__main__.py") and "pytest" not in name

def show_console() -> None:
    """Allocate and show a Windows console window if running on Windows."""
    if sys.platform == "win32":
        try:
            import ctypes
            # Only allocate a console if we don't have one already
            if not ctypes.windll.kernel32.GetConsoleWindow():
                # Try to attach to parent console first
                if not ctypes.windll.kernel32.AttachConsole(-1):
                    # Otherwise, allocate a new console window
                    ctypes.windll.kernel32.AllocConsole()
                
                # Reopen standard streams
                sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8")
                sys.stdin = open("CONIN$", "r", encoding="utf-8")
        except Exception:
            pass


if __name__ == "__main__":
    if is_faked_game() or "--timer-mode" in sys.argv:
        from orbfarmer.timer import run_timer
        try:
            idx = sys.argv.index("--timer-mode")
            minutes = int(sys.argv[idx + 1])
        except (ValueError, IndexError):
            from orbfarmer import config
            minutes = config.TIMER_MINUTES
        from orbfarmer import config
        run_timer(minutes, theme=config.TIMER_THEME)
    else:
        show_console()
        from orbfarmer.main import main
        from orbfarmer.ui import print_color, Colors

        try:
            main()
        except KeyboardInterrupt:
            print_color("\n\n[!] Interrupted", Colors.YELLOW)
            sys.exit(0)
        except Exception as e:
            print_color(f"\n[ERROR] Fatal error: {e}", Colors.RED, bold=True)
            import traceback
            traceback.print_exc()
            input("\nPress Enter to exit...")
            sys.exit(1)
