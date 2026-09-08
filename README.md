<div align="center">

# Orbfarmer

**Game sessions, thoughtfully presented.**

A cross-platform companion for Discord game-detection experiments.
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

Download the archive for your operating system from [Releases](https://github.com/Shaderx/Orbfarmer/releases), extract it into a writable folder, and launch `Orbfarmer.exe` on Windows or `Orbfarmer` from a terminal on macOS/Linux. Choose a game and keep its timer running. In Steam mode, return to the main wizard and press **Enter** to stop and clean up.

Windows provides the full game-detection experience. macOS and Linux builds are available for compatible Discord/process-detection setups, but Steam registry discovery and the native visible timer window are Windows-specific.

The countdown measures local time, **not verified quest progress**. Check Discord for detection and completion. Behavior varies by game; artwork falls back gracefully when unavailable.

### Build

Python 3.12 with Tcl/Tk is required.

```shell
git clone https://github.com/Shaderx/Orbfarmer.git
cd Orbfarmer
python -m venv .venv
# Activate with `.venv/bin/activate` on macOS/Linux or
# `.venv\Scripts\Activate.ps1` in PowerShell on Windows.
python -m pip install --only-binary=:all: -r requirements-build.txt
python -m pytest -q
python build.py
```

The executable is written to `dist-local/Orbfarmer.exe` on Windows and `dist-local/Orbfarmer` on macOS/Linux. For source use, run `python orbfarmer.py` after installing `requirements.txt`.

Pushing a semantic-version tag such as `v1.2.3` automatically tests and builds all three platforms, generates checksums, and publishes a GitHub Release. Maintainers can also start the same workflow manually from the Actions page.

Compiled builds create `settings.json` beside the executable. Defaults: a 15-minute timer, `simulations/` output, and `AUTO_DELETE: false`. Steam mode's explicit stop cleanup works independently of `AUTO_DELETE`; other modes use it for app-exit cleanup.

### Credits

Maintained by **shaderx** — detection fixes, scoped cleanup, artwork-driven UI, and the Orbfarmer redesign.

Based on [orbshacker](https://github.com/strykey/orbshacker), with original work credited to Strykey, Daniel Pires, and Pannenkoekisus. Steam artwork belongs to its respective owners. Original copyright and [license notices](LICENSE) are retained.

For educational use. Follow the applicable platform terms. No detection or reward guarantees.
