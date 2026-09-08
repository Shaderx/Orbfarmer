"""
orbfarmer – central configuration.

Reads user-editable values from the root-level ``settings.py``.
If a value is missing there, the default defined below is used.
Internal-only constants (API URLs, headers, timeouts) live here
and are NOT exposed in settings.py.
"""

import json
import sys
from pathlib import Path
import subprocess
from typing import TypeVar, cast

from . import _version as _build_version
from .path_utils import sanitize_relative_path

T = TypeVar("T")

def _get_default_json_content() -> str:
    content = {
        "CHOSEN_FOLDER": ".",
        "FAKE_EXE_DIR": "simulations",
        "AUTO_DELETE": False,
        "TIMER_MINUTES": 15
    }
    return json.dumps(content, indent=2)

def _is_faked_game() -> bool:
    """Check if the currently running executable/script is a faked game copy."""
    if getattr(sys, "frozen", False):
        name = str(sys.executable).replace("\\", "/").rsplit("/", 1)[-1].lower()
        return name not in ("orbfarmer", "orbfarmer.exe")
    else:
        name = Path(sys.argv[0]).name.lower()
        return name not in ("orbfarmer.py", "__main__.py") and "pytest" not in name

def _load_embedded_settings() -> dict | None:
    if not getattr(sys, "frozen", False):
        return None
    try:
        exe_path = Path(sys.executable)
        if not exe_path.exists():
            return None
        with open(exe_path, "rb") as f:
            # Seek to end and read up to 65536 bytes
            f.seek(0, 2)
            file_size = f.tell()
            read_size = min(file_size, 65536)
            f.seek(file_size - read_size)
            chunk = f.read(read_size)
        
        marker = b"__ORBFARMER_BAKED_CONFIG__"
        if marker in chunk:
            parts = chunk.split(marker)
            if len(parts) >= 3:
                json_bytes = parts[-2]
                return json.loads(json_bytes.decode("utf-8"))
    except Exception:
        pass
    return None

def _load_settings():
    # 1. Try loading embedded settings from the executable
    embedded = _load_embedded_settings()
    if embedded is not None:
        return embedded

    # 2. Determine the path of settings.json
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        json_path = exe_dir / "settings.json"
        
        # If settings.json doesn't exist, create it from default template
        # ONLY if we are the main application (not a faked game)
        if not _is_faked_game() and not json_path.exists():
            try:
                json_path.write_text(_get_default_json_content(), encoding="utf-8")
            except Exception:
                pass
    else:
        # Development mode
        dev_dir = Path(__file__).resolve().parents[1]
        json_path = dev_dir / "settings.json"

    # 3. Try loading settings.json
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Compiled applications accept data-only JSON configuration. Never execute
    # a Python sidecar placed next to the downloaded executable.
    if getattr(sys, "frozen", False):
        return None

    # Source checkouts retain settings.py compatibility.
    try:
        import settings as _user  # root-level settings.py
        return _user
    except ImportError:
        return None  # no user settings file – use all defaults

_user = _load_settings()


def _get(name: str, default: T) -> T:
    """Return a value from the user settings, falling back to *default*."""
    if _user is None:
        return default
    if isinstance(_user, dict):
        return cast(T, _user.get(name, default))
    return cast(T, getattr(_user, name, default))


def _git_version() -> str | None:
    """Return the current git tag as a version string when available."""
    commands = [
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
    ]
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                cwd=Path(__file__).resolve().parents[1],
            )
            tag = result.stdout.strip()
            if tag:
                return tag.lstrip("v")
        except Exception:
            continue
    return None


def _resolve_version() -> str:
    """Resolve the app version from build metadata or git tags."""
    built_version = getattr(_build_version, "VERSION", None)
    if built_version:
        return str(built_version)

    git_version = _git_version()
    if git_version:
        return git_version

    return "0.0.0"


# ── App identity ──────────────────────────────────────────────────────────────
VERSION   = _resolve_version()
DEVELOPER = "shaderx"
ORIGINAL_DEVELOPERS = "Strykey / Daniel Pires / Pannenkoekisus"
CONTRIBUTOR = "shaderx"

# ── GitHub repo ───────────────────────────────────────────────────────────────
GITHUB_REPO_OWNER = "Shaderx"
GITHUB_REPO_NAME  = "Orbfarmer"
REPO_URL          = f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"

# ── Network endpoints (internal – not in settings.py) ─────────────────────────
DISCORD_API_URL        = "https://discord.com/api/v9/applications/detectable"
GITHUB_BACKUP_URL      = (
    "https://gist.githubusercontent.com/Cynosphere/"
    "c1e77f77f0e565ddaac2822977961e76/raw/gameslist.json"
)
STEAMCMD_API_URL       = "https://api.steamcmd.net/v1/info"
STEAM_STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch"

# ── HTTP settings (internal) ─────────────────────────────────────────────────
REQUEST_TIMEOUT      = 10
REQUEST_TIMEOUT_LONG = 20

DISCORD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://discord.com/",
    "Origin":          "https://discord.com",
}

# ── UI / UX (user-editable via settings.py or settings.json) ──────────────────
SLEEP_SHORT        = 1.0
SLEEP_LONG         = 2.0
FAKE_EXE_DIR       = sanitize_relative_path(_get("FAKE_EXE_DIR", "simulations"))
MAX_SEARCH_RESULTS = 20
AUTO_DELETE        = _get("AUTO_DELETE",        False)
TIMER_MINUTES      = _get("TIMER_MINUTES",      15)
TIMER_THEME        = _get("TIMER_THEME",        {})

# Resolve CHOSEN_FOLDER as a Path object
app_folder = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
default_folder = "."
chosen_folder_val = _get("CHOSEN_FOLDER", default_folder)
if isinstance(chosen_folder_val, str):
    if chosen_folder_val.strip() == "Desktop":
        CHOSEN_FOLDER = Path.home() / "Desktop"
    else:
        CHOSEN_FOLDER = Path(chosen_folder_val.strip() or ".")
        if not CHOSEN_FOLDER.is_absolute():
            CHOSEN_FOLDER = app_folder / CHOSEN_FOLDER
else:
    CHOSEN_FOLDER = chosen_folder_val
