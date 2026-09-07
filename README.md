<div align="center">

# Orbfarmer

**Game sessions, thoughtfully presented.**

A Windows companion for Discord game-detection experiments.
Steam artwork. Visible timers. Local, scoped cleanup.

[Get started](#get-started) · [Build](#build) · [Releases](https://github.com/Shaderx/Orbfarmer/releases)

<img src="docs/timer.png" alt="Orbfarmer timer with Dragon's Dogma II artwork and a live countdown" width="760">

</div>

### Small by design

- **Find a game** through Discord's database or Steam's catalog, or enter an executable path.
- **Keep it visible** with a game-themed window, Steam hero image and icon, sampled accent colors, and a live countdown.
- **Keep it local** in `simulations/<game path>/` beside the app. No Steam-library edits, client injection, or automatic update installation.
- **Clean up deliberately.** Steam mode's Enter-to-stop action removes only that session's unchanged, owned files.

### Get started

Run `Orbfarmer.exe` from a writable folder with the Discord desktop app open. Choose a game and keep its timer window open. In Steam mode, return to the main wizard and press **Enter** to stop and clean up.

The countdown measures local time, **not verified quest progress**. Check Discord for detection and completion. Behavior varies by game; artwork falls back gracefully when unavailable.

### Build

Windows and Python 3.12 with Tcl/Tk are required.

```powershell
git clone https://github.com/Shaderx/Orbfarmer.git
cd Orbfarmer
python -m venv .venv
.\.venv\Scripts\python -m pip install --only-binary=:all: -r requirements-build.txt
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python build.py
```

The executable is written to `dist-local/Orbfarmer.exe`. For source use, run `python orbfarmer.py` after installing `requirements.txt`.

Compiled builds create `settings.json` beside the executable. Defaults: a 15-minute timer, `simulations/` output, and `AUTO_DELETE: false`. Steam mode's explicit stop cleanup works independently of `AUTO_DELETE`; other modes use it for app-exit cleanup.

### Credits

Maintained by **shaderx** — detection fixes, scoped cleanup, artwork-driven UI, and the Orbfarmer redesign.

Based on [orbshacker](https://github.com/strykey/orbshacker), with original work credited to Strykey, Daniel Pires, and Pannenkoekisus. Steam artwork belongs to its respective owners. Original copyright and [license notices](LICENSE) are retained.

For educational use. Follow the applicable platform terms. No detection or reward guarantees.
