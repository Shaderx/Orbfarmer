"""
faker.py – GameFaker class, manual_mode, and executable launching.

In frozen (.exe) mode:   copies orbfarmer.exe itself → GameName.exe (--timer-mode)
In source mode:           copies pythonw.exe → GameName.exe + _orbfarmer_timer.pyw
"""

import os
import sys
import hashlib
import ast
import json
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, timer
from .artwork import prepare_artwork
from .path_utils import resolve_within, sanitize_relative_path
from .ui import (
    Colors, print_color, print_boxed_title,
    loading_animation, ask_confirm,
)

def _is_frozen() -> bool:
    return getattr(sys, 'frozen', False)


def _find_source_exe() -> Path:
    """Find the executable to copy for fake game processes."""
    if _is_frozen():
        return Path(sys.executable)  # copy ourselves

    # Source mode: prefer the real pythonw.exe from sys.base_prefix to avoid venv launcher stub issues
    base_dir = Path(sys.base_prefix)
    pythonw = base_dir / "pythonw.exe"
    if pythonw.exists():
        return pythonw
    python = base_dir / "python.exe"
    if python.exists():
        return python

    # Fallback to sys.executable's parent
    pythonw_fallback = Path(sys.executable).parent / "pythonw.exe"
    if pythonw_fallback.exists():
        return pythonw_fallback
    return Path(sys.executable)  # fallback to python.exe


def _timer_script_for(exe_path: Path) -> Path:
    """Return the private timer-script path paired with a fake executable."""
    return exe_path.with_name(f"_{exe_path.stem}_orbfarmer_timer.pyw")


_TIMER_HELPER_MARKER = "# ORBFARMER_TIMER_HELPER_V1"
_PYTHONHOME_MARKER = "# ORBFARMER_PYTHONHOME_JSON="


class _UnrecognizedReplacementFile(Exception):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(str(path))


