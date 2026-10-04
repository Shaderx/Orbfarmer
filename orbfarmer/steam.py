"""
steam.py – Steam quest helpers: registry, API, appmanifest, and quest mode UI.
"""

import os
import sys
import time
from typing import Any, TypedDict, cast
from pathlib import Path

from _orbfarmer_files import parse_manifest, render_manifest

from . import config
from .path_utils import (
    resolve_within,
    sanitize_filename,
    sanitize_path_segment,
    sanitize_relative_path,
)
from .faker import GameFaker
from .ui import (
    Colors, print_color, print_boxed_title,
    loading_animation, ask_confirm,
)
from .net import fetch_json
from .errors import NetworkError


class SteamAppInfo(TypedDict):
    name: str
    installdir: str
    executable: str
    depot_id: str | None


class SteamStoreItem(TypedDict):
    id: int
    name: str


class SteamLaunchEntry(TypedDict, total=False):
    executable: str
    config: dict[str, str]


SteamLaunchMap = dict[str, SteamLaunchEntry]
SteamDataMap = dict[str, Any]

# Windows registry – optional
try:
    import winreg as _winreg
except ImportError:
    _winreg = None


# ── Registry helpers ──────────────────────────────────────────────────────────

def get_steam_path() -> Path | None:
    """Read Steam installation path from Windows registry."""
    if sys.platform != 'win32' or _winreg is None:
        return None
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        value, _ = _winreg.QueryValueEx(key, "SteamPath")
        _winreg.CloseKey(key)
        return Path(value)
    except Exception:
        fallback = Path("C:/Program Files (x86)/Steam")
        return fallback if fallback.exists() else None


def get_steam_user_id() -> str:
    """Read the currently logged-in Steam user ID from registry."""
    if sys.platform != 'win32' or _winreg is None:
        return "0"
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess")
        value, _ = _winreg.QueryValueEx(key, "ActiveUser")
        _winreg.CloseKey(key)
        steam_id_64 = int(value) + 76561197960265728
        return str(steam_id_64)
    except Exception:
        return "0"


# ── API helpers ───────────────────────────────────────────────────────────────

def _pick_windows_exe(launch: SteamLaunchMap) -> str | None:
    """Return the first Windows .exe found in a SteamCMD launch dict."""
    for key in sorted(launch.keys()):
        entry = launch[key]
        oslist = entry.get("config", {}).get("oslist", "windows")
        if "windows" in oslist or oslist == "":
            exe = entry.get("executable", "")
            if exe.endswith(".exe"):
                return exe.replace("\\", "/")
    return None


def fetch_steam_app_info(appid: int) -> SteamAppInfo | None:
    """Fetch app info from SteamCMD API. Returns dict or None on failure."""
    url = f"{config.STEAMCMD_API_URL}/{appid}"
    try:
        loading_animation(f"Fetching Steam app info for {appid}", 1.2)
        data = cast(SteamDataMap, fetch_json(url))

        data_root = cast(dict[str, SteamDataMap], data.get("data", {}))
        app_data = data_root.get(str(appid), {})
        common_cfg = cast(dict[str, str], app_data.get("common", {}))
        app_cfg = cast(dict[str, Any], app_data.get("config", {}))

        raw_name = common_cfg.get("name", f"App {appid}")
        name = sanitize_filename(raw_name)
        raw_installdir = str(app_cfg.get("installdir", raw_name))
        installdir = sanitize_path_segment(raw_installdir) or name
        launch_map = cast(SteamLaunchMap, app_cfg.get("launch", {}))
        executable = _pick_windows_exe(launch_map)

        if not executable:
            executable = installdir.split("/")[-1] + ".exe"
        executable = sanitize_relative_path(executable)

        depots = cast(dict[str, Any], app_data.get("depots", {}))
        depot_id = next((key for key in depots.keys() if key.isdigit()), None)
        return {"name": name, "installdir": installdir, "executable": executable, "depot_id": depot_id}

    except NetworkError as e:
        print_color(f"[!] SteamCMD API error: {e}", Colors.YELLOW)
        return None


