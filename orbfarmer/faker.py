"""
faker.py – GameFaker class, manual_mode, and executable launching.

In frozen (.exe) mode:   copies orbfarmer.exe itself → GameName.exe (--timer-mode)
In source mode:           copies pythonw.exe → GameName.exe + _orbfarmer_timer.pyw
"""

import os
import sys
import hashlib
import shutil
import subprocess
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
    dirs: frozenset[Path]
    process_ids: frozenset[int]


class GameFaker:
    def __init__(self):
        self._frozen = _is_frozen()
        self._source_exe = _find_source_exe()
        self.chosen_path = config.CHOSEN_FOLDER
        self._created_files = []
        self._created_file_hashes: dict[Path, str] = {}
        self._created_dirs = []
        self._processes = []

    def begin_scope(self) -> FakerScope:
        """Capture the resources that existed before a scoped simulation."""
        return FakerScope(
            files=frozenset(self._created_files),
            dirs=frozenset(self._created_dirs),
            process_ids=frozenset(id(proc) for proc in self._processes),
        )

    def register_created_file(self, path: Path) -> None:
        """Register a file to be deleted on cleanup."""
        path = Path(path)
        self._created_files.append(path)
        self._created_file_hashes[path] = _sha256_file(path)

    def unregister_created_file(self, path: Path) -> None:
        """Forget an owned path after rolling back its file."""
        path = Path(path)
        self._created_files = [item for item in self._created_files if item != path]
        self._created_file_hashes.pop(path, None)

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
                code += f"\nTIMER_MINUTES = {config.TIMER_MINUTES}\nrun_timer(TIMER_MINUTES, theme={theme!r})\n"
                with open(timer_script, "x", encoding="utf-8") as script_file:
                    script_file.write(code)
                created_paths.append(timer_script)
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

        for created_path in created_paths:
            self.register_created_file(created_path)
        self._created_dirs.extend(created_dirs)

    def create_fake_game(self, exe_name: str, *, game_name: str | None = None, steam_appid=None) -> Path | None:
        """Create a game executable and its artwork under the configured output folder."""
        exe_name = sanitize_relative_path(exe_name)
        if not exe_name.lower().endswith('.exe'):
            exe_name += '.exe'
        target_path = resolve_within(self.chosen_path, config.FAKE_EXE_DIR, exe_name)
        try:
            loading_animation(f"Creating {exe_name.split('/')[-1]}", 0.8)
            self.copy_exe_to(target_path, game_name=game_name, steam_appid=steam_appid)
            print_color(f"[OK] Created: {target_path}", Colors.GREEN, bold=True)
            return target_path
        except Exception as e:
            print_color(f"[ERROR] Failed to create executable: {e}", Colors.RED, bold=True)
            print_color("[!] Check file permissions or disk space", Colors.YELLOW)
            return None

    def launch_executable(self, exe_path: Path) -> bool:
        """Launch the fake game process in background."""
        try:
            loading_animation("Launching process", 0.8)

            if self._frozen:
                args = [str(exe_path)]
                env = None
            else:
                timer_script = _timer_script_for(exe_path)
                args = [str(exe_path), str(timer_script)]
                env = os.environ.copy()
                base_prefix = Path(sys.base_prefix)
                env["PYTHONHOME"] = str(base_prefix)

                path_parts = [str(base_prefix), env.get("PATH", "")]
                env["PATH"] = os.pathsep.join(part for part in path_parts if part)

            if sys.platform == 'win32':
                DETACHED_PROCESS = 0x00000008
                proc = subprocess.Popen(
                    args,
                    env=env,
                    creationflags=DETACHED_PROCESS,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            else:
                proc = subprocess.Popen(
                    args,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
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
        processes = [proc for proc in self._processes if id(proc) not in scope.process_ids]
        files = [path for path in self._created_files if path not in scope.files]
        dirs = [path for path in self._created_dirs if path not in scope.dirs]

        print_color("\n[*] Stopping simulation and cleaning up...", Colors.CYAN)
        self._terminate_processes(processes)
        self._delete_files(files)
        self._remove_empty_dirs(dirs)

        process_ids = {id(proc) for proc in processes}
        dir_paths = set(dirs)
        self._processes = [proc for proc in self._processes if id(proc) not in process_ids]
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