def _timer_helper_details(script_path: Path) -> tuple[bool, Path | None]:
    """Return whether *script_path* matches an Orbfarmer timer helper."""
    try:
        contents = script_path.read_text(encoding="utf-8")
        timer_source = Path(timer.__file__).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False, None

    source_prefix = timer_source + "\n"
    if not contents.startswith(source_prefix):
        return False, None

    lines = contents[len(source_prefix):].splitlines()
    python_home = None
    if lines and lines[0] == _TIMER_HELPER_MARKER:
        if len(lines) < 4 or not lines[1].startswith(_PYTHONHOME_MARKER):
            return False, None
        try:
            python_home = Path(json.loads(lines[1][len(_PYTHONHOME_MARKER):]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return False, None
        lines = lines[2:]

    if len(lines) != 2:
        return False, None
    if not re.fullmatch(r"TIMER_MINUTES = \d+", lines[0]):
        return False, None
    call = re.fullmatch(r"run_timer\(TIMER_MINUTES, theme=(.*)\)", lines[1])
    if not call:
        return False, None
    try:
        theme = ast.literal_eval(call.group(1))
    except (SyntaxError, ValueError):
        return False, None
    if not isinstance(theme, dict):
        return False, None
    return True, python_home


def _has_frozen_helper_config(exe_path: Path) -> bool:
    """Identify a frozen Orbfarmer helper from its appended settings marker."""
    marker = b"__ORBFARMER_BAKED_CONFIG__"
    try:
        with open(exe_path, "rb") as exe_file:
            exe_file.seek(0, os.SEEK_END)
            exe_file.seek(max(0, exe_file.tell() - 65536))
            return marker in exe_file.read()
    except OSError:
        return False


def _copy_file_exclusive(source: Path, target: Path) -> None:
    """Copy *source* to a newly-created *target*, never overwriting a file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(target, flags)
    try:
        with open(source, "rb") as source_file, os.fdopen(descriptor, "wb") as target_file:
            descriptor = -1
            shutil.copyfileobj(source_file, target_file)
        shutil.copystat(source, target)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            target.unlink()
        except OSError:
            pass
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FakerScope:
    """Snapshot used to clean up only resources created by one operation."""

    files: frozenset[Path]
    file_generations: tuple[tuple[Path, int], ...]
    dirs: frozenset[Path]
    process_ids: frozenset[int]


class GameFaker:
    def __init__(self):
        self._frozen = _is_frozen()
        self._source_exe = _find_source_exe()
        self.chosen_path = config.CHOSEN_FOLDER
        self._created_files = []
        self._created_file_hashes: dict[Path, str] = {}
        self._created_file_generations: dict[Path, int] = {}
        self._file_generation = 0
        self._created_dirs = []
        self._processes = []

    def begin_scope(self) -> FakerScope:
        """Capture the resources that existed before a scoped simulation."""
        return FakerScope(
            files=frozenset(self._created_files),
            file_generations=tuple(
                (path, generation)
                for path in self._created_files
                if (generation := self._created_file_generations.get(path)) is not None
            ),
            dirs=frozenset(self._created_dirs),
            process_ids=frozenset(id(proc) for proc in self._processes),
        )

    def register_created_file(self, path: Path) -> None:
        """Register a file to be deleted on cleanup."""
        path = Path(path)
        self._register_file_hash(path, _sha256_file(path))

    def _register_file_hash(self, path: Path, content_hash: str) -> None:
        """Record ownership after the file hash has been read successfully."""
        if path not in self._created_files:
            self._created_files.append(path)
        self._created_file_hashes[path] = content_hash
        self._file_generation += 1
        self._created_file_generations[path] = self._file_generation

    def unregister_created_file(self, path: Path) -> None:
        """Forget an owned path after rolling back its file."""
        path = Path(path)
        self._created_files = [item for item in self._created_files if item != path]
        self._created_file_hashes.pop(path, None)
        self._created_file_generations.pop(path, None)

    def delete_owned_file(self, path: Path) -> bool:
        """Delete *path* only if it still matches the file this instance made."""
        path = Path(path)
        expected_hash = self._created_file_hashes.get(path)
        if expected_hash is None:
            return False
        if not path.exists():
            self.unregister_created_file(path)
            return True
        if _sha256_file(path) != expected_hash:
            self.unregister_created_file(path)
            return False
        path.unlink()
        self.unregister_created_file(path)
        return True

    @staticmethod
    def _create_parent_dirs(directory: Path) -> list[Path]:
        """Create and return only directories that did not already exist."""
        missing: list[Path] = []
        current = directory
        while not current.exists() and current != current.parent:
            missing.append(current)
            current = current.parent

        created: list[Path] = []
        for candidate in reversed(missing):
            try:
                candidate.mkdir()
                created.append(candidate)
            except FileExistsError:
                pass
        return created

    def copy_exe_to(self, target_path: Path, *, game_name: str | None = None, steam_appid=None) -> None:
        """Copy the faker executable to *target_path*.

        In source mode, also creates a ``_orbfarmer_timer.pyw`` next to
        the target so the renamed Python interpreter can run it.
        """
        target_path = target_path.resolve(strict=False)
        theme, assets = prepare_artwork(game_name or target_path.stem, steam_appid)
        self._copy_prepared_exe_to(target_path, theme, assets)

    def _copy_prepared_exe_to(self, target_path: Path, theme: dict, assets: dict[str, bytes]) -> None:
        """Create one simulation using already prepared theme and artwork data."""
        created_dirs = self._create_parent_dirs(target_path.parent)

        created_paths: list[Path] = []
        try:
            _copy_file_exclusive(self._source_exe, target_path)
            created_paths.append(target_path)

            for kind, data in assets.items():
                asset_path = target_path.with_name(f"_{target_path.stem}_{kind}.png")
                with open(asset_path, "xb") as asset_file:
                    created_paths.append(asset_path)
                    asset_file.write(data)
                theme[kind] = asset_path.name

            target_config = {
                "TIMER_MINUTES": config.TIMER_MINUTES,
                "TIMER_THEME": theme,
            }

            import json
            if self._frozen:
                json_data = json.dumps(target_config).encode("utf-8")
                marker = b"__ORBFARMER_BAKED_CONFIG__"
                with open(target_path, "ab") as target_file:
                    target_file.write(marker + json_data + marker)
            else:
                timer_script = _timer_script_for(target_path)
                # Share the standalone implementation with compiled timers.
                code = Path(timer.__file__).read_text(encoding="utf-8")
                python_home = json.dumps(str(Path(sys.base_prefix)))
                code += (
                    f"\n{_TIMER_HELPER_MARKER}\n"
                    f"{_PYTHONHOME_MARKER}{python_home}\n"
                    f"TIMER_MINUTES = {config.TIMER_MINUTES}\n"
                    f"run_timer(TIMER_MINUTES, theme={theme!r})\n"
                )
                with open(timer_script, "x", encoding="utf-8") as script_file:
                    created_paths.append(timer_script)
                    script_file.write(code)
            # Hash every file before changing ownership so a failed read can
            # roll back the whole copy without losing an earlier generation.
            file_hashes = [(path, _sha256_file(path)) for path in created_paths]
        except Exception:
            for created_path in reversed(created_paths):
                try:
                    created_path.unlink()
                except OSError:
                    pass
            for created_dir in reversed(created_dirs):
                try:
                    created_dir.rmdir()
                except OSError:
                    pass
            raise

        for created_path, content_hash in file_hashes:
            self._register_file_hash(created_path, content_hash)
        self._created_dirs.extend(created_dirs)

    @staticmethod
    def _ask_existing_executable_action(target_path: Path) -> str:
        print_color(f"\n[!] An executable already exists: {target_path}", Colors.YELLOW)
        print("  Run existing (default) — use the file as it is")
        print("  Replace — create a new simulation at this path")
        print("  Cancel — return without launching")
        while True:
            answer = input("Choose [Run existing/Replace/Cancel] (Run existing): ").strip().lower()
            if not answer or answer in ("r", "run", "run existing", "1"):
                return "run"
            if answer in ("replace", "p", "2"):
                return "replace"
            if answer in ("cancel", "c", "3"):
                return "cancel"
            print_color("[!] Choose Run existing, Replace, or Cancel.", Colors.YELLOW)

    def choose_executable_action(self, target_path: Path) -> str:
        """Return the selected action before any library plan or permission probe."""
        target_path = Path(target_path)
        try:
            info = target_path.lstat()
        except FileNotFoundError:
            return "create"
        except PermissionError:
            # The path may exist behind a denied directory. Ask before any
            # later plan or permission probe can make a write decision.
            return self._ask_existing_executable_action(target_path)
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return self._ask_existing_executable_action(target_path)
        return "create"

    @staticmethod
    def _replacement_companions(target_path: Path, assets: dict[str, bytes]) -> dict[Path, bytes | None]:
        companions: dict[Path, bytes | None] = {
            _timer_script_for(target_path): None,
        }
        companions.update({
            target_path.with_name(f"_{target_path.stem}_{kind}.png"): data
            for kind, data in assets.items()
        })
        return companions

    def _replace_existing_executable(self, target_path: Path, theme: dict, assets: dict[str, bytes]) -> None:
        """Replace a selected target and verified helper sidecars with rollback."""
        if not target_path.is_file() or target_path.is_symlink():
            raise FileExistsError(f"The existing path is not a regular executable file: {target_path}")

        replace_paths = [target_path]
        for companion, expected_data in self._replacement_companions(target_path, assets).items():
            if not companion.exists() and not companion.is_symlink():
                continue
            if companion.is_symlink() or not companion.is_file():
                raise _UnrecognizedReplacementFile(companion)
            if expected_data is None:
                is_helper, _ = _timer_helper_details(companion)
                if not is_helper:
                    raise _UnrecognizedReplacementFile(companion)
            elif companion.read_bytes() != expected_data:
                raise _UnrecognizedReplacementFile(companion)
            replace_paths.append(companion)

        backups: list[tuple[Path, Path]] = []
        try:
            for original_path in replace_paths:
                descriptor, backup_name = tempfile.mkstemp(
                    prefix=f".{original_path.name}.orbfarmer-",
                    suffix=".bak",
                    dir=original_path.parent,
                )
                os.close(descriptor)
                backup_path = Path(backup_name)
                try:
                    os.replace(original_path, backup_path)
                except Exception:
                    backup_path.unlink(missing_ok=True)
                    raise
                backups.append((backup_path, original_path))

            self._copy_prepared_exe_to(target_path, theme, assets)
        except Exception:
            for backup_path, original_path in reversed(backups):
                try:
                    if original_path.exists() or original_path.is_symlink():
                        print_color(
                            f"[!] Could not restore {original_path}; saved original at {backup_path}",
                            Colors.YELLOW,
                        )
                    else:
                        os.replace(backup_path, original_path)
                except OSError:
                    print_color(
                        f"[!] Could not restore {original_path}; saved original at {backup_path}",
                        Colors.YELLOW,
                    )
            raise
        else:
            for backup_path, _ in backups:
                try:
                    backup_path.unlink()
                except OSError:
                    print_color(f"[!] Could not remove replacement backup: {backup_path}", Colors.YELLOW)

    def prepare_executable(self, target_path: Path, *, game_name: str | None = None, steam_appid=None) -> Path | None:
        """Create, reuse, replace, or cancel a simulation at *target_path*."""
        target_path = Path(target_path).resolve(strict=False)

        action = self.choose_executable_action(target_path)
        if action == "run":
            print_color(f"[OK] Using existing executable: {target_path}", Colors.GREEN, bold=True)
            return target_path
        if action == "cancel":
            print_color("\n[!] Operation cancelled", Colors.YELLOW)
            return None

        if target_path.is_file():
            if action == "replace":
                theme, assets = prepare_artwork(game_name or target_path.stem, steam_appid)
                try:
                    self._replace_existing_executable(target_path, theme, assets)
                except _UnrecognizedReplacementFile as exc:
                    print_color(
                        f"[ERROR] Cannot replace the matching sidecar because it is not a verified Orbfarmer file: {exc}",
                        Colors.RED,
                        bold=True,
                    )
                    return None
                return target_path

        theme, assets = prepare_artwork(game_name or target_path.stem, steam_appid)
        try:
            self._copy_prepared_exe_to(target_path, theme, assets)
        except FileExistsError:
            if not target_path.is_file():
                raise
            action = self._ask_existing_executable_action(target_path)
            if action == "run":
                print_color(f"[OK] Using existing executable: {target_path}", Colors.GREEN, bold=True)
                return target_path
            if action == "cancel":
                print_color("\n[!] Operation cancelled", Colors.YELLOW)
                return None
            # Another process created the target after the first check.
            # Prepare the replacement with the same rollback rules as a normal collision.
            theme, assets = prepare_artwork(game_name or target_path.stem, steam_appid)
            try:
                self._replace_existing_executable(target_path, theme, assets)
            except _UnrecognizedReplacementFile as exc:
                print_color(
                    f"[ERROR] Cannot replace the matching sidecar because it is not a verified Orbfarmer file: {exc}",
                    Colors.RED,
                    bold=True,
                )
                return None
        return target_path

    def create_fake_game(self, exe_name: str, *, game_name: str | None = None, steam_appid=None) -> Path | None:
        """Create a game executable and its artwork under the configured output folder."""
        exe_name = sanitize_relative_path(exe_name)
        if not exe_name.lower().endswith('.exe'):
            exe_name += '.exe'
        target_path = resolve_within(self.chosen_path, config.FAKE_EXE_DIR, exe_name)
        try:
            loading_animation(f"Creating {exe_name.split('/')[-1]}", 0.8)
            previous_generation = self._created_file_generations.get(target_path)
            result = self.prepare_executable(target_path, game_name=game_name, steam_appid=steam_appid)
            if result and self._created_file_generations.get(result) != previous_generation:
                print_color(f"[OK] Created: {target_path}", Colors.GREEN, bold=True)
            return result
        except Exception as e:
            print_color(f"[ERROR] Failed to create executable: {e}", Colors.RED, bold=True)
            print_color("[!] Check file permissions or disk space", Colors.YELLOW)
            return None

    def launch_executable(self, exe_path: Path) -> bool:
        """Launch the fake game process in background."""
        try:
            loading_animation("Launching process", 0.8)
            exe_path = Path(exe_path).resolve(strict=False)
            timer_script = _timer_script_for(exe_path)
            is_frozen_helper = _has_frozen_helper_config(exe_path)
            is_source_helper, python_home = _timer_helper_details(timer_script)

            if is_source_helper and not is_frozen_helper:
                args = [str(exe_path), str(timer_script)]
                if python_home is None and not self._frozen:
                    python_home = Path(sys.base_prefix)
                if python_home is None and self._frozen:
                    pythonw = shutil.which("pythonw.exe") or shutil.which("python.exe")
                    if pythonw:
                        python_home = Path(pythonw).parent
                if python_home is None:
                    env = None
                else:
                    env = os.environ.copy()
                    env["PYTHONHOME"] = str(python_home)
                    path_parts = [str(python_home), env.get("PATH", "")]
                    env["PATH"] = os.pathsep.join(part for part in path_parts if part)
            else:
                args = [str(exe_path)]
                env = None

            if sys.platform == 'win32':
                DETACHED_PROCESS = 0x00000008
                proc = subprocess.Popen(
                    args,
                    env=env,
                    creationflags=DETACHED_PROCESS,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    cwd=str(exe_path.parent),
                )
            else:
                proc = subprocess.Popen(
                    args,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    cwd=str(exe_path.parent),
                )
            self._processes.append(proc)

            print_color("[OK] Process launched in background", Colors.GREEN, bold=True)
            print_color("[*] Discord should now detect the game (if Discord is running)", Colors.CYAN)
            print_color("[!] IMPORTANT: Discord MUST be running for the spoofing to work", Colors.YELLOW)
            print_color("[*] Wait a few seconds for Discord to scan processes", Colors.GRAY)
            print_color("[*] TIP: You can run this tool multiple times to emulate multiple games!", Colors.MAGENTA)
            return True
        except Exception as e:
            print_color(f"[!] Failed to auto-launch: {e}", Colors.YELLOW)
            print_color(f"[*] You can manually run: {exe_path}", Colors.CYAN)
            return False

    @staticmethod
    def _terminate_processes(processes: list) -> None:
        """Terminate launched processes, including their Windows child trees."""
        for proc in processes:
            try:
                pid = getattr(proc, "pid", None)
                if sys.platform == "win32" and isinstance(pid, int):
                    if proc.poll() is not None:
                        continue
                    result = subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=10,
                    )
                    if result.returncode != 0:
                        proc.terminate()
                else:
                    proc.terminate()
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

        if processes:
            time.sleep(1.0)
            for proc in processes:
                try:
                    proc.kill()
                except Exception:
                    pass

    def stop_scope_processes(self, scope: FakerScope) -> None:
        """Stop only processes launched after *scope* was captured."""
        processes = [proc for proc in self._processes if id(proc) not in scope.process_ids]
        self._terminate_processes(processes)
        process_ids = {id(proc) for proc in processes}
        self._processes = [proc for proc in self._processes if id(proc) not in process_ids]

    def _delete_files(self, files: list[Path]) -> None:
        """Delete owned files while preserving missing or replaced paths."""
        for file_path in files:
            deleted = False
            preserved = False
            for _attempt in range(5):
                try:
                    deleted = self.delete_owned_file(file_path)
                    preserved = not deleted
                    if preserved:
                        print_color(f"[!] Preserving replaced file: {file_path}", Colors.YELLOW)
                    break
                except Exception:
                    time.sleep(0.2)
            if not deleted and not preserved and file_path.exists():
                print_color(f"[!] Failed to delete: {file_path} (file is locked)", Colors.YELLOW)

    @staticmethod
    def _remove_empty_dirs(dirs: list[Path]) -> None:
        """Remove only directories recorded as created by this instance."""
        for dir_path in sorted(dirs, key=lambda path: len(path.parts), reverse=True):
            try:
                if dir_path.exists() and not any(dir_path.iterdir()):
                    dir_path.rmdir()
            except Exception:
                pass

    def cleanup_scope(self, scope: FakerScope) -> None:
        """Stop and remove only resources created after *scope* was captured."""
        scope_generations = dict(scope.file_generations)
        files = [
            path for path in self._created_files
            if path not in scope.files
            or self._created_file_generations.get(path) != scope_generations.get(path)
        ]
        dirs = [path for path in self._created_dirs if path not in scope.dirs]

        print_color("\n[*] Stopping simulation and cleaning up...", Colors.CYAN)
        self.stop_scope_processes(scope)
        self._delete_files(files)
        self._remove_empty_dirs(dirs)

        dir_paths = set(dirs)
        self._created_dirs = [path for path in self._created_dirs if path not in dir_paths]
        print_color("[OK] Simulation stopped and cleaned up!", Colors.GREEN)

    def cleanup(self) -> None:
        """Clean up all launched processes and created files if AUTO_DELETE is enabled."""
        if not config.AUTO_DELETE:
            return

        print_color("\n[*] AUTO_DELETE enabled. Cleaning up faked processes and files...", Colors.CYAN)

        self._terminate_processes(list(self._processes))
        self._delete_files(list(self._created_files))
        self._remove_empty_dirs(list(self._created_dirs))

        self._processes.clear()
        self._created_dirs.clear()

        print_color("[OK] Cleanup complete!", Colors.GREEN)


def manual_mode(faker: GameFaker) -> None:
    """Manual mode – user types an exact process name to fake."""
    print_boxed_title("MANUAL MODE", width=50, color=Colors.CYAN)
    print_color("[*] Enter the exact process name Discord expects", Colors.CYAN)
    print_color("[*] Examples:", Colors.GRAY)
    print_color("    • TslGame.exe (PUBG)", Colors.GRAY)
    print_color("    • League of Legends.exe (LoL)", Colors.GRAY)
    print_color("    • Overwatch.exe", Colors.GRAY)
    print_color("[*] Make sure the name matches exactly (case-sensitive on some systems)", Colors.GRAY)
    print()

    exe_name = input(f"{Colors.BOLD}Executable name{Colors.RESET} (or 'back'): ").strip()
    if not exe_name or exe_name.lower() in ('back', 'b'):
        return

    print(f"\n{Colors.BOLD}Summary:{Colors.RESET}")
    print(f"  Executable: {Colors.CYAN}{exe_name}{Colors.RESET}")
    print(f"  Path: {Colors.GRAY}{faker.chosen_path / config.FAKE_EXE_DIR / exe_name}{Colors.RESET}")

    if not ask_confirm():
        print_color("\n[!] Operation cancelled", Colors.YELLOW)
        time.sleep(config.SLEEP_SHORT)
        return

    result = faker.create_fake_game(exe_name)
    if result:
        print()
        faker.launch_executable(result)
        print_color("\n[OK] Setup complete!", Colors.GREEN, bold=True)
        print_color("[!] IMPORTANT: Discord MUST be running for the spoofing to work", Colors.YELLOW)

    input(f"\n{Colors.GRAY}Press Enter to continue...{Colors.RESET}")