def search_steam_games(query: str) -> list[SteamStoreItem]:
    """Search Steam store. Returns list of {id, name} dicts."""
    try:
        loading_animation(f"Searching Steam for '{query}'", 1.0)
        data = cast(dict[str, Any], fetch_json(
            config.STEAM_STORE_SEARCH_URL,
            params={"term": query, "l": "english", "cc": "US"},
        ))
        return cast(list[SteamStoreItem], data.get("items", []))
    except NetworkError as e:
        print_color(f"[!] Steam search error: {e}", Colors.YELLOW)
        return []


# ── Appmanifest generation and validation ─────────────────────────────────────

_MAX_ACF_BYTES = 4 * 1024 * 1024
_FC27_APPID = 4080220


def _validate_installdir(installdir: str) -> str:
    """Validate a folder name with the shared ACF serializer."""
    render_manifest(1, "Game", installdir)
    return installdir


def _read_appmanifest(appmanifest: Path, appid: int) -> str:
    """Validate a manifest through the standalone filesystem core."""
    if appmanifest.stat().st_size > _MAX_ACF_BYTES:
        raise ValueError("ACF manifest is too large")
    return parse_manifest(appmanifest.read_bytes(), appid)


def generate_appmanifest(appid: int, name: str, installdir: str, steam_path: Path) -> Path | None:
    """Create a minimal appmanifest, using the FC27 recipe for that app only."""
    content = render_manifest(appid, name, installdir).decode("utf-8")
    steamapps = Path(steam_path) / "steamapps"
    manifest = steamapps / f"appmanifest_{appid}.acf"
    created = False
    try:
        if not steamapps.is_dir():
            raise FileNotFoundError(f"Steam library has no steamapps folder: {steamapps}")
        manifest = resolve_within(steamapps, manifest.name)
        with open(manifest, "x", encoding="utf-8", newline="\n") as file:
            created = True
            file.write(content)
        print_color(f"[OK] Created appmanifest: {manifest}", Colors.GREEN, bold=True)
        return manifest
    except Exception as error:
        if created:
            try:
                manifest.unlink(missing_ok=True)
            except OSError:
                print_color(f"[!] Could not remove incomplete appmanifest: {manifest}", Colors.YELLOW)
        print_color(f"[ERROR] Failed to write appmanifest: {error}", Colors.RED, bold=True)
        return None

# ── Interactive UI for Steam Quest Mode ───────────────────────────────────────

def _resolve_steam_path() -> Path | None:
    """Auto-detect or prompt for Steam path."""
    steam_path = get_steam_path()
    if steam_path and steam_path.exists():
        return steam_path
    print_color("[!] Could not locate Steam automatically.", Colors.YELLOW)
    manual = input(
        f"{Colors.BOLD}Enter Steam path manually{Colors.RESET}"
        " (e.g. C:/Program Files (x86)/Steam): "
    ).strip()
    if not manual:
        print_color("[!] No Steam path provided. Aborting.", Colors.RED)
        return None
    return Path(manual)


def _pick_steam_game(query: str) -> SteamStoreItem | None:
    """Search Steam and let the user choose a game."""
    results = search_steam_games(query)
    if not results:
        print_color(f"\n[ERROR] No results found for '{query}'", Colors.RED)
        print_color("[!] Try a different search term", Colors.YELLOW)
        time.sleep(config.SLEEP_LONG)
        return None

    print(f"\n{Colors.BOLD}{Colors.GREEN}Found {len(results)} result(s):{Colors.RESET}\n")
    print(f"{Colors.GRAY}{'─' * 60}{Colors.RESET}")
    for idx, game in enumerate(results, 1):
        print(f"  {Colors.BOLD}{Colors.CYAN}{idx:2d}.{Colors.RESET} {Colors.WHITE}{game['name']}{Colors.RESET}  {Colors.GRAY}(AppID: {game['id']}){Colors.RESET}")
        if idx < len(results):
            print(f"{Colors.GRAY}{'─' * 60}{Colors.RESET}")
    print()

    raw = input(f"{Colors.BOLD}Select [1-{len(results)}]{Colors.RESET} (or 'back'): ").strip()
    if raw.lower() in ('back', 'b', ''):
        return None
    try:
        idx = int(raw)
        if not 1 <= idx <= len(results):
            raise ValueError
    except ValueError:
        print_color("[ERROR] Invalid selection.", Colors.RED)
        time.sleep(config.SLEEP_SHORT)
        return None
    return results[idx - 1]


