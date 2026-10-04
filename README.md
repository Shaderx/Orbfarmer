<div align="center">

# Orbfarmer

**Game sessions, thoughtfully presented.**

A Windows-first companion for Discord game-detection experiments.
Steam artwork. Visible timers. Local, scoped cleanup.

[![Build and release](https://github.com/Shaderx/Orbfarmer/actions/workflows/release.yml/badge.svg)](https://github.com/Shaderx/Orbfarmer/actions/workflows/release.yml)
[![Latest release](https://img.shields.io/github/v/release/Shaderx/Orbfarmer)](https://github.com/Shaderx/Orbfarmer/releases/latest)
[![Windows x64](https://img.shields.io/badge/platform-Windows_x64-0078D4)](https://github.com/Shaderx/Orbfarmer/releases/latest)

[Get started](#get-started) · [Build](#build) · [Releases](https://github.com/Shaderx/Orbfarmer/releases)

<img src="docs/timer.png" alt="Orbfarmer timer with Dragon's Dogma II artwork and a live countdown" width="760">

</div>

### Small by design

- **Find a game** through Discord's database or Steam's catalog, or enter an executable path.
- **Keep it visible** with a game-themed window, Steam hero image and icon, sampled accent colors, and an elapsed-time counter (HH:MM:SS). Sessions run until you close the timer or stop them in the main app.
- **Choose the output.** Local simulations use `simulations/<game path>/` beside the app. Steam games also offer an optional **Steam library + ACF** session for detection that requires an installed-game manifest.
- **Reuse an executable.** If the file exists, press Enter to run it. Choose **Replace** to recreate it, or **Cancel** to leave the session.
- **Clean up deliberately.** Steam mode's Enter-to-stop action removes only that session's unchanged, owned files. Reused executables and existing Steam manifests are preserved.

### Get started

Download `Orbfarmer-Windows-x86_64.zip` from [Releases](https://github.com/Shaderx/Orbfarmer/releases/latest), extract it into a writable folder, and launch `Orbfarmer.exe` with the Discord desktop app open. Choose a game and keep its timer window open. In Steam mode, return to the main wizard and press **Enter** to stop and clean up.

Windows is the current development and release focus. **macOS and Linux support is on hold for a future release.** Their initial v1.0.0 packages compiled successfully, but compilation does not establish working Discord detection or feature parity. Treat those existing downloads as experimental and unvalidated for everyday use; future releases currently package Windows only.

The counter measures local time, **not verified quest progress**. There is no 15-minute cutoff. Check Discord for detection and completion. Behavior varies by game; artwork falls back gracefully when unavailable.

### Steam library + ACF

Choose **Steam library + ACF** after selecting a Steam game, or a Discord database game with a Steam AppID. Select the Steam installation folder or another library root, such as `D:\SteamLibrary`, which must contain `steamapps`. The app shows the executable and `appmanifest_<AppID>.acf` paths before setup. Local simulation remains the default.

The library session places the executable under `steamapps/common/<install folder>/` and creates a missing ACF manifest in the same library's `steamapps` folder. If a manifest already exists, the app uses its installation folder and preserves its contents. Press Enter in the main app to stop the session and remove unchanged files created by that session. If Steam changes a generated manifest, cleanup preserves it.

If the selected writes need administrator access, the app explains which paths need it before Windows displays a UAC prompt. You can cancel that prompt to cancel setup. The app keeps the timer at your normal user privileges and uses the approved filesystem helper for preparation and cleanup. Writable libraries, and Run-existing sessions with an existing manifest, do not request elevation.

For **EA SPORTS FC 27**, the app uses AppID `4080220`, folder `EA SPORTS FC 27`, and executable `fc27.exe`. Its new ACF contains only `appid`, `name`, and `installdir`, following the variant supplied in the [FC27 community comments](https://www.reddit.com/r/DiscordQuests/comments/1wq2m86/ea_sports_fc_27_quest/). This FC27 recipe omits `StateFlags`; other games retain `StateFlags = 4`. The [Steam listing](https://store.steampowered.com/app/4080220/) confirms the AppID. If Discord does not start tracking, keep the library session running while you fully restart Steam and Discord. This keeps the manifest available when the clients restart.

### Build

Windows and Python 3.12 with Tcl/Tk are required for the supported build.

```powershell
git clone https://github.com/Shaderx/Orbfarmer.git
cd Orbfarmer
python -m venv .venv
.\.venv\Scripts\python -m pip install --only-binary=:all: -r requirements-build.txt
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python build.py
```

The executable is written to `dist-local/Orbfarmer.exe`. For source use, run `python orbfarmer.py` after installing `requirements.txt`.

Pushing a semantic-version tag such as `v1.2.3` automatically runs the tests, builds and packages Windows, generates checksums, and publishes a GitHub Release. Maintainers can also start the workflow manually from the [Actions page](https://github.com/Shaderx/Orbfarmer/actions/workflows/release.yml).

### Validation

The [v1.0.0 GitHub Actions run](https://github.com/Shaderx/Orbfarmer/actions/runs/34208339679) passed all **66 Windows tests**, built the executable with PyInstaller, and uploaded the release archive and `SHA256SUMS.txt`. The tests include timer-window visibility and deadline checks, configuration loading, scoped cleanup, and update-notification behavior. macOS and Linux each passed 64 tests with the two Windows window tests skipped; runtime support remains on hold.

To check a Windows download, compare this command's output with its entry in the release's `SHA256SUMS.txt`:

```powershell
Get-FileHash .\Orbfarmer-Windows-x86_64.zip -Algorithm SHA256
```

Checksums verify file integrity; they are not a publisher signature. Automated tests do not verify Discord quest completion or rewards.

Compiled builds create `settings.json` beside the executable. Sessions count up until stopped; the legacy `TIMER_MINUTES` setting is ignored. Defaults include `simulations/` output and `AUTO_DELETE: false`. Steam mode's explicit stop cleanup works independently of `AUTO_DELETE`; other modes use it for app-exit cleanup.

### Credits

Maintained by **shaderx** — detection fixes, scoped cleanup, artwork-driven UI, and the Orbfarmer redesign.

Based on [orbshacker](https://github.com/strykey/orbshacker), with original work credited to Strykey, Daniel Pires, and Pannenkoekisus. Steam artwork belongs to its respective owners. Original copyright and [license notices](LICENSE) are retained.

For educational use. Follow the applicable platform terms. No detection or reward guarantees.
