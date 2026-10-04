"""Standalone, bounded file transactions for Steam library sessions.

This module is intentionally outside the ``orbfarmer`` package. An elevated
helper can import it without loading application settings or other package
modules.
"""

from __future__ import annotations

import ast
import base64
import binascii
import contextlib
import ctypes
import errno
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import re
import secrets
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator


_MAX_ACF_BYTES = 4 * 1024 * 1024
_MAX_ACF_TOKENS = 100_000
_MAX_ACF_DEPTH = 64
_FC27_APPID = 4080220
_MAX_WIRE_BYTES = 12 * 1024 * 1024
_MAX_ASSET_BYTES = 4 * 1024 * 1024
_PLAN_KEYS = {
    "version", "transaction_id", "library_root", "appid", "name",
    "installdir", "executable", "action", "minutes", "theme", "assets",
    "manifest_action", "before",
}
_BEFORE_KEYS = {"exe", "timer", "hero", "icon", "manifest"}
_ASSET_KEYS = {"hero", "icon"}
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_TXID_RE = re.compile(r"[0-9a-f]{32}\Z")
_ACCENT_RE = re.compile(r"#[0-9a-fA-F]{6}\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TIMER_MARKER = "# ORBFARMER_TIMER_HELPER_V1"
_PYTHONHOME_MARKER = "# ORBFARMER_PYTHONHOME_JSON="
_BAKED_MARKER = b"__ORBFARMER_BAKED_CONFIG__"


class PlanError(ValueError):
    """The transaction plan is not canonical or exceeds its limits."""


class ConcurrentModificationError(RuntimeError):
    """A destination changed after the plan snapshot was made."""


class PreparationError(RuntimeError):
    """Preparation failed after rollback was attempted."""

    def __init__(
        self,
        message: str,
        *,
        permission_denied: bool = False,
        denied_paths: tuple[str, ...] = (),
        rollback: CleanupReport | None = None,
    ) -> None:
        super().__init__(message)
        self.permission_denied = permission_denied
        self.denied_paths = tuple(denied_paths)
        self.rollback = rollback or CleanupReport()


@dataclass(frozen=True)
class ProbeResult:
    needs_elevation: bool
    denied_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupReport:
    removed: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    leftovers: tuple[str, ...] = ()
    backups: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.leftovers and not self.backups

    def to_dict(self) -> dict[str, Any]:
        return {
            "removed": list(self.removed),
            "preserved": list(self.preserved),
            "leftovers": list(self.leftovers),
            "backups": list(self.backups),
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CleanupReport:
        if not isinstance(value, dict) or set(value) != {
            "removed", "preserved", "leftovers", "backups", "complete",
        }:
            raise ValueError("cleanup report has an invalid shape")
        for key in ("removed", "preserved", "leftovers", "backups"):
            items = value[key]
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise ValueError("cleanup report paths must be string lists")
        report = cls(*(tuple(value[key]) for key in ("removed", "preserved", "leftovers", "backups")))
        if type(value["complete"]) is not bool or value["complete"] != report.complete:
            raise ValueError("cleanup report completeness flag is inconsistent")
        return report


@dataclass(frozen=True)
class _Snapshot:
    size: int
    sha256: str
    identity: str

    def to_plan_value(self) -> dict[str, Any]:
        return {"size": self.size, "sha256": self.sha256, "identity": self.identity}


@dataclass
class _Runtime:
    executable: bytes
    timer_source: bytes
    frozen: bool
    python_home: str


_runtime_override: _Runtime | None = None
_runtime_lock = threading.RLock()


@contextlib.contextmanager
def _runtime_for_tests(
    *, executable: bytes, timer_source: bytes, frozen: bool = False,
    python_home: str | None = None,
) -> Iterator[None]:
    """Inject internal runtime bytes for isolated tests; never a wire field."""
    global _runtime_override
    runtime = _Runtime(
        executable=bytes(executable),
        timer_source=bytes(timer_source),
        frozen=bool(frozen),
        python_home=python_home or sys.base_prefix,
    )
    with _runtime_lock:
        previous = _runtime_override
        _runtime_override = runtime
    try:
        yield
    finally:
        with _runtime_lock:
            _runtime_override = previous


def _runtime() -> _Runtime:
    with _runtime_lock:
        if _runtime_override is not None:
            return _Runtime(**vars(_runtime_override))

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        executable_path = Path(sys.executable)
    else:
        base = Path(sys.base_prefix)
        candidates = [base / "pythonw.exe", base / "python.exe"]
        candidates.extend((Path(sys.executable).parent / "pythonw.exe", Path(sys.executable)))
        executable_path = next((path for path in candidates if path.is_file()), Path(sys.executable))

    if frozen:
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        timer_path = bundle_root / "orbfarmer" / "timer.py"
    else:
        timer_path = Path(__file__).parent / "orbfarmer" / "timer.py"
    try:
        executable_bytes = _read_trusted_file(executable_path)
        timer_bytes = _read_trusted_file(timer_path)
    except OSError as error:
        raise RuntimeError(f"trusted Orbfarmer runtime is unavailable: {error}") from error
    return _Runtime(executable_bytes, timer_bytes, frozen, str(Path(sys.base_prefix)))


def _read_trusted_file(path: Path) -> bytes:
    """Read an executable or timer source selected from this process runtime."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "trusted runtime source is not a regular file", str(path))
        with os.fdopen(fd, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(fd)


def _escape_vdf(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("ACF values cannot contain control characters")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 160:
        raise ValueError("name must contain 1 to 160 characters")
    _escape_vdf(name)
    return name


def _validate_installdir(installdir: str) -> str:
    if not isinstance(installdir, str) or not installdir or installdir != installdir.strip():
        raise ValueError("ACF installdir must be a non-empty folder name")
    if installdir in {".", ".."} or any(char in installdir for char in "/\\:"):
        raise ValueError("ACF installdir must be a single relative folder name")
    if any(char in installdir for char in '<>"|?*'):
        raise ValueError("ACF installdir contains a Windows-invalid character")
    if PurePosixPath(installdir).is_absolute() or PureWindowsPath(installdir).is_absolute():
        raise ValueError("ACF installdir cannot be absolute")
    if PureWindowsPath(installdir).drive or any(ord(char) < 32 or ord(char) == 127 for char in installdir):
        raise ValueError("ACF installdir contains an invalid path value")
    if installdir.endswith((".", " ")) or len(installdir) > 255:
        raise ValueError("ACF installdir cannot end with a dot or space or exceed 255 characters")
    _validate_windows_component(installdir)
    return installdir


def _validate_windows_component(component: str) -> None:
    if not component or component in {".", ".."}:
        raise ValueError("relative path contains an empty or traversal component")
    if len(component) > 255:
        raise ValueError("relative path component is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in component):
        raise ValueError("relative path contains a control character")
    if any(char in '<>:"|?*\\/' for char in component):
        raise ValueError("relative path contains a Windows-invalid character")
    if component.endswith((".", " ")):
        raise ValueError("relative path component cannot end with a dot or space")
    device = component.split(".", 1)[0].casefold()
    reserved = {"con", "prn", "aux", "nul", "conin$", "conout$", "clock$"}
    reserved.update(f"com{digit}" for digit in "123456789¹²³")
    reserved.update(f"lpt{digit}" for digit in "123456789¹²³")
    if device in reserved:
        raise ValueError("relative path contains a reserved Windows device name")


def _validate_executable(executable: str) -> str:
    if not isinstance(executable, str) or not executable or "\\" in executable:
        raise ValueError("executable must be a strict relative path using forward slashes")
    if executable.startswith("/") or executable.startswith("//") or ":" in executable:
        raise ValueError("executable cannot be absolute, a device path, or an alternate stream")
    pieces = executable.split("/")
    for piece in pieces:
        _validate_windows_component(piece)
    if not pieces[-1].lower().endswith(".exe"):
        raise ValueError("executable must end in .exe")
    return executable


def _validate_theme(theme: Any, appid: int) -> dict[str, Any]:
    if not isinstance(theme, dict) or set(theme) - {"name", "accent", "steam_appid"}:
        raise ValueError("theme has an invalid shape")
    if "name" not in theme or "accent" not in theme:
        raise ValueError("theme requires name and accent")
    name = _validate_name(theme["name"])
    accent = theme["accent"]
    if not isinstance(accent, str) or not _ACCENT_RE.fullmatch(accent):
        raise ValueError("theme accent must be a six-digit hex color")
    result = {"name": name, "accent": accent}
    if "steam_appid" in theme:
        if type(theme["steam_appid"]) is not int or theme["steam_appid"] != appid:
            raise ValueError("theme steam_appid must match appid")
        result["steam_appid"] = appid
    return result


def _check_png(data: bytes, kind: str) -> None:
    if not isinstance(data, bytes) or len(data) > _MAX_ASSET_BYTES:
        raise ValueError(f"{kind} asset must be PNG bytes no larger than 4 MiB")
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError(f"{kind} asset is not a PNG")


def _validate_assets(assets: Any, *, wire: bool) -> dict[str, str] | dict[str, bytes]:
    if not isinstance(assets, dict) or set(assets) - _ASSET_KEYS:
        raise ValueError("assets accepts only hero and icon")
    result: dict[str, str] | dict[str, bytes] = {}
    for kind, value in assets.items():
        if wire:
            if not isinstance(value, str) or len(value) > ((_MAX_ASSET_BYTES + 2) // 3) * 4:
                raise ValueError(f"{kind} asset is not bounded base64")
            try:
                decoded = base64.b64decode(value.encode("ascii"), validate=True)
            except (UnicodeEncodeError, binascii.Error, ValueError) as error:
                raise ValueError(f"{kind} asset is not valid base64") from error
            if base64.b64encode(decoded).decode("ascii") != value:
                raise ValueError(f"{kind} asset base64 is not canonical")
            _check_png(decoded, kind)
            result[kind] = value
        else:
            _check_png(value, kind)
            result[kind] = value
    return result


def _canonical_root(library_root: Path | str, *, require_exists: bool) -> Path:
    root = Path(library_root)
    if not root.is_absolute():
        raise ValueError("library_root must be absolute")
    raw = os.fspath(root)
    if os.name == "nt":
        if raw.startswith(("\\\\", "//")) or raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
            raise ValueError("library_root cannot use UNC or device syntax")
        win = PureWindowsPath(raw)
        if not win.drive or win.root != "\\":
            raise ValueError("library_root must use an absolute local drive path")
    else:
        if raw.startswith("//"):
            raise ValueError("library_root cannot use UNC syntax")
    # Inspect the submitted spelling before resolving it. Resolving first would
    # hide a junction or symlink at the library root itself.
    _reject_reparse_components(root)
    if require_exists and not root.is_dir():
        raise FileNotFoundError(f"Steam library root is not a directory: {root}")
    resolved = root.resolve(strict=require_exists)
    if require_exists:
        _reject_reparse_components(resolved)
        for required in (resolved / "steamapps", resolved / "steamapps" / "common"):
            if not required.is_dir():
                raise FileNotFoundError(f"Steam library is missing {required.name}: {required}")
            _reject_reparse_components(required)
    return resolved


def _is_reparse(info: os.stat_result) -> bool:
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attrs & reparse)


def _reject_reparse_components(path: Path) -> None:
    absolute = Path(path)
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for component in parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if _is_reparse(info):
            raise ValueError(f"reparse or symbolic-link component is not allowed: {current}")


def _paths_for(plan: dict[str, Any]) -> dict[str, Path]:
    root = Path(plan["library_root"])
    common = root / "steamapps" / "common" / plan["installdir"]
    target = common.joinpath(*plan["executable"].split("/"))
    return {
        "exe": target,
        "timer": target.with_name(f"_{target.stem}_orbfarmer_timer.pyw"),
        "hero": target.with_name(f"_{target.stem}_hero.png"),
        "icon": target.with_name(f"_{target.stem}_icon.png"),
        "manifest": root / "steamapps" / f"appmanifest_{plan['appid']}.acf",
    }


def parse_manifest(contents: bytes, appid: int) -> str:
    """Validate bounded Valve KeyValues data and return its install folder."""
    if type(appid) is not int or appid <= 0:
        raise ValueError("Steam appid must be a positive integer")
    if not isinstance(contents, bytes) or len(contents) > _MAX_ACF_BYTES:
        raise ValueError("ACF manifest is too large or is not bytes")
    text = contents.decode("utf-8-sig")
    root = _parse_vdf_pairs(_tokenize_vdf(text))
    app_states = [value for key, value in root if key.casefold() == "appstate"]
    if len(root) != 1 or len(app_states) != 1 or not isinstance(app_states[0], list):
        raise ValueError("ACF manifest must have one root AppState block")
    state = app_states[0]
    if _unique_string_value(state, "appid") != str(appid):
        raise ValueError("ACF manifest appid does not match the selected Steam game")
    return _validate_installdir(_unique_string_value(state, "installdir"))


def _tokenize_vdf(text: str) -> list[str]:
    if len(text.encode("utf-8")) > _MAX_ACF_BYTES:
        raise ValueError("ACF manifest is too large")
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline == -1 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ValueError("ACF manifest has an unclosed comment")
            index = end + 2
            continue
        if char in "{}":
            tokens.append(char)
            index += 1
        elif char == '"':
            index += 1
            value: list[str] = []
            while index < len(text) and text[index] != '"':
                if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in ("\\", '"'):
                    value.append(text[index + 1])
                    index += 2
                else:
                    value.append(text[index])
                    index += 1
            if index >= len(text):
                raise ValueError("ACF manifest has an unclosed quoted value")
            tokens.append("".join(value))
            index += 1
        else:
            end = index
            while end < len(text) and not text[end].isspace() and text[end] not in '{}"\\':
                if text.startswith("//", end) or text.startswith("/*", end):
                    break
                end += 1
            if end == index:
                raise ValueError("ACF manifest contains an invalid token")
            tokens.append(text[index:end])
            index = end
        if len(tokens) > _MAX_ACF_TOKENS:
            raise ValueError("ACF manifest has too many values")
    return tokens


def _parse_vdf_pairs(tokens: list[str]) -> list[tuple[str, str | list]]:
    position = 0

    def parse_block(depth: int) -> list[tuple[str, str | list]]:
        nonlocal position
        if depth > _MAX_ACF_DEPTH:
            raise ValueError("ACF manifest is nested too deeply")
        pairs: list[tuple[str, str | list]] = []
        while position < len(tokens) and tokens[position] != "}":
            key = tokens[position]
            if key == "{":
                raise ValueError("ACF manifest has an unexpected block")
            position += 1
            if position >= len(tokens):
                raise ValueError("ACF manifest is missing a value")
            value = tokens[position]
            if value == "{":
                position += 1
                pairs.append((key, parse_block(depth + 1)))
                if position >= len(tokens) or tokens[position] != "}":
                    raise ValueError("ACF manifest has an unclosed block")
                position += 1
            elif value == "}":
                raise ValueError("ACF manifest is missing a value")
            else:
                pairs.append((key, value))
                position += 1
        return pairs

    result = parse_block(0)
    if position != len(tokens):
        raise ValueError("ACF manifest has an unexpected closing block")
    return result


def _unique_string_value(pairs: list[tuple[str, str | list]], key: str) -> str:
    values = [value for name, value in pairs if name.casefold() == key.casefold()]
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError(f"ACF manifest must contain one {key} value")
    return values[0]


def render_manifest(appid: int, name: str, installdir: str) -> bytes:
    if type(appid) is not int or appid <= 0:
        raise ValueError("Steam appid must be a positive integer")
    _validate_name(name)
    _validate_installdir(installdir)
    if appid == _FC27_APPID:
        name = "EA SPORTS FC™ 27"
        state_flags = ""
    else:
        state_flags = '\t"StateFlags"\t\t"4"\n'
    content = (
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{_escape_vdf(str(appid))}"\n'
        f'\t"name"\t\t"{_escape_vdf(name)}"\n'
        f"{state_flags}"
        f'\t"installdir"\t\t"{_escape_vdf(installdir)}"\n'
        '}\n'
    )
    return content.encode("utf-8")


def _validate_snapshot(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"size", "sha256", "identity"}:
        raise PlanError(f"before.{label} has an invalid shape")
    if type(value["size"]) is not int or value["size"] < 0:
        raise PlanError(f"before.{label}.size must be a non-negative integer")
    if not isinstance(value["sha256"], str) or not _HEX64_RE.fullmatch(value["sha256"]):
        raise PlanError(f"before.{label}.sha256 must be lowercase SHA-256 hex")
    identity = value["identity"]
    if not isinstance(identity, str) or not identity or len(identity) > 512:
        raise PlanError(f"before.{label}.identity must be a bounded opaque string")
    if any(ord(char) < 32 or ord(char) == 127 for char in identity):
        raise PlanError(f"before.{label}.identity contains a control character")


def _validate_plan(plan: Any, *, check_root: bool = True) -> dict[str, Any]:
    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS:
        raise PlanError("plan has missing or unknown keys")
    if type(plan["version"]) is not int or plan["version"] != 1:
        raise PlanError("plan version must be 1")
    if not isinstance(plan["transaction_id"], str) or not _TXID_RE.fullmatch(plan["transaction_id"]):
        raise PlanError("transaction_id must be 32 lowercase hexadecimal characters")
    root_text = plan["library_root"]
    if not isinstance(root_text, str) or not root_text:
        raise PlanError("library_root must be an absolute canonical path")
    root = Path(root_text)
    if not root.is_absolute():
        raise PlanError("library_root must be absolute")
    if os.name == "nt":
        if root_text.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
            raise PlanError("library_root cannot use UNC or device syntax")
        win_root = PureWindowsPath(root_text)
        if not win_root.drive or win_root.root != "\\":
            raise PlanError("library_root must use an absolute local drive path")
    else:
        if root_text.startswith("//"):
            raise PlanError("library_root cannot use UNC syntax")
    if check_root:
        try:
            canonical = _canonical_root(root_text, require_exists=True)
        except (OSError, ValueError) as error:
            raise PlanError(f"library_root is not a safe Steam library: {error}") from error
        equal = ntpath.normcase(ntpath.normpath(root_text)) == ntpath.normcase(ntpath.normpath(str(canonical))) if os.name == "nt" else os.path.normpath(root_text) == os.path.normpath(str(canonical))
        if not equal:
            raise PlanError("library_root must be canonical")
    if type(plan["appid"]) is not int or plan["appid"] <= 0:
        raise PlanError("appid must be a positive integer")
    try:
        _validate_name(plan["name"])
        _validate_installdir(plan["installdir"])
        _validate_executable(plan["executable"])
    except (TypeError, ValueError) as error:
        raise PlanError(str(error)) from error
    if not isinstance(plan["action"], str) or plan["action"] not in {"run", "create", "replace"}:
        raise PlanError("action must be run, create, or replace")
    if type(plan["minutes"]) is not int or not 1 <= plan["minutes"] <= 10080:
        raise PlanError("minutes must be an integer from 1 through 10080")
    try:
        theme = _validate_theme(plan["theme"], plan["appid"])
        assets = _validate_assets(plan["assets"], wire=True)
    except (TypeError, ValueError) as error:
        raise PlanError(str(error)) from error
    if theme != plan["theme"] or assets != plan["assets"]:
        raise PlanError("theme or assets are not in canonical form")
    if not isinstance(plan["manifest_action"], str) or plan["manifest_action"] not in {"create", "preserve"}:
        raise PlanError("manifest_action must be create or preserve")
    before = plan["before"]
    if not isinstance(before, dict) or set(before) != _BEFORE_KEYS:
        raise PlanError("before must contain exactly exe, timer, hero, icon, and manifest")
    for key in _BEFORE_KEYS:
        _validate_snapshot(before[key], key)
    if plan["action"] == "run" and before["exe"] is None:
        raise PlanError("run requires an existing executable snapshot")
    if plan["action"] == "create" and before["exe"] is not None:
        raise PlanError("create requires an absent executable")
    if plan["action"] == "replace" and before["exe"] is None:
        raise PlanError("replace requires an existing executable")
    if plan["manifest_action"] == "create" and before["manifest"] is not None:
        raise PlanError("manifest creation requires an absent appmanifest")
    if plan["manifest_action"] == "preserve" and before["manifest"] is None:
        raise PlanError("manifest preservation requires an existing appmanifest")
    return plan


def _identity_hash(info: os.stat_result, data: bytes | None = None) -> _Snapshot:
    if data is None:
        raise AssertionError("file snapshot requires bytes or a streaming adapter")
    identity = f"{info.st_dev:x}:{info.st_ino:x}"
    return _Snapshot(len(data), hashlib.sha256(data).hexdigest(), identity)


def build_plan(
    *, library_root: Path, appid: int, name: str, installdir: str,
    executable: str, action: str, minutes: int, theme: dict,
    assets: dict[str, bytes],
) -> dict[str, Any]:
    """Build a canonical plan and snapshot every derived output path."""
    root = _canonical_root(library_root, require_exists=True)
    if type(appid) is not int or appid <= 0:
        raise ValueError("Steam appid must be a positive integer")
    name = _validate_name(name)
    installdir = _validate_installdir(installdir)
    executable = _validate_executable(executable)
    if action not in {"run", "create", "replace"}:
        raise ValueError("action must be run, create, or replace")
    if type(minutes) is not int or not 1 <= minutes <= 10080:
        raise ValueError("minutes must be an integer from 1 through 10080")
    normalized_theme = _validate_theme(theme, appid)
    normalized_assets = _validate_assets(assets, wire=False)
    wire_assets = {
        kind: base64.b64encode(value).decode("ascii")
        for kind, value in normalized_assets.items()
    }
    plan: dict[str, Any] = {
        "version": 1,
        "transaction_id": secrets.token_hex(16),
        "library_root": str(root),
        "appid": appid,
        "name": name,
        "installdir": installdir,
        "executable": executable,
        "action": action,
        "minutes": minutes,
        "theme": normalized_theme,
        "assets": wire_assets,
        "manifest_action": "create",
        "before": {key: None for key in _BEFORE_KEYS},
    }
    paths = _paths_for(plan)
    fs = _get_filesystem()
    snapshots: dict[str, _Snapshot | None] = {}
    manifest_bytes: bytes | None = None
    for key, path in paths.items():
        snapshot, contents = fs.inspect(
            path, read_bytes=(key == "manifest"),
            max_bytes=_MAX_ACF_BYTES if key == "manifest" else None,
        )
        snapshots[key] = snapshot
        if key == "manifest" and snapshot is not None:
            manifest_bytes = contents
    plan["before"] = {
        key: snapshot.to_plan_value() if snapshot is not None else None
        for key, snapshot in snapshots.items()
    }
    if snapshots["manifest"] is not None:
        plan["manifest_action"] = "preserve"
        assert manifest_bytes is not None
        manifest_installdir = parse_manifest(manifest_bytes, appid)
        if manifest_installdir != installdir:
            raise ValueError("selected installdir does not match the validated existing appmanifest")

    _validate_plan(plan)
    runtime = _runtime()
    _validate_build_selection(plan, paths, snapshots, runtime)
    encode_plan(plan)
    return plan


def _validate_build_selection(
    plan: dict[str, Any], paths: dict[str, Path],
    snapshots: dict[str, _Snapshot | None], runtime: _Runtime,
) -> None:
    action = plan["action"]
    if action == "create":
        if snapshots["exe"] is not None:
            raise FileExistsError(f"create target already exists: {paths['exe']}")
        for kind in _selected_creation_sidecars(plan, runtime):
            if snapshots[kind] is not None:
                raise FileExistsError(f"create sidecar already exists: {paths[kind]}")
    elif action == "replace":
        if snapshots["exe"] is None:
            raise FileNotFoundError(f"replace target does not exist: {paths['exe']}")
        _verify_replacement_sidecars(plan, paths, snapshots, runtime, fs=_get_filesystem())
    if plan["manifest_action"] == "create" and snapshots["manifest"] is not None:
        raise FileExistsError(f"appmanifest already exists: {paths['manifest']}")


def _selected_creation_sidecars(plan: dict[str, Any], runtime: _Runtime) -> tuple[str, ...]:
    selected = ["timer"] if not runtime.frozen else []
    selected.extend(kind for kind in ("hero", "icon") if kind in plan["assets"])
    return tuple(selected)


def _plan_json(plan: dict[str, Any]) -> bytes:
    return json.dumps(
        plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def encode_plan(plan: dict) -> bytes:
    _validate_plan(plan)
    payload = _plan_json(plan)
    if len(payload) > _MAX_WIRE_BYTES:
        raise PlanError("encoded plan exceeds the 12 MiB wire limit")
    return payload


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise PlanError(f"invalid JSON constant: {value}")


def decode_plan(payload: bytes) -> dict:
    if not isinstance(payload, bytes) or len(payload) > _MAX_WIRE_BYTES:
        raise PlanError("plan payload must be bytes no larger than 12 MiB")
    try:
        plan = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlanError("plan is not valid UTF-8 JSON") from error
    _validate_plan(plan)
    if _plan_json(plan) != payload:
        raise PlanError("plan JSON must use canonical compact encoding")
    return plan


def has_writes(plan: dict) -> bool:
    _validate_plan(plan)
    return plan["action"] in {"create", "replace"} or plan["manifest_action"] == "create"


def _same_snapshot(actual: _Snapshot | None, expected: _Snapshot | dict[str, Any] | None) -> bool:
    if actual is None or expected is None:
        return actual is expected
    if isinstance(expected, dict):
        return (
            actual.size == expected.get("size")
            and actual.sha256 == expected.get("sha256")
            and actual.identity == expected.get("identity")
        )
    return actual == expected


@dataclass
class _Backup:
    original_path: Path
    path: Path
    snapshot: _Snapshot
    handle: Any = None


@dataclass
class _OwnedFile:
    path: Path
    snapshot: _Snapshot


@dataclass(frozen=True)
class _OwnedDirectory:
    path: Path
    identity: str


class _PortableFilesystem:
    """Descriptor-relative adapter for non-Windows tests and development."""

    def _relative(self, path: Path, root: Path) -> tuple[str, ...]:
        try:
            relative = Path(path).relative_to(Path(root))
        except ValueError as error:
            raise ValueError(f"destination escapes library root: {path}") from error
        parts = relative.parts
        for part in parts:
            _validate_windows_component(part)
        return parts

    @staticmethod
    def _identity(info: os.stat_result) -> str:
        return f"{info.st_dev:x}:{info.st_ino:x}"

    def _open_directory(self, path: Path, root: Path) -> tuple[int, list[int]]:
        parts = self._relative(path, root)
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        fds = [root_fd]
        try:
            root_info = os.fstat(root_fd)
            if not stat.S_ISDIR(root_info.st_mode):
                raise NotADirectoryError(root)
            for part in parts:
                fd = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fds[-1],
                )
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
                    os.close(fd)
                    raise ValueError(f"reparse or non-directory component: {path / part}")
                fds.append(fd)
            return fds[-1], fds
        except Exception:
            for fd in reversed(fds):
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise

    @staticmethod
    def _close_fds(fds: list[int]) -> None:
        for fd in reversed(fds):
            with contextlib.suppress(OSError):
                os.close(fd)

    def _open_file(self, path: Path, root: Path, *, delete_access: bool = False) -> tuple[int, int, list[int]] | None:
        path = Path(path)
        try:
            parent_fd, fds = self._open_directory(path.parent, root)
        except FileNotFoundError:
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if delete_access:
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            self._close_fds(fds)
            return None
        except Exception:
            self._close_fds(fds)
            raise
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
            os.close(fd)
            self._close_fds(fds)
            raise ValueError(f"target is not a regular file: {path}")
        return fd, parent_fd, fds

    @staticmethod
    def _hash_fd(fd: int, *, return_bytes: bool = False, max_bytes: int | None = None) -> tuple[str, int, bytes | None]:
        info = os.fstat(fd)
        size = info.st_size
        if max_bytes is not None and size > max_bytes:
            raise ValueError("file exceeds the allowed size")
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if return_bytes else None
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            total += len(block)
            digest.update(block)
            if chunks is not None:
                chunks.append(block)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("file exceeds the allowed size")
        os.lseek(fd, 0, os.SEEK_SET)
        return digest.hexdigest(), total, b"".join(chunks) if chunks is not None else None

    def inspect(
        self, path: Path, *, read_bytes: bool = False, max_bytes: int | None = None,
    ) -> tuple[_Snapshot | None, bytes | None]:
        path = Path(path)
        # Root is the Steam library. It is derived from the destination's parent
        # by the caller through _inspection_root(), so inspect remains internal.
        root = _inspection_root(path)
        opened = self._open_file(path, root)
        if opened is None:
            return None, None
        fd, _, fds = opened
        try:
            info = os.fstat(fd)
            digest, size, contents = self._hash_fd(fd, return_bytes=read_bytes, max_bytes=max_bytes)
            return _Snapshot(size, digest, self._identity(info)), contents
        finally:
            os.close(fd)
            self._close_fds(fds)

    def ensure_parent_dirs(self, parent: Path, root: Path) -> list[_OwnedDirectory]:
        parent = Path(parent)
        parts = self._relative(parent, root)
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        fds = [root_fd]
        created: list[_OwnedDirectory] = []
        current = Path(root)
        try:
            for part in parts:
                current = current / part
                try:
                    fd = os.open(
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=fds[-1],
                    )
                except FileNotFoundError:
                    os.mkdir(part, dir_fd=fds[-1])
                    fd = os.open(
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=fds[-1],
                    )
                    created.append(_OwnedDirectory(current, self._identity(os.fstat(fd))))
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
                    os.close(fd)
                    raise ValueError(f"reparse or non-directory component: {current}")
                fds.append(fd)
            return created
        except Exception as error:
            try:
                setattr(error, "created_directories", tuple(created))
            except Exception:
                pass
            self._close_fds(fds)
            raise
        finally:
            self._close_fds(fds)

    def create_file(self, path: Path, contents: bytes, root: Path) -> _Snapshot:
        path = Path(path)
        _, fds = self._open_directory(path.parent, root)
        fd = -1
        try:
            fd = os.open(
                path.name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=fds[-1],
            )
            info = os.fstat(fd)
            view = memoryview(contents)
            while view:
                written = os.write(fd, view[:1024 * 1024])
                if written <= 0:
                    raise OSError(errno.EIO, "short write", str(path))
                view = view[written:]
            os.fsync(fd)
            digest, size, _ = self._hash_fd(fd)
            return _Snapshot(size, digest, self._identity(info))
        except Exception:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.unlink(path.name, dir_fd=fds[-1])
            raise
        finally:
            if fd >= 0:
                os.close(fd)
            self._close_fds(fds)

    def backup_file(self, path: Path, expected: _Snapshot, backup_path: Path, root: Path) -> _Backup:
        path = Path(path)
        backup_path = Path(backup_path)
        _, fds = self._open_directory(path.parent, root)
        fd = -1
        try:
            fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fds[-1])
            info = os.fstat(fd)
            digest, size, _ = self._hash_fd(fd)
            actual = _Snapshot(size, digest, self._identity(info))
            if not _same_snapshot(actual, expected):
                raise ConcurrentModificationError(f"replacement source changed: {path}")
            os.rename(path.name, backup_path.name, src_dir_fd=fds[-1], dst_dir_fd=fds[-1])
            after_info = os.fstat(fd)
            after_digest, after_size, _ = self._hash_fd(fd)
            after = _Snapshot(after_size, after_digest, self._identity(after_info))
            if not _same_snapshot(after, expected):
                raise ConcurrentModificationError(f"replacement source changed during backup: {path}")
            return _Backup(path, backup_path, expected)
        finally:
            if fd >= 0:
                os.close(fd)
            self._close_fds(fds)

    def restore_backup(self, backup: _Backup, root: Path) -> bool:
        opened = self._open_file(backup.path, root)
        if opened is None:
            return False
        fd, _, fds = opened
        try:
            info = os.fstat(fd)
            digest, size, _ = self._hash_fd(fd)
            actual = _Snapshot(size, digest, self._identity(info))
            if not _same_snapshot(actual, backup.snapshot):
                return False
            try:
                os.stat(backup.original_path.name, dir_fd=fds[-1], follow_symlinks=False)
            except FileNotFoundError:
                os.rename(
                    backup.path.name, backup.original_path.name,
                    src_dir_fd=fds[-1], dst_dir_fd=fds[-1],
                )
                return True
            return False
        finally:
            os.close(fd)
            self._close_fds(fds)

    def discard_backup(self, backup: _Backup, root: Path) -> bool:
        return self.delete_if_matches(backup.path, backup.snapshot, root) in {"removed", "missing"}

    def delete_if_matches(self, path: Path, expected: _Snapshot, root: Path) -> str:
        opened = self._open_file(path, root)
        if opened is None:
            return "missing"
        fd, parent_fd, fds = opened
        try:
            info = os.fstat(fd)
            digest, size, _ = self._hash_fd(fd)
            actual = _Snapshot(size, digest, self._identity(info))
            if not _same_snapshot(actual, expected):
                return "preserved"
            os.unlink(Path(path).name, dir_fd=parent_fd)
            return "removed"
        finally:
            os.close(fd)
            self._close_fds(fds)

    def remove_directory_if_matches(self, owned: _OwnedDirectory, root: Path) -> str:
        path = owned.path
        try:
            _, fds = self._open_directory(path.parent, root)
        except FileNotFoundError:
            return "missing"
        try:
            try:
                fd = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fds[-1],
                )
            except FileNotFoundError:
                return "missing"
            try:
                if self._identity(os.fstat(fd)) != owned.identity:
                    return "preserved"
            finally:
                os.close(fd)
            try:
                os.rmdir(path.name, dir_fd=fds[-1])
            except OSError as error:
                if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    return "preserved"
                raise
            return "removed"
        finally:
            self._close_fds(fds)

    def probe(self, path: Path, root: Path, *, replace_existing: bool) -> None:
        path = Path(path)
        parent = _nearest_existing_parent(path.parent, root)
        _, fds = self._open_directory(parent, root)
        probe_name = f".orbfarmer-probe-{secrets.token_hex(12)}"
        renamed_name = f".orbfarmer-probe-{secrets.token_hex(12)}"
        fd = -1
        try:
            if replace_existing:
                existing = self._open_file(path, root, delete_access=True)
                if existing is None:
                    raise ConcurrentModificationError(f"replacement file disappeared: {path}")
                old_fd, _, old_fds = existing
                os.close(old_fd)
                self._close_fds(old_fds)
            fd = os.open(
                probe_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=fds[-1],
            )
            os.write(fd, b"orbfarmer permission probe")
            os.fsync(fd)
            os.rename(probe_name, renamed_name, src_dir_fd=fds[-1], dst_dir_fd=fds[-1])
            os.close(fd)
            fd = -1
            os.unlink(renamed_name, dir_fd=fds[-1])
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.unlink(probe_name, dir_fd=fds[-1])
                os.close(fd)
            self._close_fds(fds)


def _inspection_root(path: Path) -> Path:
    path = Path(path)
    parts = path.parts
    if len(parts) < 3:
        raise ValueError("cannot infer library root from target")
    try:
        steamapps_index = next(index for index, part in enumerate(parts) if part.casefold() == "steamapps")
    except StopIteration as error:
        raise ValueError("target is outside a Steam library") from error
    return Path(*parts[:steamapps_index]) if os.name != "nt" else Path(*parts[:steamapps_index])


def _nearest_existing_parent(path: Path, root: Path) -> Path:
    candidate = Path(path)
    root = Path(root)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"destination escapes library root: {candidate}") from error
    while True:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if candidate == root:
                raise
            candidate = candidate.parent
            continue
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"probe parent is not a safe directory: {candidate}")
        return candidate


def _is_access_denied(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    if winerror in {5, 1314}:
        return True
    if winerror in {32, 33}:
        return False
    return isinstance(error, PermissionError) or error.errno in {errno.EACCES, errno.EPERM}


if os.name == "nt":
    from ctypes import wintypes as _wintypes

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", _wintypes.USHORT),
            ("MaximumLength", _wintypes.USHORT),
            ("Buffer", _wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", _wintypes.ULONG),
            ("RootDirectory", _wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", _wintypes.ULONG),
            ("SecurityDescriptor", _wintypes.LPVOID),
            ("SecurityQualityOfService", _wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", _FileId128)]

    class _FileTime(ctypes.Structure):
        _fields_ = [("dwLowDateTime", _wintypes.DWORD), ("dwHighDateTime", _wintypes.DWORD)]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", _wintypes.DWORD),
            ("ftCreationTime", _FileTime),
            ("ftLastAccessTime", _FileTime),
            ("ftLastWriteTime", _FileTime),
            ("dwVolumeSerialNumber", _wintypes.DWORD),
            ("nFileSizeHigh", _wintypes.DWORD),
            ("nFileSizeLow", _wintypes.DWORD),
            ("nNumberOfLinks", _wintypes.DWORD),
            ("nFileIndexHigh", _wintypes.DWORD),
            ("nFileIndexLow", _wintypes.DWORD),
        ]

    class _FileRenameFlags(ctypes.Union):
        _fields_ = [("ReplaceIfExists", ctypes.c_ubyte), ("Flags", _wintypes.DWORD)]

    class _FileRenameInfo(ctypes.Structure):
        _anonymous_ = ("RenameFlags",)
        _fields_ = [
            ("RenameFlags", _FileRenameFlags),
            ("RootDirectory", _wintypes.HANDLE),
            ("FileNameLength", _wintypes.DWORD),
            ("FileName", ctypes.c_wchar * 1),
        ]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", _wintypes.BOOL)]

    class _FileDispositionInfoEx(ctypes.Structure):
        _fields_ = [("Flags", _wintypes.DWORD)]


class _WinAPI:
    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows native adapter is not available")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self.kernel32.CreateFileW.argtypes = [
            _wintypes.LPCWSTR, _wintypes.DWORD, _wintypes.DWORD,
            _wintypes.LPVOID, _wintypes.DWORD, _wintypes.DWORD, _wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = _wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [_wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = _wintypes.BOOL
        self.kernel32.GetFileInformationByHandle.argtypes = [
            _wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation),
        ]
        self.kernel32.GetFileInformationByHandle.restype = _wintypes.BOOL
        self.kernel32.GetFileInformationByHandleEx.argtypes = [
            _wintypes.HANDLE, ctypes.c_int, _wintypes.LPVOID, _wintypes.DWORD,
        ]
        self.kernel32.GetFileInformationByHandleEx.restype = _wintypes.BOOL
        self.kernel32.GetFinalPathNameByHandleW.argtypes = [
            _wintypes.HANDLE, _wintypes.LPWSTR, _wintypes.DWORD, _wintypes.DWORD,
        ]
        self.kernel32.GetFinalPathNameByHandleW.restype = _wintypes.DWORD
        self.kernel32.GetFileSizeEx.argtypes = [_wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
        self.kernel32.GetFileSizeEx.restype = _wintypes.BOOL
        self.kernel32.SetFilePointerEx.argtypes = [
            _wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), _wintypes.DWORD,
        ]
        self.kernel32.SetFilePointerEx.restype = _wintypes.BOOL
        self.kernel32.ReadFile.argtypes = [
            _wintypes.HANDLE, _wintypes.LPVOID, _wintypes.DWORD,
            ctypes.POINTER(_wintypes.DWORD), _wintypes.LPVOID,
        ]
        self.kernel32.ReadFile.restype = _wintypes.BOOL
        self.kernel32.WriteFile.argtypes = [
            _wintypes.HANDLE, _wintypes.LPCVOID, _wintypes.DWORD,
            ctypes.POINTER(_wintypes.DWORD), _wintypes.LPVOID,
        ]
        self.kernel32.WriteFile.restype = _wintypes.BOOL
        self.kernel32.FlushFileBuffers.argtypes = [_wintypes.HANDLE]
        self.kernel32.FlushFileBuffers.restype = _wintypes.BOOL
        self.kernel32.SetFileInformationByHandle.argtypes = [
            _wintypes.HANDLE, ctypes.c_int, _wintypes.LPVOID, _wintypes.DWORD,
        ]
        self.kernel32.SetFileInformationByHandle.restype = _wintypes.BOOL
        self.ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(_wintypes.HANDLE), _wintypes.ULONG,
            ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IoStatusBlock),
            ctypes.POINTER(ctypes.c_longlong), _wintypes.ULONG, _wintypes.ULONG,
            _wintypes.ULONG, _wintypes.ULONG, _wintypes.LPVOID, _wintypes.ULONG,
        ]
        self.ntdll.NtCreateFile.restype = ctypes.c_long
        self.ntdll.NtSetInformationFile.argtypes = [
            _wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock), _wintypes.LPVOID,
            _wintypes.ULONG, _wintypes.ULONG,
        ]
        self.ntdll.NtSetInformationFile.restype = ctypes.c_long
        self.ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        self.ntdll.RtlNtStatusToDosError.restype = _wintypes.ULONG

    def check(self, result: Any) -> Any:
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def nt_error(self, status: int, path: Path) -> OSError:
        winerror = int(self.ntdll.RtlNtStatusToDosError(status))
        error = ctypes.WinError(winerror)
        error.filename = str(path)
        return error


_win_api: _WinAPI | None = None


def _get_win_api() -> _WinAPI:
    global _win_api
    if _win_api is None:
        _win_api = _WinAPI()
    return _win_api


_GENERIC_SHARE_READ = 0x00000001
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x00000040
_OBJ_DONT_REPARSE = 0x00001000
_FILE_READ_DATA = 0x00000001
_FILE_WRITE_DATA = 0x00000002
_FILE_ADD_FILE = 0x00000002
_FILE_ADD_SUBDIRECTORY = 0x00000004
_FILE_LIST_DIRECTORY = 0x00000001
_FILE_TRAVERSE = 0x00000020
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_DELETE_CHILD = 0x00000040
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


class _WinHandle:
    def __init__(self, api: _WinAPI, handle: int, path: Path) -> None:
        self.api = api
        self.handle = handle
        self.path = Path(path)
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.api.kernel32.CloseHandle(self.handle)
            self.closed = True

    def attrs(self) -> int:
        info = _ByHandleFileInformation()
        self.api.check(self.api.kernel32.GetFileInformationByHandle(self.handle, ctypes.byref(info)))
        return int(info.dwFileAttributes)

    def identity(self) -> str:
        info = _FileIdInfo()
        if self.api.kernel32.GetFileInformationByHandleEx(
            self.handle, 18, ctypes.byref(info), ctypes.sizeof(info),
        ):
            return f"{int(info.VolumeSerialNumber):016x}:{bytes(info.FileId.Identifier).hex()}"
        legacy = _ByHandleFileInformation()
        self.api.check(self.api.kernel32.GetFileInformationByHandle(self.handle, ctypes.byref(legacy)))
        index = (int(legacy.nFileIndexHigh) << 32) | int(legacy.nFileIndexLow)
        return f"{int(legacy.dwVolumeSerialNumber):08x}:{index:016x}"

    def final_path(self) -> Path:
        size = 512
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            written = self.api.kernel32.GetFinalPathNameByHandleW(self.handle, buffer, size, 0)
            if written == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if written < size:
                value = buffer.value
                if value.startswith("\\\\?\\UNC\\"):
                    value = "\\\\" + value[8:]
                elif value.startswith("\\\\?\\"):
                    value = value[4:]
                return Path(value)
            size = int(written) + 1

    def size(self) -> int:
        value = ctypes.c_longlong()
        self.api.check(self.api.kernel32.GetFileSizeEx(self.handle, ctypes.byref(value)))
        return int(value.value)

    def _seek_zero(self) -> None:
        self.api.check(self.api.kernel32.SetFilePointerEx(self.handle, 0, None, 0))

    def read_bytes(self, *, max_bytes: int | None = None) -> bytes:
        size = self.size()
        if max_bytes is not None and size > max_bytes:
            raise ValueError("file exceeds the allowed size")
        self._seek_zero()
        remaining = size
        chunks: list[bytes] = []
        while remaining:
            count = min(1024 * 1024, remaining)
            buffer = ctypes.create_string_buffer(count)
            read = _wintypes.DWORD()
            self.api.check(self.api.kernel32.ReadFile(
                self.handle, buffer, count, ctypes.byref(read), None,
            ))
            if not read.value:
                break
            chunks.append(buffer.raw[:read.value])
            remaining -= read.value
        if remaining:
            raise OSError(errno.EIO, "file changed or ended during read", str(self.path))
        self._seek_zero()
        return b"".join(chunks)

    def snapshot(self, *, read_bytes: bool = False, max_bytes: int | None = None) -> tuple[_Snapshot, bytes | None]:
        size = self.size()
        if max_bytes is not None and size > max_bytes:
            raise ValueError("file exceeds the allowed size")
        self._seek_zero()
        remaining = size
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if read_bytes else None
        total = 0
        while remaining:
            count = min(1024 * 1024, remaining)
            buffer = ctypes.create_string_buffer(count)
            read = _wintypes.DWORD()
            self.api.check(self.api.kernel32.ReadFile(
                self.handle, buffer, count, ctypes.byref(read), None,
            ))
            if not read.value:
                break
            block = buffer.raw[:read.value]
            digest.update(block)
            total += len(block)
            remaining -= len(block)
            if chunks is not None:
                chunks.append(block)
        if remaining:
            raise OSError(errno.EIO, "file changed or ended during hash", str(self.path))
        self._seek_zero()
        if total != size:
            raise OSError(errno.EIO, "file size changed during hash", str(self.path))
        snap = _Snapshot(size, digest.hexdigest(), self.identity())
        return snap, b"".join(chunks) if chunks is not None else None

    def write_bytes(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            block = bytes(view[:1024 * 1024])
            buffer = ctypes.create_string_buffer(block)
            written = _wintypes.DWORD()
            self.api.check(self.api.kernel32.WriteFile(
                self.handle, buffer, len(block), ctypes.byref(written), None,
            ))
            if not written.value:
                raise OSError(errno.EIO, "short file write", str(self.path))
            view = view[written.value:]
        self.api.check(self.api.kernel32.FlushFileBuffers(self.handle))

    def rename(self, parent: _WinHandle, new_name: str, expected_old_path: Path, new_path: Path) -> None:
        if expected_old_path.parent != new_path.parent:
            raise ValueError("transaction renames must stay in the same directory")
        if Path(new_name).name != new_name or "\\" in new_name or "/" in new_name:
            raise ValueError("handle rename accepts one path component")
        if not _same_windows_path(self.final_path(), expected_old_path):
            raise ConcurrentModificationError(f"opened file path changed: {expected_old_path}")
        filename = new_name.encode("utf-16-le")
        name_offset = _FileRenameInfo.FileName.offset
        if name_offset + ctypes.sizeof(ctypes.c_wchar) > ctypes.sizeof(_FileRenameInfo):
            raise RuntimeError("FILE_RENAME_INFO layout is not supported on this runtime")
        buffer_size = max(ctypes.sizeof(_FileRenameInfo), name_offset + len(filename))
        buffer = ctypes.create_string_buffer(buffer_size)
        info = ctypes.cast(buffer, ctypes.POINTER(_FileRenameInfo)).contents
        info.ReplaceIfExists = False
        # A simple basename with no RootDirectory renames within the source
        # handle's pinned directory. It does not reopen the target directory.
        info.RootDirectory = None
        info.FileNameLength = len(filename)
        ctypes.memmove(ctypes.addressof(buffer) + name_offset, filename, len(filename))
        status_block = _IoStatusBlock()
        status = self.api.ntdll.NtSetInformationFile(
            self.handle, ctypes.byref(status_block), buffer, len(buffer), 10,
        )
        if status < 0:
            raise self.api.nt_error(status, new_path)
        self.path = Path(new_path)
        if not _same_windows_path(self.final_path(), new_path):
            raise ConcurrentModificationError(f"renamed file resolved outside its expected path: {new_path}")

    def delete(self, expected_path: Path) -> None:
        if not _same_windows_path(self.final_path(), expected_path):
            raise ConcurrentModificationError(f"opened file path changed: {expected_path}")
        info = _FileDispositionInfoEx(3)
        if self.api.kernel32.SetFileInformationByHandle(
            self.handle, 21, ctypes.byref(info), ctypes.sizeof(info),
        ):
            return
        error = ctypes.get_last_error()
        if error not in {1, 50, 87}:
            raise ctypes.WinError(error)
        legacy = _FileDispositionInfo(True)
        self.api.check(self.api.kernel32.SetFileInformationByHandle(
            self.handle, 4, ctypes.byref(legacy), ctypes.sizeof(legacy),
        ))


class _WinDirectory:
    def __init__(self, api: _WinAPI, handle: int, path: Path) -> None:
        self.file = _WinHandle(api, handle, path)
        self.path = Path(path)

    @property
    def handle(self) -> int:
        return self.file.handle

    def close(self) -> None:
        self.file.close()


class _WindowsFilesystem:
    """Handle-relative Windows adapter used by the elevated helper."""

    def __init__(self) -> None:
        self.api = _get_win_api()

    @staticmethod
    def _path_equal(left: Path, right: Path) -> bool:
        return _same_windows_path(left, right)

    def _nt_open(
        self, parent: _WinDirectory, name: str, path: Path, *, access: int,
        disposition: int, options: int, share: int = _FILE_SHARE_READ,
    ) -> _WinHandle:
        if not name or name in {".", ".."} or "\\" in name or "/" in name:
            raise ValueError("native open accepts one validated path component")
        text = ctypes.create_unicode_buffer(name)
        encoded_length = len(name) * ctypes.sizeof(ctypes.c_wchar)
        unicode_name = _UnicodeString(
            encoded_length, encoded_length + ctypes.sizeof(ctypes.c_wchar),
            ctypes.cast(text, _wintypes.LPWSTR),
        )
        attrs = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes), parent.handle, ctypes.pointer(unicode_name),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE, None, None,
        )
        status_block = _IoStatusBlock()
        result = _wintypes.HANDLE()
        status = self.api.ntdll.NtCreateFile(
            ctypes.byref(result), access, ctypes.byref(attrs), ctypes.byref(status_block),
            None, 0x80, share, disposition, options, None, 0,
        )
        if status < 0:
            raise self.api.nt_error(status, path)
        return _WinHandle(self.api, result.value, path)

    def _open_volume_root(self, path: Path) -> _WinDirectory:
        root = Path(path.anchor)
        if not root.anchor or not PureWindowsPath(str(root)).drive:
            raise ValueError("native filesystem requires an absolute local drive path")
        handle = self.api.kernel32.CreateFileW(
            str(root),
            _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            _FILE_SHARE_READ, None, 3,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if handle == _wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        directory = _WinDirectory(self.api, handle, root)
        attrs = directory.file.attrs()
        if attrs & _FILE_ATTRIBUTE_REPARSE_POINT or not attrs & _FILE_ATTRIBUTE_DIRECTORY:
            directory.close()
            raise ValueError(f"unsafe volume root: {root}")
        if not self._path_equal(directory.file.final_path(), root):
            directory.close()
            raise ValueError(f"volume root handle resolved unexpectedly: {root}")
        return directory

    def _pin_directory(
        self, path: Path, root: Path, *, final_access: int = 0,
    ) -> tuple[_WinDirectory, list[_WinDirectory]]:
        path = Path(path)
        root = Path(root)
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"destination escapes library root: {path}") from error
        drive_root = Path(path.anchor)
        if Path(root.anchor) != drive_root:
            raise ValueError("library root and destination must use the same local drive")
        directories: list[_WinDirectory] = [self._open_volume_root(path)]
        current = drive_root
        components = path.parts[1:]
        try:
            for index, part in enumerate(components):
                current = current / part
                # TRAVERSE is a share-relevant execute bit. Metadata-only
                # handles do not reserve sharing against later writers.
                access = _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
                if index == len(components) - 1:
                    access |= final_access
                opened = self._nt_open(
                    directories[-1], part, current, access=access,
                    disposition=_FILE_OPEN,
                    options=_FILE_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
                    share=_FILE_SHARE_READ,
                )
                attrs = opened.attrs()
                if attrs & _FILE_ATTRIBUTE_REPARSE_POINT or not attrs & _FILE_ATTRIBUTE_DIRECTORY:
                    opened.close()
                    raise ValueError(f"reparse or non-directory component: {current}")
                if not self._path_equal(opened.final_path(), current):
                    opened.close()
                    raise ValueError(f"directory handle resolved unexpectedly: {current}")
                directories.append(_WinDirectory(self.api, opened.handle, current))
                opened.closed = True
            return directories[-1], directories
        except Exception:
            for directory in reversed(directories):
                with contextlib.suppress(OSError):
                    directory.close()
            raise

    @staticmethod
    def _close_directories(directories: list[_WinDirectory]) -> None:
        for directory in reversed(directories):
            with contextlib.suppress(Exception):
                directory.close()

    def _open_file(
        self, path: Path, root: Path, *, delete_access: bool = False,
        write_access: bool = False, allow_missing: bool = True,
    ) -> tuple[_WinHandle, list[_WinDirectory]] | None:
        path = Path(path)
        try:
            parent, directories = self._pin_directory(path.parent, root)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        access = _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
        if delete_access:
            access |= _DELETE
        if write_access:
            access |= _FILE_WRITE_DATA
        try:
            opened = self._nt_open(
                parent, path.name, path, access=access, disposition=_FILE_OPEN,
                options=_FILE_NON_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
            )
        except FileNotFoundError:
            self._close_directories(directories)
            if allow_missing:
                return None
            raise
        try:
            attrs = opened.attrs()
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"reparse file target is not allowed: {path}")
            if attrs & _FILE_ATTRIBUTE_DIRECTORY:
                raise IsADirectoryError(path)
            if not self._path_equal(opened.final_path(), path):
                raise ValueError(f"file handle resolved unexpectedly: {path}")
            return opened, directories
        except Exception:
            opened.close()
            self._close_directories(directories)
            raise

    def inspect(
        self, path: Path, *, read_bytes: bool = False, max_bytes: int | None = None,
    ) -> tuple[_Snapshot | None, bytes | None]:
        root = _inspection_root(Path(path))
        opened = self._open_file(path, root)
        if opened is None:
            return None, None
        file, directories = opened
        try:
            snapshot, contents = file.snapshot(read_bytes=read_bytes, max_bytes=max_bytes)
            if not self._path_equal(file.final_path(), path):
                raise ConcurrentModificationError(f"destination path changed during snapshot: {path}")
            return snapshot, contents
        finally:
            file.close()
            self._close_directories(directories)

    def ensure_parent_dirs(self, parent_path: Path, root: Path) -> list[_OwnedDirectory]:
        parent_path = Path(parent_path)
        root = Path(root)
        try:
            parent_path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"destination escapes library root: {parent_path}") from error
        existing_parent = _nearest_existing_parent(parent_path, root)
        relative = parent_path.relative_to(existing_parent)
        current, directories = self._pin_directory(
            existing_parent, root,
            final_access=_FILE_ADD_SUBDIRECTORY if relative.parts else 0,
        )
        created: list[_OwnedDirectory] = []
        path = existing_parent
        try:
            for index, part in enumerate(relative.parts):
                path = path / part
                access = _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
                if index < len(relative.parts) - 1:
                    access |= _FILE_ADD_SUBDIRECTORY
                try:
                    opened = self._nt_open(
                        current, part, path, access=access | _DELETE, disposition=_FILE_CREATE,
                        options=_FILE_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
                    )
                    created.append(_OwnedDirectory(path, opened.identity()))
                except FileExistsError:
                    opened = self._nt_open(
                        current, part, path, access=access, disposition=_FILE_OPEN,
                        options=_FILE_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
                    )
                attrs = opened.attrs()
                if attrs & _FILE_ATTRIBUTE_REPARSE_POINT or not attrs & _FILE_ATTRIBUTE_DIRECTORY:
                    opened.close()
                    raise ValueError(f"reparse or non-directory component: {path}")
                if not self._path_equal(opened.final_path(), path):
                    opened.close()
                    raise ValueError(f"directory handle resolved unexpectedly: {path}")
                directory = _WinDirectory(self.api, opened.handle, path)
                opened.closed = True
                directories.append(directory)
                current = directory
            return created
        except Exception as error:
            try:
                setattr(error, "created_directories", tuple(created))
            except Exception:
                pass
            raise
        finally:
            self._close_directories(directories)

    def create_file(self, path: Path, contents: bytes, root: Path) -> _Snapshot:
        path = Path(path)
        parent, directories = self._pin_directory(
            path.parent, root, final_access=_FILE_ADD_FILE,
        )
        file: _WinHandle | None = None
        try:
            file = self._nt_open(
                parent, path.name, path, access=(
                    _FILE_READ_DATA | _FILE_WRITE_DATA | _FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE
                ), disposition=_FILE_CREATE,
                options=_FILE_NON_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
            )
            if file.attrs() & _FILE_ATTRIBUTE_REPARSE_POINT or not self._path_equal(file.final_path(), path):
                raise ValueError(f"new file handle resolved unexpectedly: {path}")
            file.write_bytes(contents)
            snapshot, _ = file.snapshot()
            return snapshot
        except Exception as error:
            if file is not None:
                try:
                    partial, _ = file.snapshot()
                    file.delete(path)
                except Exception:
                    setattr(error, "rollback_leftover", str(path))
            raise
        finally:
            if file is not None:
                file.close()
            self._close_directories(directories)

    def _rename(
        self, file: _WinHandle, source_path: Path, target_path: Path, root: Path,
        *, pinned_parent: _WinHandle | None = None,
    ) -> None:
        if source_path.parent != target_path.parent:
            raise ValueError("transaction backup rename must stay in one directory")
        if pinned_parent is not None:
            if not self._path_equal(pinned_parent.final_path(), target_path.parent):
                raise ConcurrentModificationError(f"rename parent changed: {target_path.parent}")
            file.rename(pinned_parent, target_path.name, source_path, target_path)
            return
        parent, directories = self._pin_directory(
            target_path.parent, root,
        )
        try:
            file.rename(parent.file, target_path.name, source_path, target_path)
        finally:
            self._close_directories(directories)

    def backup_file(self, path: Path, expected: _Snapshot, backup_path: Path, root: Path) -> _Backup:
        opened = self._open_file(path, root, delete_access=True, allow_missing=False)
        assert opened is not None
        file, directories = opened
        try:
            actual, _ = file.snapshot()
            if not _same_snapshot(actual, expected):
                raise ConcurrentModificationError(f"replacement source changed: {path}")
            self._rename(file, path, backup_path, root, pinned_parent=directories[-1].file)
            after, _ = file.snapshot()
            if not _same_snapshot(after, expected):
                raise ConcurrentModificationError(f"replacement source changed during backup: {path}")
            return _Backup(Path(path), Path(backup_path), expected, file)
        except Exception:
            file.close()
            raise
        finally:
            self._close_directories(directories)

    def restore_backup(self, backup: _Backup, root: Path) -> bool:
        file: _WinHandle = backup.handle
        if file is None or file.closed:
            opened = self._open_file(backup.path, root, delete_access=True, allow_missing=False)
            assert opened is not None
            file, directories = opened
        else:
            directories = []
        try:
            actual, _ = file.snapshot()
            if not _same_snapshot(actual, backup.snapshot):
                return False
            current = self._open_file(backup.original_path, root, allow_missing=True)
            if current is not None:
                current_file, current_dirs = current
                current_file.close()
                self._close_directories(current_dirs)
                return False
            self._rename(file, backup.path, backup.original_path, root)
            backup.path = backup.original_path
            file.close()
            backup.handle = None
            return True
        finally:
            self._close_directories(directories)

    def discard_backup(self, backup: _Backup, root: Path) -> bool:
        file: _WinHandle = backup.handle
        if file is None or file.closed:
            opened = self._open_file(backup.path, root, delete_access=True, allow_missing=True)
            if opened is None:
                return True
            file, directories = opened
        else:
            directories = []
        try:
            actual, _ = file.snapshot()
            if not _same_snapshot(actual, backup.snapshot):
                return False
            file.delete(backup.path)
            file.close()
            backup.handle = None
            return True
        finally:
            self._close_directories(directories)

    def delete_if_matches(self, path: Path, expected: _Snapshot, root: Path) -> str:
        opened = self._open_file(path, root, delete_access=True, allow_missing=True)
        if opened is None:
            return "missing"
        file, directories = opened
        try:
            actual, _ = file.snapshot()
            if not _same_snapshot(actual, expected):
                return "preserved"
            file.delete(path)
            return "removed"
        finally:
            file.close()
            self._close_directories(directories)

    def remove_directory_if_matches(self, owned: _OwnedDirectory, root: Path) -> str:
        path = owned.path
        try:
            parent, directories = self._pin_directory(path.parent, root)
        except FileNotFoundError:
            return "missing"
        directory: _WinHandle | None = None
        try:
            try:
                directory = self._nt_open(
                    parent, path.name, path,
                    access=_FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE,
                    disposition=_FILE_OPEN,
                    options=_FILE_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
                )
            except FileNotFoundError:
                return "missing"
            attrs = directory.attrs()
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT or not attrs & _FILE_ATTRIBUTE_DIRECTORY:
                return "preserved"
            if directory.identity() != owned.identity or not self._path_equal(directory.final_path(), path):
                return "preserved"
            try:
                directory.delete(path)
            except OSError as error:
                if getattr(error, "winerror", None) in {145, 183}:
                    return "preserved"
                raise
            return "removed"
        finally:
            if directory is not None:
                directory.close()
            self._close_directories(directories)

    def _probe_file_and_directory(self, parent_path: Path, root: Path, *, create_directory: bool) -> None:
        parent, directories = self._pin_directory(
            parent_path, root, final_access=_FILE_ADD_SUBDIRECTORY if create_directory else _FILE_ADD_FILE,
        )
        file: _WinHandle | None = None
        directory: _WinHandle | None = None
        file_name = f".orbfarmer-probe-{secrets.token_hex(12)}"
        renamed_name = f".orbfarmer-probe-{secrets.token_hex(12)}"
        dir_name = f".orbfarmer-probe-dir-{secrets.token_hex(12)}"
        try:
            file_parent = parent.file
            file_parent_path = parent_path
            if create_directory:
                dir_path = parent_path / dir_name
                directory = self._nt_open(
                    parent, dir_name, dir_path,
                    access=_FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _FILE_ADD_FILE | _FILE_ADD_SUBDIRECTORY | _DELETE | _SYNCHRONIZE,
                    disposition=_FILE_CREATE,
                    options=_FILE_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
                )
                if directory.attrs() & _FILE_ATTRIBUTE_REPARSE_POINT or not directory.attrs() & _FILE_ATTRIBUTE_DIRECTORY:
                    raise ValueError("permission probe directory handle is not a safe directory")
                if not self._path_equal(directory.final_path(), dir_path):
                    raise ConcurrentModificationError("permission probe directory resolved unexpectedly")
                file_parent = directory
                file_parent_path = dir_path
            file_path = file_parent_path / file_name
            file = self._nt_open(
                file_parent, file_name, file_path,
                access=_FILE_READ_DATA | _FILE_WRITE_DATA | _FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE,
                disposition=_FILE_CREATE,
                options=_FILE_NON_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
            )
            file.write_bytes(b"orbfarmer permission probe")
            first, _ = file.snapshot()
            moved_path = file_parent_path / renamed_name
            file.rename(file_parent, renamed_name, file_path, moved_path)
            moved, _ = file.snapshot()
            if not _same_snapshot(moved, first):
                raise ConcurrentModificationError("permission probe file changed during rename")
            file.delete(moved_path)
            file.close()
            file = None

            if directory is not None:
                directory.delete(dir_path)
                directory.close()
                directory = None
        finally:
            if file is not None:
                with contextlib.suppress(Exception):
                    if not file.closed:
                        candidate = Path(file.path)
                        file.delete(candidate)
                file.close()
            if directory is not None:
                with contextlib.suppress(Exception):
                    if not directory.closed:
                        directory.delete(Path(directory.path))
                directory.close()
            self._close_directories(directories)

    def probe(self, path: Path, root: Path, *, replace_existing: bool) -> None:
        path = Path(path)
        if replace_existing:
            opened = self._open_file(path, root, delete_access=True, allow_missing=False)
            assert opened is not None
            file, directories = opened
            try:
                file.snapshot()
            finally:
                file.close()
                self._close_directories(directories)
        parent = _nearest_existing_parent(path.parent, root)
        self._probe_file_and_directory(parent, root, create_directory=parent != path.parent)


def _same_windows_path(left: Path, right: Path) -> bool:
    def normalized(value: str) -> str:
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return ntpath.normcase(ntpath.normpath(value))

    return normalized(os.fspath(left)) == normalized(os.fspath(right))


def _get_filesystem() -> _WindowsFilesystem | _PortableFilesystem:
    if os.name == "nt":
        return _WindowsFilesystem()
    return _PortableFilesystem()


def _decode_plan_assets(plan: dict[str, Any]) -> dict[str, bytes]:
    return {
        kind: base64.b64decode(value.encode("ascii"), validate=True)
        for kind, value in plan["assets"].items()
    }


def _is_verified_timer(contents: bytes, timer_source: bytes) -> bool:
    try:
        text = contents.decode("utf-8")
        source_prefix = timer_source.decode("utf-8") + "\n"
    except UnicodeDecodeError:
        return False
    if not text.startswith(source_prefix):
        return False
    lines = text[len(source_prefix):].splitlines()
    if lines and lines[0] == _TIMER_MARKER:
        if len(lines) < 4 or not lines[1].startswith(_PYTHONHOME_MARKER):
            return False
        try:
            home = json.loads(lines[1][len(_PYTHONHOME_MARKER):])
            if not isinstance(home, str):
                return False
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        lines = lines[2:]
    if len(lines) != 2 or not re.fullmatch(r"TIMER_MINUTES = \d+", lines[0]):
        return False
    call = re.fullmatch(r"run_timer\(TIMER_MINUTES, theme=(.*)\)", lines[1])
    if not call:
        return False
    try:
        theme = ast.literal_eval(call.group(1))
    except (SyntaxError, ValueError):
        return False
    return isinstance(theme, dict)


def _verify_replacement_sidecars(
    plan: dict[str, Any], paths: dict[str, Path],
    snapshots: dict[str, _Snapshot | None], runtime: _Runtime,
    *, fs: _WindowsFilesystem | _PortableFilesystem,
) -> None:
    if snapshots["exe"] is None:
        raise FileNotFoundError(f"replacement executable does not exist: {paths['exe']}")
    timer_snapshot = snapshots["timer"]
    if timer_snapshot is not None:
        actual, contents = fs.inspect(paths["timer"], read_bytes=True, max_bytes=2 * 1024 * 1024)
        if not _same_snapshot(actual, timer_snapshot):
            raise ConcurrentModificationError(f"replacement timer changed: {paths['timer']}")
        assert contents is not None
        if not _is_verified_timer(contents, runtime.timer_source):
            raise ValueError(f"unrecognized Orbfarmer timer sidecar: {paths['timer']}")
    asset_bytes = _decode_plan_assets(plan)
    for kind, expected_data in asset_bytes.items():
        existing = snapshots[kind]
        if existing is None:
            continue
        expected_hash = hashlib.sha256(expected_data).hexdigest()
        if existing.size != len(expected_data) or existing.sha256 != expected_hash:
            raise ValueError(f"unrecognized Orbfarmer {kind} sidecar: {paths[kind]}")


def _inspect_plan_outputs(
    plan: dict[str, Any], fs: _WindowsFilesystem | _PortableFilesystem,
) -> tuple[dict[str, Path], dict[str, _Snapshot | None], bytes | None]:
    paths = _paths_for(plan)
    snapshots: dict[str, _Snapshot | None] = {}
    manifest_bytes: bytes | None = None
    for key, path in paths.items():
        read = key == "manifest"
        max_bytes = _MAX_ACF_BYTES if read else None
        snapshot, contents = fs.inspect(path, read_bytes=read, max_bytes=max_bytes)
        snapshots[key] = snapshot
        if key == "manifest" and snapshot is not None:
            manifest_bytes = contents
    return paths, snapshots, manifest_bytes


def _check_current_plan_state(
    plan: dict[str, Any], paths: dict[str, Path],
    snapshots: dict[str, _Snapshot | None], manifest_bytes: bytes | None,
) -> None:
    for key, actual in snapshots.items():
        expected = plan["before"][key]
        if not _same_snapshot(actual, expected):
            raise ConcurrentModificationError(f"destination changed since planning: {paths[key]}")
    if plan["manifest_action"] == "preserve":
        if manifest_bytes is None:
            raise ConcurrentModificationError(f"existing appmanifest disappeared: {paths['manifest']}")
        current_installdir = parse_manifest(manifest_bytes, plan["appid"])
        if current_installdir != plan["installdir"]:
            raise ValueError("existing appmanifest does not match the selected install folder")


def _runtime_mode_is_frozen() -> bool:
    with _runtime_lock:
        if _runtime_override is not None:
            return _runtime_override.frozen
    return bool(getattr(sys, "frozen", False))


def _selected_mutations(
    plan: dict[str, Any], paths: dict[str, Path], snapshots: dict[str, _Snapshot | None],
    runtime: _Runtime,
) -> tuple[tuple[Path, bool], ...]:
    """Return derived write paths and whether each path replaces an original."""
    result: list[tuple[Path, bool]] = []
    action = plan["action"]
    if action in {"create", "replace"}:
        result.append((paths["exe"], action == "replace"))
        timer_selected = not runtime.frozen or (
            action == "replace" and snapshots["timer"] is not None
        )
        if timer_selected:
            result.append((paths["timer"], action == "replace" and snapshots["timer"] is not None))
        for kind in ("hero", "icon"):
            if kind in plan["assets"]:
                result.append((paths[kind], action == "replace" and snapshots[kind] is not None))
    if plan["manifest_action"] == "create":
        result.append((paths["manifest"], False))
    return tuple(result)


def probe_permissions(plan: dict) -> ProbeResult:
    _validate_plan(plan)
    if not has_writes(plan):
        return ProbeResult(False, ())
    runtime_mode = _runtime_mode_is_frozen()
    runtime = _Runtime(b"", b"", runtime_mode, "")
    paths = _paths_for(plan)
    snapshots = {
        key: _snapshot_from_plan(plan["before"][key])
        for key in _BEFORE_KEYS
    }
    selected = _selected_mutations(plan, paths, snapshots, runtime)
    fs = _get_filesystem()
    denied: list[str] = []
    for path, replace_existing in selected:
        try:
            fs.probe(path, Path(plan["library_root"]), replace_existing=replace_existing)
        except OSError as error:
            if not _is_access_denied(error):
                raise
            denied.append(str(path))
        # Each probe exercises create/rename/delete in its selected parent.
        # De-duplicate only after attempting existing-file DELETE access.
        # Native adapter may be expensive, but each selected operation is checked.
    return ProbeResult(bool(denied), tuple(dict.fromkeys(denied)))


def _snapshot_from_plan(value: dict[str, Any] | None) -> _Snapshot | None:
    if value is None:
        return None
    return _Snapshot(value["size"], value["sha256"], value["identity"])


class FileTransaction:
    """One prepared filesystem generation with private ownership records."""

    def __init__(
        self, *, target_path: Path, library_root: Path,
        filesystem: _WindowsFilesystem | _PortableFilesystem,
        owned_files: list[_OwnedFile], backups: list[_Backup],
        owned_dirs: list[_OwnedDirectory],
    ) -> None:
        self.target_path = Path(target_path)
        self._root = Path(library_root)
        self._fs = filesystem
        # prepare() fills these ledgers as each filesystem mutation succeeds.
        self._owned_files = owned_files
        self._backups = backups
        self._owned_dirs = owned_dirs
        self._state = "prepared"
        self._last_report = CleanupReport()

    def commit(self) -> CleanupReport:
        if self._state in {"aborted", "cleaned"}:
            return self._last_report
        if self._state == "prepared":
            removed: list[str] = []
            leftovers: list[str] = []
            unresolved: list[str] = []
            for backup in list(self._backups):
                try:
                    if self._fs.discard_backup(backup, self._root):
                        removed.append(str(backup.path))
                        self._backups.remove(backup)
                    else:
                        unresolved.append(str(backup.path))
                        if backup.handle is not None and hasattr(backup.handle, "close"):
                            backup.handle.close()
                            backup.handle = None
                except OSError:
                    unresolved.append(str(backup.path))
                    if backup.handle is not None and hasattr(backup.handle, "close"):
                        with contextlib.suppress(Exception):
                            backup.handle.close()
                        backup.handle = None
            self._state = "committed"
            self._last_report = CleanupReport(
                removed=tuple(removed), leftovers=tuple(leftovers), backups=tuple(unresolved),
            )
        return self._last_report

    def abort(self) -> CleanupReport:
        if self._state in {"committed", "cleaned"}:
            return self._last_report
        removed: list[str] = list(self._last_report.removed)
        preserved: list[str] = list(self._last_report.preserved)
        leftovers: list[str] = []
        unresolved_backups: list[str] = []
        remaining_files: list[_OwnedFile] = []
        for owned in reversed(self._owned_files):
            try:
                status = self._fs.delete_if_matches(owned.path, owned.snapshot, self._root)
                if status in {"removed", "missing"}:
                    if status == "removed":
                        removed.append(str(owned.path))
                else:
                    preserved.append(str(owned.path))
            except OSError:
                leftovers.append(str(owned.path))
                remaining_files.append(owned)
        self._owned_files[:] = list(reversed(remaining_files))
        for backup in reversed(self._backups):
            try:
                if self._fs.restore_backup(backup, self._root):
                    removed.append(str(backup.path))
                    self._backups.remove(backup)
                else:
                    preserved.append(str(backup.original_path))
                    unresolved_backups.append(str(backup.path))
                    self._close_backup(backup)
            except OSError:
                unresolved_backups.append(str(backup.path))
                self._close_backup(backup)
        for owned_dir in reversed(self._owned_dirs):
            try:
                status = self._fs.remove_directory_if_matches(owned_dir, self._root)
                if status == "removed":
                    removed.append(str(owned_dir.path))
                elif status == "preserved":
                    pending = [item.path for item in self._owned_files] + [item.path for item in self._backups]
                    if any(owned_dir.path in path.parents for path in pending):
                        leftovers.append(str(owned_dir.path))
                    else:
                        preserved.append(str(owned_dir.path))
            except OSError:
                leftovers.append(str(owned_dir.path))
        # A failed removal may succeed after a locked owned file is removed on a
        # later cleanup call. Keep only those identities for another attempt.
        remaining_dirs: list[_OwnedDirectory] = []
        for owned_dir in reversed(self._owned_dirs):
            # The previous pass has already attempted each directory. An entry
            # remains only when its path is still in this pass's leftovers.
            if str(owned_dir.path) in leftovers:
                remaining_dirs.append(owned_dir)
        self._owned_dirs[:] = list(reversed(remaining_dirs))
        self._state = "aborted"
        self._last_report = CleanupReport(
            tuple(removed), tuple(preserved), tuple(leftovers), tuple(unresolved_backups),
        )
        return self._last_report

    def cleanup(self) -> CleanupReport:
        if self._state == "prepared":
            return self.abort()
        if self._state == "aborted":
            # Re-run idempotent cleanup for files that were locked on the first try.
            return self.abort()
        removed = list(self._last_report.removed)
        preserved = list(self._last_report.preserved)
        leftovers: list[str] = []
        unresolved_backups: list[str] = []
        for backup in list(self._backups):
            try:
                if self._fs.discard_backup(backup, self._root):
                    removed.append(str(backup.path))
                    self._backups.remove(backup)
                else:
                    unresolved_backups.append(str(backup.path))
                    self._close_backup(backup)
            except OSError:
                unresolved_backups.append(str(backup.path))
                self._close_backup(backup)
        remaining_files: list[_OwnedFile] = []
        for owned in reversed(self._owned_files):
            try:
                status = self._fs.delete_if_matches(owned.path, owned.snapshot, self._root)
                if status == "removed":
                    removed.append(str(owned.path))
                elif status == "preserved":
                    preserved.append(str(owned.path))
            except OSError:
                leftovers.append(str(owned.path))
                remaining_files.append(owned)
        self._owned_files[:] = list(reversed(remaining_files))
        remaining_dirs: list[_OwnedDirectory] = []
        for owned_dir in reversed(self._owned_dirs):
            try:
                status = self._fs.remove_directory_if_matches(owned_dir, self._root)
                if status == "removed":
                    removed.append(str(owned_dir.path))
                elif status == "preserved":
                    pending = [item.path for item in self._owned_files] + [item.path for item in self._backups]
                    if any(owned_dir.path in path.parents for path in pending):
                        leftovers.append(str(owned_dir.path))
                        remaining_dirs.append(owned_dir)
                    else:
                        preserved.append(str(owned_dir.path))
            except OSError:
                leftovers.append(str(owned_dir.path))
                remaining_dirs.append(owned_dir)
        self._owned_dirs[:] = list(reversed(remaining_dirs))
        if not (self._backups or self._owned_files or self._owned_dirs):
            self._state = "cleaned"
        self._last_report = CleanupReport(
            tuple(dict.fromkeys(removed)), tuple(dict.fromkeys(preserved)),
            tuple(leftovers), tuple(unresolved_backups),
        )
        return self._last_report

    @staticmethod
    def _close_backup(backup: _Backup) -> None:
        if backup.handle is not None and hasattr(backup.handle, "close"):
            with contextlib.suppress(Exception):
                backup.handle.close()
            backup.handle = None


def _timer_source_for(runtime: _Runtime, plan: dict[str, Any]) -> bytes:
    theme = dict(plan["theme"])
    for kind in plan["assets"]:
        theme[kind] = _paths_for(plan)[kind].name
    source = runtime.timer_source.decode("utf-8")
    home = json.dumps(runtime.python_home)
    code = (
        f"\n{_TIMER_MARKER}\n"
        f"{_PYTHONHOME_MARKER}{home}\n"
        f"TIMER_MINUTES = {plan['minutes']}\n"
        f"run_timer(TIMER_MINUTES, theme={theme!r})\n"
    )
    return (source + code).encode("utf-8")


def _executable_contents(runtime: _Runtime, plan: dict[str, Any]) -> bytes:
    if not runtime.frozen:
        return runtime.executable
    config = {
        "TIMER_MINUTES": plan["minutes"],
        "TIMER_THEME": {
            **plan["theme"],
            **{kind: _paths_for(plan)[kind].name for kind in plan["assets"]},
        },
    }
    return runtime.executable + _BAKED_MARKER + json.dumps(config).encode("utf-8") + _BAKED_MARKER


def _backup_name(path: Path, transaction_id: str) -> Path:
    return path.with_name(f".{path.name}.orbfarmer-{transaction_id}-{secrets.token_hex(8)}.bak")


def prepare(plan: dict) -> FileTransaction:
    """Revalidate, stage all selected writes, and return their private ledger."""
    _validate_plan(plan)
    fs = _get_filesystem()
    root = Path(plan["library_root"])
    target_path = _paths_for(plan)["exe"]
    owned_files: list[_OwnedFile] = []
    backups: list[_Backup] = []
    owned_dirs: list[_OwnedDirectory] = []
    transaction = FileTransaction(
        target_path=target_path, library_root=root, filesystem=fs,
        owned_files=owned_files, backups=backups, owned_dirs=owned_dirs,
    )
    current_path = target_path
    try:
        paths, snapshots, manifest_bytes = _inspect_plan_outputs(plan, fs)
        _check_current_plan_state(plan, paths, snapshots, manifest_bytes)
        runtime = _runtime()
        _validate_build_selection(plan, paths, snapshots, runtime)
        if not has_writes(plan):
            return transaction

        mutations = _selected_mutations(plan, paths, snapshots, runtime)
        for path, _replace_existing in mutations:
            current_path = path
            try:
                owned_dirs.extend(fs.ensure_parent_dirs(path.parent, root))
            except Exception as error:
                owned_dirs.extend(getattr(error, "created_directories", ()))
                raise
        # A path can be reached through multiple planned outputs. Keep one
        # identity record for each directory we created.
        owned_dirs[:] = list({str(item.path): item for item in owned_dirs}.values())

        if plan["action"] == "replace":
            originals: list[str] = ["exe"]
            if snapshots["timer"] is not None:
                originals.append("timer")
            originals.extend(kind for kind in ("hero", "icon") if kind in plan["assets"] and snapshots[kind] is not None)
            for kind in originals:
                current_path = paths[kind]
                expected = snapshots[kind]
                assert expected is not None
                backup = _backup_name(current_path, plan["transaction_id"])
                backups.append(fs.backup_file(current_path, expected, backup, root))

        contents: list[tuple[str, bytes]] = []
        if plan["action"] in {"create", "replace"}:
            contents.append(("exe", _executable_contents(runtime, plan)))
            if not runtime.frozen:
                contents.append(("timer", _timer_source_for(runtime, plan)))
            contents.extend((kind, data) for kind, data in _decode_plan_assets(plan).items())
        if plan["manifest_action"] == "create":
            contents.append(("manifest", render_manifest(plan["appid"], plan["name"], plan["installdir"])))
        for kind, data in contents:
            current_path = paths[kind]
            if snapshots[kind] is not None and not (
                plan["action"] == "replace" and kind != "manifest"
            ):
                raise FileExistsError(f"output path already exists: {current_path}")
            snapshot = fs.create_file(current_path, data, root)
            owned_files.append(_OwnedFile(current_path, snapshot))
        return transaction
    except Exception as error:
        rollback = transaction.abort()
        if getattr(error, "rollback_leftover", None):
            rollback = CleanupReport(
                removed=rollback.removed,
                preserved=rollback.preserved,
                leftovers=tuple(dict.fromkeys((*rollback.leftovers, str(error.rollback_leftover)))),
                backups=rollback.backups,
            )
        denied = _is_access_denied(error) if isinstance(error, OSError) else False
        denied_paths = (str(getattr(error, "filename", None) or current_path),) if denied else ()
        failure = PreparationError(
            str(error), permission_denied=denied and rollback.complete,
            denied_paths=denied_paths, rollback=rollback,
        )
        failure.cause = error
        raise failure from error