def _steam_profile(appid: int) -> dict[str, str] | None:
    if appid == _FC27_APPID:
        return {"installdir": "EA SPORTS FC 27", "executable": "fc27.exe"}
    return None


def _prompt_app_info_manually(appid: int) -> SteamAppInfo:
    """Fallback: ask user to type Steam app info."""
    print_color("[!] Could not fetch app info automatically.", Colors.YELLOW)
    print_color("[*] Enter details manually:", Colors.CYAN)
    profile = _steam_profile(appid) or {}
    name_raw = input(f"  {Colors.BOLD}Game name{Colors.RESET}: ").strip() or f"App {appid}"
    install_raw = input(
        f"  {Colors.BOLD}Install dir{Colors.RESET} (folder in steamapps/common) "
        f"[{profile.get('installdir', f'App{appid}')}]: "
    ).strip() or profile.get("installdir", f"App{appid}")
    exe_raw = input(
        f"  {Colors.BOLD}Executable{Colors.RESET} (e.g. Bin/Game.exe) "
        f"[{profile.get('executable', 'Game.exe')}]: "
    ).strip() or profile.get("executable", "Game.exe")
    return {
        "name":       sanitize_filename(name_raw),
        "installdir": sanitize_path_segment(install_raw),
        "executable": sanitize_relative_path(exe_raw),
        "depot_id":   None,
    }


def _apply_steam_profile(info: SteamAppInfo, appid: int) -> SteamAppInfo:
    """Use verified local launch defaults for supported apps."""
    profile = _steam_profile(appid)
    if not profile:
        return info
    return {**info, "installdir": profile["installdir"], "executable": profile["executable"]}


def choose_steam_run_mode() -> str | None:
    """Return local/library mode, with local simulation as the default."""
    print("  1. Local simulation (default)")
    print("  2. Steam library + ACF")
    print("  3. Back")
    choice = input(f"{Colors.BOLD}Select [1-3] (default 1):{Colors.RESET} ").strip().lower()
    if choice in ("", "1", "l", "local", "local simulation"):
        return "local"
    if choice in ("2", "s", "steam", "library", "steam library + acf"):
        return "library"
    if choice in ("3", "b", "back"):
        return None
    print_color("[ERROR] Invalid selection.", Colors.RED)
    return None


def _resolve_steam_library() -> Path | None:
    """Prompt for a Steam root, using the registry root as the suggested value."""
    suggestion = get_steam_path()
    suggestion_valid = bool(suggestion and (suggestion / "steamapps").is_dir())
    if suggestion_valid:
        prompt = f"Steam library root [{suggestion}] (Enter to use it, or type another library): "
    else:
        if suggestion:
            print_color(f"[!] Registry Steam path has no steamapps folder: {suggestion}", Colors.YELLOW)
        prompt = "Steam library root (folder containing steamapps): "

    raw = input(prompt).strip()
    candidate = Path(raw).expanduser() if raw else suggestion if suggestion_valid else None
    if candidate is None:
        print_color("[!] No Steam library selected.", Colors.RED)
        return None
    try:
        candidate = candidate.resolve()
        if not candidate.is_dir() or not (candidate / "steamapps").is_dir():
            raise ValueError("Selected folder must contain an existing steamapps directory")
        return candidate
    except (OSError, ValueError) as error:
        print_color(f"[ERROR] Invalid Steam library: {error}", Colors.RED, bold=True)
        return None


def _read_existing_manifest(manifest: Path, appid: int) -> str | None:
    if not manifest.exists():
        return None
    try:
        return _read_appmanifest(manifest, appid)
    except (OSError, UnicodeError, ValueError) as error:
        print_color(f"[ERROR] Existing appmanifest is invalid: {error}", Colors.RED, bold=True)
        return ""


def _launch_scoped_executable(
    faker: GameFaker,
    target_path: Path,
    *,
    game_name: str,
    appid: int,
    manifest_path: Path | None = None,
    create_manifest: bool = False,
    installdir: str | None = None,
) -> None:
    """Prepare, optionally register, and launch one scoped simulation."""
    scope = faker.begin_scope()
    try:
        try:
            loading_animation(f"Preparing {target_path.name}", 0.8)
            prepared_path = faker.prepare_executable(
                target_path, game_name=game_name, steam_appid=appid
            )
        except Exception as error:
            print_color(f"[ERROR] Failed to prepare executable: {error}", Colors.RED, bold=True)
            return
        if prepared_path is None:
            print_color("[!] Executable preparation cancelled.", Colors.YELLOW)
            return
        print_color(f"[OK] Ready: {prepared_path}", Colors.GREEN, bold=True)

        if create_manifest:
            assert manifest_path is not None and installdir is not None
            created_manifest = generate_appmanifest(
                appid, game_name, installdir, manifest_path.parent.parent
            )
            if created_manifest is None:
                print_color("[ERROR] Could not create the appmanifest.", Colors.RED, bold=True)
                return
            # Register immediately so cleanup owns only this newly-created manifest.
            faker.register_created_file(created_manifest)

        if not faker.launch_executable(prepared_path):
            print_color("[ERROR] Failed to start the simulation.", Colors.RED, bold=True)
            return
        print_color("\n[OK] Steam Quest setup complete!", Colors.GREEN, bold=True)
        print_color("[!] Discord MUST be running for detection to work.", Colors.YELLOW)
        print_color("[*] Keep the process running until the quest is done.", Colors.CYAN)
        if manifest_path is not None:
            print_color(
                "[*] Keep this session running while restarting Steam and Discord if detection does not start.",
                Colors.GRAY,
            )
        input(f"\n{Colors.GRAY}Press Enter to stop simulation and clean up...{Colors.RESET}")
    finally:
        faker.cleanup_scope(scope)


def _library_minutes() -> int:
    """Return a valid plan value; elapsed-time sessions ignore this setting."""
    minutes = config.TIMER_MINUTES
    if type(minutes) is int and 1 <= minutes <= 10080:
        return minutes
    return 15


def _warn_about_elevation(denied_paths: tuple[str, ...]) -> None:
    print_color(
        "[!] Windows will request administrator permission to prepare these files "
        "and clean them up when the session ends. You can cancel at the Windows prompt.",
        Colors.YELLOW,
    )
    for path in denied_paths:
        print_color(f"    {path}", Colors.GRAY)


def _report_cleanup(stage: str, report) -> None:
    if report is None:
        return
    preserved = tuple(getattr(report, "preserved", ()))
    leftovers = tuple(getattr(report, "leftovers", ()))
    backups = tuple(getattr(report, "backups", ()))
    if preserved:
        print_color(f"[*] Preserved changed files after {stage}:", Colors.GRAY)
        for path in preserved:
            print_color(f"    {path}", Colors.GRAY)
    if not report.complete:
        print_color(f"[!] Filesystem cleanup was incomplete after {stage}.", Colors.YELLOW)
        for path in (*leftovers, *backups):
            print_color(f"    {path}", Colors.YELLOW)


def _run_library_plan(faker: GameFaker, plan: dict) -> None:
    """Prepare a library plan, launch it at normal integrity, then clean it up."""
    try:
        from _orbfarmer_elevation import prepare_session

        session = prepare_session(plan, on_elevation_required=_warn_about_elevation)
    except Exception as error:
        print_color(f"[ERROR] Failed to prepare Steam library files: {error}", Colors.RED, bold=True)
        _report_cleanup("failed preparation", getattr(error, "report", getattr(error, "rollback", None)))
        return
    if session is None:
        print_color("[!] Windows administrator request was cancelled. No game was launched.", Colors.YELLOW)
        return

    scope = None
    try:
        try:
            loading_animation(f"Preparing {session.target_path.name}", 0.8)
            scope = faker.begin_scope()
            if not faker.launch_executable(session.target_path):
                print_color("[ERROR] Failed to start the simulation.", Colors.RED, bold=True)
                return
            commit_report = session.commit()
            _report_cleanup("commit", commit_report)
        except Exception as error:
            print_color(f"[ERROR] Failed to launch or commit the Steam library session: {error}", Colors.RED, bold=True)
            _report_cleanup("failed launch or commit", getattr(error, "report", None))
            return

        print_color("\n[OK] Steam Quest setup complete!", Colors.GREEN, bold=True)
        print_color("[!] Discord MUST be running for detection to work.", Colors.YELLOW)
        print_color("[*] Keep the process running until the quest is done.", Colors.CYAN)
        print_color("[*] Keep this session running while restarting Steam and Discord if detection does not start.", Colors.GRAY)
        input(f"\n{Colors.GRAY}Press Enter to stop simulation and clean up...{Colors.RESET}")
    finally:
        if scope is not None:
            try:
                faker.stop_scope_processes(scope)
            except Exception as error:
                print_color(f"[!] Could not confirm that the game process stopped: {error}", Colors.YELLOW)
        try:
            _report_cleanup("session close", session.close())
        except Exception as error:
            print_color(f"[!] Filesystem cleanup failed after session close: {error}", Colors.YELLOW)
            _report_cleanup("failed session close", getattr(error, "report", None))


def run_steam_library_session(faker: GameFaker, appid: int, *, game_name: str | None = None) -> None:
    """Run an app in a selected Steam library and create a temporary ACF when needed."""
    if type(appid) is not int or appid <= 0:
        print_color("[ERROR] Steam appid must be a positive integer.", Colors.RED, bold=True)
        return
    steam_root = _resolve_steam_library()
    if steam_root is None:
        return

    try:
        steamapps = resolve_within(steam_root, "steamapps")
        manifest = resolve_within(steamapps, f"appmanifest_{appid}.acf")
        common = resolve_within(steamapps, "common")
    except ValueError as error:
        print_color(f"[ERROR] Invalid Steam library path: {error}", Colors.RED, bold=True)
        return

    existing_installdir = _read_existing_manifest(manifest, appid)
    if existing_installdir == "":
        return

    info = fetch_steam_app_info(appid) or _prompt_app_info_manually(appid)
    info = _apply_steam_profile(info, appid)
    effective_name = game_name or info["name"]
    try:
        installdir = existing_installdir or _validate_installdir(info["installdir"])
    except ValueError as error:
        print_color(f"[ERROR] Invalid Steam install folder: {error}", Colors.RED, bold=True)
        return
    executable = sanitize_relative_path(info["executable"])
    override = input(
        f"\n{Colors.BOLD}Override executable path?{Colors.RESET} [leave empty to keep]: "
    ).strip()
    if override:
        executable = sanitize_relative_path(override)

    try:
        target_path = resolve_within(common, installdir, executable.replace("/", os.sep))
        manifest = resolve_within(steamapps, manifest.name)
    except ValueError as error:
        print_color(f"[ERROR] Steam path escapes the selected library: {error}", Colors.RED, bold=True)
        return

    print(f"\n{Colors.BOLD}Steam library summary:{Colors.RESET}")
    print(f"  Appmanifest: {Colors.GRAY}{manifest}{Colors.RESET}")
    print(f"  Fake exe:    {Colors.GRAY}{target_path}{Colors.RESET}")
    if not ask_confirm():
        print_color("\n[!] Operation cancelled.", Colors.YELLOW)
        time.sleep(config.SLEEP_SHORT)
        return

    try:
        action = faker.choose_executable_action(target_path)
    except OSError as error:
        print_color(f"[ERROR] Could not inspect the selected executable: {error}", Colors.RED, bold=True)
        return
    if action == "cancel":
        print_color("\n[!] Operation cancelled.", Colors.YELLOW)
        return
    if action not in {"run", "create", "replace"}:
        print_color(f"[ERROR] Invalid executable action: {action}", Colors.RED, bold=True)
        return

    try:
        from .artwork import prepare_artwork
        from _orbfarmer_files import build_plan

        theme, assets = prepare_artwork(effective_name, appid)
        plan = build_plan(
            library_root=steam_root,
            appid=appid,
            name=effective_name,
            installdir=installdir,
            executable=executable,
            action=action,
            minutes=_library_minutes(),
            theme=theme,
            assets=assets,
        )
    except Exception as error:
        print_color(f"[ERROR] Could not prepare a Steam library plan: {error}", Colors.RED, bold=True)
        return

    _run_library_plan(faker, plan)


def _run_local_steam_session(faker: GameFaker, appid: int, game_name: str) -> None:
    info = fetch_steam_app_info(appid) or _prompt_app_info_manually(appid)
    info = _apply_steam_profile(info, appid)
    print(f"\n{Colors.BOLD}Detected info:{Colors.RESET}")
    print(f"  Name:        {Colors.CYAN}{info['name']}{Colors.RESET}")
    print(f"  Install dir: {Colors.CYAN}{info['installdir']}{Colors.RESET}")
    print(f"  Executable:  {Colors.CYAN}{info['executable']}{Colors.RESET}")

    override = input(f"\n{Colors.BOLD}Override executable path?{Colors.RESET} [leave empty to keep]: ").strip()
    if override:
        info["executable"] = sanitize_relative_path(override)
    installdir = sanitize_path_segment(info["installdir"])
    executable = sanitize_relative_path(info["executable"])
    try:
        target_path = resolve_within(
            faker.chosen_path,
            config.FAKE_EXE_DIR,
            f"{installdir}/{executable}".replace("/", os.sep),
        )
    except ValueError as error:
        print_color(f"[ERROR] Invalid executable path: {error}", Colors.RED, bold=True)
        return

    print(f"\n{Colors.BOLD}Summary:{Colors.RESET}")
    print(f"  Fake exe:    {Colors.GRAY}{target_path}{Colors.RESET}")
    if not ask_confirm():
        print_color("\n[!] Operation cancelled.", Colors.YELLOW)
        time.sleep(config.SLEEP_SHORT)
        return
    _launch_scoped_executable(faker, target_path, game_name=game_name, appid=appid)


def steam_quest_mode(faker: GameFaker) -> None:
    """Use Steam metadata to launch a simulation in the shared output folder."""
    print_boxed_title("STEAM QUEST MODE", width=55, color=Colors.CYAN)
    print_color("[*] This mode uses Steam metadata to create a simulation", Colors.CYAN)
    print_color("[*] Output goes to the configured simulations folder.", Colors.GRAY)
    print_color("[*] Search by name — demos and DLCs are separate, pick the right one!", Colors.YELLOW)
    print()

    query = input(f"\n{Colors.BOLD}Search game{Colors.RESET} (or 'back'): ").strip()
    if query.lower() in ("back", "b", ""):
        return

    game = _pick_steam_game(query)
    if not game:
        return
    try:
        appid = int(game["id"])
    except (TypeError, ValueError):
        print_color("[ERROR] Steam returned an invalid appid.", Colors.RED, bold=True)
        return
    if appid <= 0:
        print_color("[ERROR] Steam appid must be positive.", Colors.RED, bold=True)
        return

    print_color(f"\n[OK] Selected: {game['name']} (AppID: {appid})", Colors.GREEN, bold=True)
    mode = choose_steam_run_mode()
    if mode is None:
        return
    if mode == "library":
        run_steam_library_session(faker, appid, game_name=game["name"])
        return
    _run_local_steam_session(faker, appid, game["name"])
