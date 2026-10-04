"""Bounded Windows UAC transport for Steam-library file transactions.

This module is deliberately standalone.  It imports only the standard library
and the sibling ``_orbfarmer_files`` module.  The elevated entry point must run
before the application package is imported.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


_MAX_MESSAGE = 12 * 1024 * 1024
_PIPE_PREFIX = r"\\.\pipe\OrbfarmerUac-"
_PIPE_RE = re.compile(r"^\\\\\.\\pipe\\OrbfarmerUac-([0-9a-f]{32})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONNECT_TIMEOUT_SECONDS = 60.0
_CLIENT_WAIT_SECONDS = 60.0
_HELPER_CLOSE_SECONDS = 8.0
_EXCHANGE_TIMEOUT_SECONDS = 60.0
_CLEANUP_RETRIES = 3
_CLEANUP_DELAYS = (0.10, 0.35)
_ENV_LOCK = threading.RLock()

# Win32 errors used at the process and pipe boundary.
_ERROR_CANCELLED = 1223
_ERROR_IO_PENDING = 997
_ERROR_PIPE_CONNECTED = 535
_ERROR_FILE_NOT_FOUND = 2
_ERROR_SEM_TIMEOUT = 121
_ERROR_BROKEN_PIPE = 109
_ERROR_PIPE_NOT_CONNECTED = 233
_ERROR_NO_DATA = 232
_ERROR_MORE_DATA = 234


class ElevationError(RuntimeError):
    """The elevated helper could not complete its authenticated protocol."""

    def __init__(self, message: str, *, report: Any | None = None):
        super().__init__(message)
        self.report = report


class ProtocolError(ElevationError):
    """A helper or parent sent an invalid protocol message."""


class PeerRejected(ElevationError):
    """The process at the other end of a pipe did not pass identity checks."""


class _PipeDisconnected(ElevationError):
    """The other endpoint closed the pipe."""


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


def _files_module():
    """Load the standalone filesystem core without importing the app package."""
    try:
        import _orbfarmer_files  # type: ignore[import-not-found]
    except ModuleNotFoundError as error:
        if error.name != "_orbfarmer_files":
            raise
        # ``-I -S`` excludes the current directory.  Add only this module's
        # trusted, absolute directory so the paired core can be imported.
        module_dir = Path(__file__).resolve(strict=True).parent
        sys.path.insert(0, str(module_dir))
        import _orbfarmer_files  # type: ignore[import-not-found,no-redef]
    return _orbfarmer_files


def _json_bytes(value: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ProtocolError("Protocol message is not valid JSON data") from error
    if not payload or len(payload) > _MAX_MESSAGE:
        raise ProtocolError("Protocol message exceeds the size limit")
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Protocol message contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> Any:
    raise ProtocolError("Protocol message contains a non-finite number")


def _parse_json(payload: bytes) -> dict[str, Any]:
    if not payload or len(payload) > _MAX_MESSAGE:
        raise ProtocolError("Protocol message has an invalid size")
    try:
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except ProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ProtocolError("Protocol message is not valid JSON") from error
    if not isinstance(value, dict):
        raise ProtocolError("Protocol message must be a JSON object")
    return value


class _PipeConnection:
    """A bounded length-prefixed JSON stream over a Windows named pipe."""

    def __init__(self, handle: int):
        self.handle = handle
        self.closed = False

    def _read_exact(self, size: int, deadline: float | None) -> bytes:
        if type(size) is not int or size < 0 or size > _MAX_MESSAGE:
            raise ProtocolError("Invalid protocol frame length")
        if os.name != "nt":
            raise ElevationError("Named-pipe transport requires Windows")
        kernel32 = _kernel32()
        read_file = kernel32.ReadFile
        read_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        read_file.restype = ctypes.c_int
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            amount = min(remaining, 64 * 1024)
            buffer = ctypes.create_string_buffer(amount)
            received = ctypes.c_uint32()
            event = kernel32.CreateEventW(None, 1, 0, None)
            if not event:
                code = ctypes.get_last_error()
                raise _win_error(code, "Could not create named-pipe read event")
            overlapped = _OVERLAPPED()
            overlapped.hEvent = event
            try:
                ok = read_file(self.handle, buffer, amount, ctypes.byref(received), ctypes.byref(overlapped))
                if not ok:
                    code = ctypes.get_last_error()
                    if code != _ERROR_IO_PENDING:
                        if code in (_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED, _ERROR_NO_DATA):
                            raise _PipeDisconnected("Named-pipe peer disconnected")
                        if code == _ERROR_MORE_DATA:
                            raise ProtocolError("Named pipe returned an unexpected partial message")
                        raise OSError(code, "Could not read the named pipe")
                    _wait_for_io(self.handle, event, overlapped, deadline, "named-pipe read")
                    get_result = kernel32.GetOverlappedResult
                    get_result.argtypes = [ctypes.c_void_p, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
                    get_result.restype = ctypes.c_int
                    if not get_result(self.handle, ctypes.byref(overlapped), ctypes.byref(received), 0):
                        code = ctypes.get_last_error()
                        if code in (_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED, _ERROR_NO_DATA):
                            raise _PipeDisconnected("Named-pipe peer disconnected")
                        raise _win_error(code, "Could not complete named-pipe read")
            finally:
                kernel32.CloseHandle(event)
            if received.value == 0:
                raise _PipeDisconnected("Named-pipe peer disconnected")
            chunks.append(buffer.raw[: received.value])
            remaining -= received.value
            if deadline is None:
                deadline = time.monotonic() + _EXCHANGE_TIMEOUT_SECONDS
        return b"".join(chunks)

    def _write_all(self, payload: bytes, deadline: float | None) -> None:
        if os.name != "nt":
            raise ElevationError("Named-pipe transport requires Windows")
        kernel32 = _kernel32()
        write_file = kernel32.WriteFile
        write_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        write_file.restype = ctypes.c_int
        offset = 0
        while offset < len(payload):
            amount = min(len(payload) - offset, 64 * 1024)
            buffer = ctypes.create_string_buffer(payload[offset : offset + amount])
            written = ctypes.c_uint32()
            event = kernel32.CreateEventW(None, 1, 0, None)
            if not event:
                code = ctypes.get_last_error()
                raise _win_error(code, "Could not create named-pipe write event")
            overlapped = _OVERLAPPED()
            overlapped.hEvent = event
            try:
                ok = write_file(self.handle, buffer, amount, ctypes.byref(written), ctypes.byref(overlapped))
                if not ok:
                    code = ctypes.get_last_error()
                    if code != _ERROR_IO_PENDING:
                        if code in (_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED, _ERROR_NO_DATA):
                            raise _PipeDisconnected("Named-pipe peer disconnected")
                        raise OSError(code, "Could not write the named pipe")
                    _wait_for_io(self.handle, event, overlapped, deadline, "named-pipe write")
                    get_result = kernel32.GetOverlappedResult
                    get_result.argtypes = [ctypes.c_void_p, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
                    get_result.restype = ctypes.c_int
                    if not get_result(self.handle, ctypes.byref(overlapped), ctypes.byref(written), 0):
                        code = ctypes.get_last_error()
                        if code in (_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED, _ERROR_NO_DATA):
                            raise _PipeDisconnected("Named-pipe peer disconnected")
                        raise _win_error(code, "Could not complete named-pipe write")
            finally:
                kernel32.CloseHandle(event)
            if written.value == 0:
                raise _PipeDisconnected("Named-pipe peer disconnected")
            offset += written.value

    def send(self, value: dict[str, Any]) -> None:
        payload = _json_bytes(value)
        self._write_all(struct.pack("!I", len(payload)) + payload, time.monotonic() + _EXCHANGE_TIMEOUT_SECONDS)

    def receive(self, *, idle: bool = False) -> dict[str, Any]:
        deadline = None if idle else time.monotonic() + _EXCHANGE_TIMEOUT_SECONDS
        header = self._read_exact(4, deadline)
        (size,) = struct.unpack("!I", header)
        if size == 0 or size > _MAX_MESSAGE:
            raise ProtocolError("Protocol frame exceeds the size limit")
        # Only waiting for the next idle command is unbounded. Its payload
        # still has a deadline once the peer starts the frame.
        if deadline is None:
            deadline = time.monotonic() + _EXCHANGE_TIMEOUT_SECONDS
        return _parse_json(self._read_exact(size, deadline))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if os.name == "nt" and self.handle:
            _kernel32().CloseHandle(self.handle)
        self.handle = 0


def _win_error(code: int, message: str) -> OSError:
    return OSError(code, message)


def _cancel_and_drain(handle: int, overlapped: _OVERLAPPED) -> None:
    """Finish cancellation before releasing the operation's buffers or event."""
    kernel32 = _kernel32()
    kernel32.CancelIoEx(handle, ctypes.byref(overlapped))
    transferred = ctypes.c_uint32()
    function = kernel32.GetOverlappedResult
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    function.restype = ctypes.c_int
    function(handle, ctypes.byref(overlapped), ctypes.byref(transferred), 1)


def _wait_for_io(handle: int, event: int, overlapped: _OVERLAPPED, deadline: float | None, label: str) -> None:
    wait_ms = 0xFFFFFFFF if deadline is None else max(0, min(0xFFFFFFFE, int((deadline - time.monotonic()) * 1000)))
    try:
        result = _kernel32().WaitForSingleObject(event, wait_ms)
        if result == 0:
            return
        if result == 0x00000102:
            raise TimeoutError(f"Timed out waiting for {label}")
        raise _win_error(ctypes.get_last_error(), f"Could not wait for {label}")
    except BaseException:
        _cancel_and_drain(handle, overlapped)
        raise


def _kernel32():
    if os.name != "nt":
        raise ElevationError("Native UAC transport requires Windows")
    if not hasattr(_kernel32, "_dll"):
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        dll.CloseHandle.argtypes = [ctypes.c_void_p]
        dll.CloseHandle.restype = ctypes.c_int
        dll.LocalFree.argtypes = [ctypes.c_void_p]
        dll.LocalFree.restype = ctypes.c_void_p
        dll.GetCurrentProcess.argtypes = []
        dll.GetCurrentProcess.restype = ctypes.c_void_p
        dll.GetCurrentThread.argtypes = []
        dll.GetCurrentThread.restype = ctypes.c_void_p
        dll.GetCurrentProcessId.argtypes = []
        dll.GetCurrentProcessId.restype = ctypes.c_uint32
        dll.GetProcessId.argtypes = [ctypes.c_void_p]
        dll.GetProcessId.restype = ctypes.c_uint32
        dll.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        dll.OpenProcess.restype = ctypes.c_void_p
        dll.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        dll.CreateEventW.restype = ctypes.c_void_p
        dll.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        dll.WaitForSingleObject.restype = ctypes.c_uint32
        dll.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        dll.TerminateProcess.restype = ctypes.c_int
        dll.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        dll.CancelIoEx.restype = ctypes.c_int
        dll.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        dll.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        dll.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        dll.GetSystemDirectoryW.restype = ctypes.c_uint32
        _kernel32._dll = dll  # type: ignore[attr-defined]
    return _kernel32._dll  # type: ignore[attr-defined]


def _advapi32():
    if os.name != "nt":
        raise ElevationError("Native UAC transport requires Windows")
    if not hasattr(_advapi32, "_dll"):
        dll = ctypes.WinDLL("advapi32", use_last_error=True)
        dll.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        dll.OpenProcessToken.restype = ctypes.c_int
        dll.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.GetTokenInformation.restype = ctypes.c_int
        dll.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        dll.ConvertSidToStringSidW.restype = ctypes.c_int
        dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
        dll.CreateWellKnownSid.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.CreateWellKnownSid.restype = ctypes.c_int
        dll.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        dll.EqualSid.restype = ctypes.c_int
        dll.ImpersonateNamedPipeClient.argtypes = [ctypes.c_void_p]
        dll.ImpersonateNamedPipeClient.restype = ctypes.c_int
        dll.OpenThreadToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        dll.OpenThreadToken.restype = ctypes.c_int
        dll.RevertToSelf.argtypes = []
        dll.RevertToSelf.restype = ctypes.c_int
        _advapi32._dll = dll  # type: ignore[attr-defined]
    return _advapi32._dll  # type: ignore[attr-defined]


def _process_id() -> int:
    return int(_kernel32().GetCurrentProcessId())


def _filetime_value(value: Any) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _process_created_from_handle(handle: int) -> int:
    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    creation = FILETIME()
    exit_time = FILETIME()
    kernel_time = FILETIME()
    user_time = FILETIME()
    function = _kernel32().GetProcessTimes
    function.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    function.restype = ctypes.c_int
    if not function(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not read process creation time")
    return _filetime_value(creation)


def _process_created(pid: int) -> int:
    kernel32 = _kernel32()
    function = kernel32.OpenProcess
    function.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    function.restype = ctypes.c_void_p
    handle = function(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not open peer process")
    try:
        return _process_created_from_handle(handle)
    finally:
        kernel32.CloseHandle(handle)


def _process_image_from_handle(handle: int) -> str:
    kernel32 = _kernel32()
    function = kernel32.QueryFullProcessImageNameW
    function.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    function.restype = ctypes.c_int
    size = ctypes.c_uint32(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not function(handle, 0, buffer, ctypes.byref(size)):
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not read peer process image")
    return buffer.value


def _process_token(handle: int) -> int:
    token = ctypes.c_void_p()
    function = _advapi32().OpenProcessToken
    function.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    function.restype = ctypes.c_int
    if not function(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not inspect peer token")
    return int(token.value)


def _current_token() -> int:
    return _process_token(_kernel32().GetCurrentProcess())


def _token_user_sid(token: int) -> str:
    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    needed = ctypes.c_uint32()
    advapi32 = _advapi32()
    function = advapi32.GetTokenInformation
    function.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    function.restype = ctypes.c_int
    function(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
    if not needed.value:
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not inspect peer user SID")
    buffer = ctypes.create_string_buffer(needed.value)
    if not function(token, 1, buffer, needed.value, ctypes.byref(needed)):
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not inspect peer user SID")
    user = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents
    sid_text = ctypes.c_wchar_p()
    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert.restype = ctypes.c_int
    if not convert(user.User.Sid, ctypes.byref(sid_text)):
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not format peer user SID")
    try:
        return sid_text.value or ""
    finally:
        _kernel32().LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))


def _current_user_sid() -> str:
    token = _current_token()
    try:
        return _token_user_sid(token)
    finally:
        _kernel32().CloseHandle(token)


def _peer_token_info(pid: int) -> tuple[str, bool, bool, str]:
    kernel32 = _kernel32()
    process = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not open named-pipe peer")
    token = None
    try:
        image = _process_image_from_handle(process)
        token = _process_token(process)
        sid = _token_user_sid(token)
        elevated, administrator = _token_security_info(token)
        return sid, elevated, administrator, image
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)


def _token_security_info(token: int) -> tuple[bool, bool]:
    """Query elevation and enabled Administrators membership with TOKEN_QUERY."""
    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]

    class TOKEN_GROUPS(ctypes.Structure):
        _fields_ = [("GroupCount", ctypes.c_uint32), ("Groups", SID_AND_ATTRIBUTES * 1)]

    advapi32 = _advapi32()
    elevated = ctypes.c_uint32()
    needed = ctypes.c_uint32()
    if not advapi32.GetTokenInformation(token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(needed)):
        raise _win_error(ctypes.get_last_error(), "Could not inspect peer elevation state")
    advapi32.GetTokenInformation(token, 2, None, 0, ctypes.byref(needed))  # TokenGroups
    if needed.value < TOKEN_GROUPS.Groups.offset:
        raise _win_error(ctypes.get_last_error(), "Could not inspect peer token groups")
    groups_buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetTokenInformation(token, 2, groups_buffer, needed.value, ctypes.byref(needed)):
        raise _win_error(ctypes.get_last_error(), "Could not inspect peer token groups")
    count = ctypes.cast(groups_buffer, ctypes.POINTER(ctypes.c_uint32)).contents.value
    if count > (len(groups_buffer) - TOKEN_GROUPS.Groups.offset) // ctypes.sizeof(SID_AND_ATTRIBUTES):
        raise PeerRejected("Peer token groups are malformed")
    groups = (SID_AND_ATTRIBUTES * count).from_buffer(groups_buffer, TOKEN_GROUPS.Groups.offset)
    admin_sid = ctypes.create_string_buffer(68)
    sid_size = ctypes.c_uint32(len(admin_sid))
    if not advapi32.CreateWellKnownSid(26, None, admin_sid, ctypes.byref(sid_size)):
        raise _win_error(ctypes.get_last_error(), "Could not create Administrators SID")
    administrator = any(
        group.Attributes & 0x00000004 and not group.Attributes & 0x00000010
        and advapi32.EqualSid(group.Sid, admin_sid)
        for group in groups
    )
    return bool(elevated.value), bool(administrator)


def _pipe_client_token_info(pipe_handle: int) -> tuple[str, bool, bool]:
    """Read the authenticated hello's identification token without using it."""
    advapi32 = _advapi32()
    if not advapi32.ImpersonateNamedPipeClient(pipe_handle):
        raise _win_error(ctypes.get_last_error(), "Could not inspect the named-pipe client token")
    token = ctypes.c_void_p()
    try:
        if not advapi32.OpenThreadToken(_kernel32().GetCurrentThread(), 0x0008, 1, ctypes.byref(token)):
            raise _win_error(ctypes.get_last_error(), "Could not query the named-pipe identification token")
        sid = _token_user_sid(int(token.value))
        elevated, administrator = _token_security_info(int(token.value))
        return sid, elevated, administrator
    finally:
        if token.value:
            _kernel32().CloseHandle(token)
        if not advapi32.RevertToSelf():
            raise _win_error(ctypes.get_last_error(), "Could not revert the named-pipe identification context")


def _process_image(pid: int) -> str:
    kernel32 = _kernel32()
    process = kernel32.OpenProcess(0x1000, 0, pid)
    if not process:
        raise _win_error(ctypes.get_last_error(), "Could not inspect named-pipe process image")
    try:
        return _process_image_from_handle(process)
    finally:
        kernel32.CloseHandle(process)


def _normalize_image(path: str) -> str:
    value = path.replace("/", "\\")
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def _helper_executable() -> Path:
    """Use the real source interpreter rather than a Windows venv launcher."""
    executable = sys.executable if getattr(sys, "frozen", False) else getattr(sys, "_base_executable", sys.executable)
    return Path(executable).resolve(strict=True)


def _is_descendant_or_same(child_pid: int, ancestor_pid: int) -> bool:
    """Check the current process-parent chain, with a strict depth bound."""
    if child_pid == ancestor_pid:
        return True
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    invalid = ctypes.c_void_p(-1).value
    if snapshot in (None, invalid):
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not inspect helper process ancestry")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        parents: dict[int, int] = {}
        first = kernel32.Process32FirstW
        first.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        first.restype = ctypes.c_int
        next_process = kernel32.Process32NextW
        next_process.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        next_process.restype = ctypes.c_int
        if first(snapshot, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not next_process(snapshot, ctypes.byref(entry)):
                    break
        current = child_pid
        for _ in range(64):
            parent = parents.get(current)
            if parent is None or parent == 0 or parent == current:
                return False
            if parent == ancestor_pid:
                return True
            current = parent
        return False
    finally:
        kernel32.CloseHandle(snapshot)


def _named_pipe_server_pid(handle: int) -> int:
    pid = ctypes.c_uint32()
    function = _kernel32().GetNamedPipeServerProcessId
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    function.restype = ctypes.c_int
    if not function(handle, ctypes.byref(pid)):
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not identify named-pipe server")
    return int(pid.value)


def _named_pipe_client_pid(handle: int) -> int:
    pid = ctypes.c_uint32()
    function = _kernel32().GetNamedPipeClientProcessId
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    function.restype = ctypes.c_int
    if not function(handle, ctypes.byref(pid)):
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not identify named-pipe client")
    return int(pid.value)


def _make_security_descriptor(user_sid: str) -> tuple[Any, int]:
    if not re.fullmatch(r"S-1-(?:[0-9]+-)*[0-9]+", user_sid, flags=re.IGNORECASE):
        raise PeerRejected("Initiating user SID is malformed")
    # Grant pipe data read/write, read attributes, read-control and synchronization.
    # Do not grant FILE_APPEND_DATA (the named-pipe create-instance right),
    # DELETE, WRITE_DAC or WRITE_OWNER.  The ACL is protected
    # against inherited ACEs and names the initiating SID, Administrators and
    # SYSTEM explicitly.
    rights = "0x00120083"
    sddl = f"D:P(A;;{rights};;;{user_sid})(A;;{rights};;;BA)(A;;{rights};;;SY)"
    descriptor = ctypes.c_void_p()
    size = ctypes.c_uint32()
    convert = _advapi32().ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)]
    convert.restype = ctypes.c_int
    if not convert(sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)):
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not create named-pipe access control")
    return descriptor, int(size.value)


def _create_pipe_server(name: str, user_sid: str) -> int:
    if not _PIPE_RE.fullmatch(name):
        raise ValueError("Named-pipe name is invalid")
    descriptor, _ = _make_security_descriptor(user_sid)

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", ctypes.c_uint32),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", ctypes.c_int),
        ]

    security = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, 0)
    kernel32 = _kernel32()
    function = kernel32.CreateNamedPipeW
    function.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
    ]
    function.restype = ctypes.c_void_p
    # PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED
    open_mode = 0x00000003 | 0x00080000 | 0x40000000
    # PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS
    pipe_mode = 0x00000000 | 0x00000000 | 0x00000000 | 0x00000008
    handle = function(name, open_mode, pipe_mode, 1, 64 * 1024, 64 * 1024, 0, ctypes.byref(security))
    _kernel32().LocalFree(descriptor)
    if not handle or handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not create the local named pipe")
    return int(handle)


def _connect_server(handle: int, process_handle: int, timeout: float) -> None:
    kernel32 = _kernel32()
    event = kernel32.CreateEventW(None, 1, 0, None)
    if not event:
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not create named-pipe event")
    overlapped = _OVERLAPPED()
    overlapped.hEvent = event
    connect = kernel32.ConnectNamedPipe
    connect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_OVERLAPPED)]
    connect.restype = ctypes.c_int
    connected = bool(connect(handle, ctypes.byref(overlapped)))
    if not connected:
        code = ctypes.get_last_error()
        if code == _ERROR_PIPE_CONNECTED:
            connected = True
        elif code != _ERROR_IO_PENDING:
            kernel32.CloseHandle(event)
            raise _win_error(code, "Could not accept named-pipe client")
    if connected:
        kernel32.CloseHandle(event)
        return

    deadline = time.monotonic() + timeout
    wait = kernel32.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait.restype = ctypes.c_uint32
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _cancel_and_drain(handle, overlapped)
            kernel32.CloseHandle(event)
            raise TimeoutError("Elevated helper did not connect before the startup timeout")
        result = wait(event, min(100, max(1, int(remaining * 1000))))
        if result == 0:
            break
        if result not in (0x00000102,):  # WAIT_TIMEOUT
            code = ctypes.get_last_error()
            _cancel_and_drain(handle, overlapped)
            kernel32.CloseHandle(event)
            raise _win_error(code, "Could not wait for named-pipe client")
        if process_handle:
            process_state = wait(process_handle, 0)
            if process_state == 0x00000102:  # WAIT_TIMEOUT: process still runs
                continue
            if process_state == 0:  # WAIT_OBJECT_0: process exited
                _cancel_and_drain(handle, overlapped)
                kernel32.CloseHandle(event)
                raise ElevationError("Elevated helper exited before it connected")
            code = ctypes.get_last_error()
            _cancel_and_drain(handle, overlapped)
            kernel32.CloseHandle(event)
            raise _win_error(code, "Could not inspect elevated helper startup")
    transferred = ctypes.c_uint32()
    get_result = kernel32.GetOverlappedResult
    get_result.argtypes = [ctypes.c_void_p, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    get_result.restype = ctypes.c_int
    if not get_result(handle, ctypes.byref(overlapped), ctypes.byref(transferred), 0):
        code = ctypes.get_last_error()
        kernel32.CloseHandle(event)
        raise _win_error(code, "Could not complete named-pipe connection")
    kernel32.CloseHandle(event)


def _connect_client(name: str, timeout: float) -> _PipeConnection:
    if not _PIPE_RE.fullmatch(name):
        raise ValueError("Named-pipe name is invalid")
    kernel32 = _kernel32()
    wait_named_pipe = kernel32.WaitNamedPipeW
    wait_named_pipe.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    wait_named_pipe.restype = ctypes.c_int
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Parent named pipe did not become available")
        wait_named_pipe(name, min(1000, max(1, int(remaining * 1000))))
        # Request only pipe data read/write, read attributes, read-control and
        # synchronization.  The explicit
        # identification SQOS flags prevent an elevated client token from being
        # impersonated by the normal-token server.
        handle = create_file(
            name,
            0x00120083,  # read/write data, read attributes, read-control and sync
            0,
            None,
            3,  # OPEN_EXISTING
            0x40000000 | 0x00100000 | 0x00010000,  # OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle and handle != invalid:
            return _PipeConnection(int(handle))
        code = ctypes.get_last_error()
        if code in (_ERROR_FILE_NOT_FOUND, _ERROR_SEM_TIMEOUT):
            continue
        raise _win_error(code, "Could not connect to parent named pipe")


def _pipe_name() -> str:
    return _PIPE_PREFIX + os.urandom(16).hex()


def _close_handle(handle: int | None) -> None:
    if handle and os.name == "nt":
        _kernel32().CloseHandle(handle)


def _safe_cwd() -> str:
    kernel32 = _kernel32()
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    function = kernel32.GetSystemDirectoryW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    length = function(buffer, size)
    if not length or length >= size:
        code = ctypes.get_last_error()
        raise _win_error(code, "Could not determine a safe helper working directory")
    return buffer.value


class _ShellExecuteInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("fMask", ctypes.c_uint32),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_uint32),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def _shell_execute_helper(pipe_name: str, parent_pid: int, parent_created: int, plan_digest: str) -> int | None:
    if os.name != "nt":
        raise ElevationError("Native UAC transport requires Windows")
    executable = _helper_executable()
    if not executable.is_absolute() or not executable.is_file():
        raise ElevationError("Could not locate the absolute Python or application executable")
    args = [
        "--filesystem-helper",
        "--pipe",
        pipe_name,
        "--parent-pid",
        str(parent_pid),
        "--parent-created",
        str(parent_created),
        "--plan-sha256",
        plan_digest,
    ]
    if getattr(sys, "frozen", False):
        parameters = subprocess.list2cmdline(args)
    else:
        helper_script = Path(__file__).resolve(strict=True)
        if not helper_script.is_absolute():
            raise ElevationError("Helper script path must be absolute")
        parameters = subprocess.list2cmdline(["-I", "-S", str(helper_script), *args])

    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = parameters
    info.lpDirectory = _safe_cwd()
    info.nShow = 0  # SW_HIDE: only Windows owns the UAC prompt UI.
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell_execute = shell32.ShellExecuteExW
    shell_execute.argtypes = [ctypes.POINTER(_ShellExecuteInfoW)]
    shell_execute.restype = ctypes.c_int
    old_value = os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
    set_reset = bool(getattr(sys, "frozen", False))
    with _ENV_LOCK:
        try:
            if set_reset:
                os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            if not shell_execute(ctypes.byref(info)):
                code = ctypes.get_last_error()
                if not code and info.hInstApp:
                    code = int(info.hInstApp)
                if code == _ERROR_CANCELLED:
                    return None
                raise _win_error(code, "Windows could not start the elevated helper")
        finally:
            if set_reset:
                if old_value is None:
                    os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
                else:
                    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = old_value
    if not info.hProcess:
        raise ElevationError("Windows started the helper without a process handle")
    return int(info.hProcess)


def _verify_server(pipe_handle: int, expected_pid: int, expected_created: int) -> None:
    pid = _named_pipe_server_pid(pipe_handle)
    if pid != expected_pid:
        raise PeerRejected("Named-pipe server process does not match the initiating process")
    if _process_created(pid) != expected_created:
        raise PeerRejected("Named-pipe server creation time does not match")
    image = _process_image(pid)
    if _normalize_image(image) != _normalize_image(str(_helper_executable())):
        raise PeerRejected("Named-pipe server image does not match the application interpreter")


def _verify_client(pipe_handle: int, launcher_handle: int) -> int:
    pid = _named_pipe_client_pid(pipe_handle)
    _, elevated, administrator = _pipe_client_token_info(pipe_handle)
    if not elevated or not administrator:
        raise PeerRejected("Named-pipe client does not have an elevated administrator token")
    if _normalize_image(_process_image(pid)) != _normalize_image(str(_helper_executable())):
        raise PeerRejected("Named-pipe client image does not match the helper executable")
    launcher_pid = int(_kernel32().GetProcessId(launcher_handle))
    if not launcher_pid or not _is_descendant_or_same(pid, launcher_pid):
        raise PeerRejected("Named-pipe client is not a descendant of the launched helper")
    wait = _kernel32().WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait.restype = ctypes.c_uint32
    if wait(launcher_handle, 0) != 0x00000102:  # WAIT_TIMEOUT means still live
        raise PeerRejected("Launched helper exited before peer verification")
    return pid


def _valid_report(report: Any) -> Any:
    files = _files_module()
    if isinstance(report, files.CleanupReport):
        return report
    if isinstance(report, dict):
        return files.CleanupReport.from_dict(report)
    raise ProtocolError("Helper response does not contain a cleanup report")


def _report_dict(report: Any) -> dict[str, Any]:
    if not hasattr(report, "to_dict"):
        raise ProtocolError("Filesystem operation did not return a cleanup report")
    result = report.to_dict()
    if not isinstance(result, dict):
        raise ProtocolError("Filesystem cleanup report is invalid")
    return result


def _error_message(message: dict[str, Any]) -> None:
    if set(message) != {"type", "code", "message", "report"} or message.get("type") != "error":
        raise ProtocolError("Helper returned an invalid error message")
    code = message.get("code")
    detail = message.get("message")
    if not isinstance(code, str) or not isinstance(detail, str) or len(detail) > 1000:
        raise ProtocolError("Helper returned an invalid error message")
    report = None if message["report"] is None else _valid_report(message["report"])
    raise ElevationError(f"Elevated helper failed ({code}): {detail}", report=report)


def _expected_target_path(plan: dict[str, Any]) -> Path:
    relative = plan["executable"].replace("/", os.sep)
    return Path(plan["library_root"], "steamapps", "common", plan["installdir"], relative)


def _same_path(left: str | Path, right: str | Path) -> bool:
    def normalize(value: str | Path) -> str:
        rendered = os.path.abspath(os.path.normpath(os.fspath(value))).replace("/", "\\")
        if rendered.startswith("\\\\?\\"):
            rendered = rendered[4:]
        return os.path.normcase(rendered)

    return normalize(left) == normalize(right)


class PreparedSession:
    """One prepared file transaction, local or owned by an elevated helper."""

    def __init__(
        self,
        target_path: Path,
        *,
        elevated: bool,
        transaction: Any | None = None,
        connection: _PipeConnection | None = None,
        helper_process: int | None = None,
    ):
        self.target_path = Path(target_path)
        self.elevated = bool(elevated)
        self._transaction = transaction
        self._connection = connection
        self._helper_process = helper_process
        self._committed = False
        self._closed = False
        self._close_report = None

    def commit(self) -> Any:
        if self._closed:
            raise ElevationError("Prepared session is already closed")
        if self._committed:
            raise ElevationError("Prepared session is already committed")
        if self.elevated:
            assert self._connection is not None
            self._connection.send({"type": "commit"})
            message = self._connection.receive()
            if message.get("type") == "error":
                _error_message(message)
            if set(message) != {"type", "report"} or message.get("type") != "committed":
                raise ProtocolError("Helper returned an invalid commit response")
            report = _valid_report(message["report"])
        else:
            report = self._transaction.commit()
        self._committed = True
        return report

    def close(self) -> Any:
        if self._closed:
            return self._close_report
        self._closed = True
        try:
            if self.elevated:
                assert self._connection is not None
                self._connection.send({"type": "cleanup" if self._committed else "abort"})
                message = self._connection.receive()
                if message.get("type") == "error":
                    _error_message(message)
                if set(message) != {"type", "report"} or message.get("type") != "closed":
                    raise ProtocolError("Helper returned an invalid close response")
                self._close_report = _valid_report(message["report"])
            elif self._committed:
                self._close_report = _retry_cleanup(self._transaction.cleanup)
            else:
                self._close_report = _retry_cleanup(self._transaction.abort)
            return self._close_report
        finally:
            self._shutdown_helper()

    def _shutdown_helper(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._helper_process is not None:
            handle = self._helper_process
            self._helper_process = None
            if os.name == "nt":
                kernel32 = _kernel32()
                wait = kernel32.WaitForSingleObject
                wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                wait.restype = ctypes.c_uint32
                if wait(handle, int(_HELPER_CLOSE_SECONDS * 1000)) == 0x00000102:
                    kernel32.TerminateProcess(handle, 1)
                    wait(handle, 2000)
                kernel32.CloseHandle(handle)

    def __enter__(self) -> "PreparedSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _retry_cleanup(operation: Callable[[], Any]) -> Any:
    result = None
    for attempt in range(_CLEANUP_RETRIES):
        result = operation()
        if getattr(result, "complete", False):
            return result
        if attempt < len(_CLEANUP_DELAYS):
            time.sleep(_CLEANUP_DELAYS[attempt])
    return result


def _launch_elevated(plan: dict[str, Any], plan_payload: bytes, plan_digest: str) -> PreparedSession | None:
    if os.name != "nt":
        raise ElevationError("Native UAC elevation is available only on Windows")
    pipe_name = _pipe_name()
    user_sid = _current_user_sid()
    pipe_handle = _create_pipe_server(pipe_name, user_sid)
    helper_process: int | None = None
    connection: _PipeConnection | None = None
    try:
        parent_pid = _process_id()
        parent_created = _process_created(parent_pid)
        helper_process = _shell_execute_helper(pipe_name, parent_pid, parent_created, plan_digest)
        if helper_process is None:
            return None
        _connect_server(pipe_handle, helper_process, _CONNECT_TIMEOUT_SECONDS)
        connection = _PipeConnection(pipe_handle)
        pipe_handle = 0
        hello = connection.receive()
        if set(hello) != {"type", "version"} or hello.get("type") != "hello" or type(hello.get("version")) is not int or hello["version"] != 1:
            raise ProtocolError("Elevated helper returned an invalid hello")
        _verify_client(connection.handle, helper_process)
        connection.send(
            {
                "type": "plan",
                "version": 1,
                "parent_pid": parent_pid,
                "parent_created": parent_created,
                "plan_sha256": plan_digest,
                "plan": plan,
            }
        )
        response = connection.receive()
        if response.get("type") == "error":
            _error_message(response)
        if set(response) != {"type", "target_path"} or response.get("type") != "prepared":
            raise ProtocolError("Elevated helper returned an invalid prepared response")
        target_path = response.get("target_path")
        expected_path = _expected_target_path(plan)
        if (
            not isinstance(target_path, str)
            or not Path(target_path).is_absolute()
            or not _same_path(target_path, expected_path)
        ):
            raise PeerRejected("Elevated helper prepared an unexpected target path")
        session = PreparedSession(
            Path(target_path),
            elevated=True,
            connection=connection,
            helper_process=helper_process,
        )
        connection = None
        helper_process = None
        return session
    finally:
        if connection is not None:
            connection.close()
        if pipe_handle:
            _close_handle(pipe_handle)
        if helper_process is not None:
            if os.name == "nt":
                _kernel32().CloseHandle(helper_process)


def prepare_session(
    plan: dict[str, Any],
    *,
    on_elevation_required: Callable[[tuple[str, ...]], None],
) -> PreparedSession | None:
    """Probe selected writes, prepare locally, and elevate at most once.

    A ``None`` result means only that Windows returned UAC cancellation error
    1223.  Every other setup or helper failure raises a descriptive exception.
    """
    files = _files_module()
    plan_payload = files.encode_plan(plan)
    if not isinstance(plan_payload, bytes):
        raise TypeError("Filesystem core encode_plan() must return bytes")
    plan = files.decode_plan(plan_payload)
    digest = hashlib.sha256(plan_payload).hexdigest()
    expected_path = _expected_target_path(plan)

    def elevate(denied_paths: tuple[str, ...]) -> PreparedSession | None:
        on_elevation_required(denied_paths)
        return _launch_elevated(plan, plan_payload, digest)

    has_writes = files.has_writes(plan)
    if has_writes:
        probe = files.probe_permissions(plan)
        denied = tuple(probe.denied_paths)
        if probe.needs_elevation:
            return elevate(denied)

    try:
        transaction = files.prepare(plan)
    except files.PreparationError as error:
        if (
            has_writes
            and error.permission_denied
            and getattr(error.rollback, "complete", False)
        ):
            return elevate(tuple(error.denied_paths))
        raise

    target_path = Path(transaction.target_path)
    if not _same_path(target_path, expected_path):
        _retry_cleanup(transaction.abort)
        raise ElevationError("Filesystem core prepared an unexpected target path")
    return PreparedSession(target_path, elevated=False, transaction=transaction)


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ProtocolError(f"{label} contains missing or unknown fields")


def _read_helper_args(argv: list[str]) -> tuple[str, int, int, str]:
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise ValueError("Helper arguments must be a list of strings")
    remaining = list(argv)
    if remaining and remaining[0] == "--filesystem-helper":
        remaining.pop(0)
    values: dict[str, str] = {}
    allowed = {"--pipe", "--parent-pid", "--parent-created", "--plan-sha256"}
    index = 0
    while index < len(remaining):
        flag = remaining[index]
        if flag not in allowed or flag in values or index + 1 >= len(remaining):
            raise ValueError("Helper command line is invalid")
        values[flag] = remaining[index + 1]
        index += 2
    if set(values) != allowed:
        raise ValueError("Helper command line is incomplete")
    pipe_name = values["--pipe"]
    digest = values["--plan-sha256"]
    if not _PIPE_RE.fullmatch(pipe_name) or not _SHA256_RE.fullmatch(digest):
        raise ValueError("Helper pipe or plan hash is invalid")
    if not values["--parent-pid"].isdecimal() or not values["--parent-created"].isdecimal():
        raise ValueError("Helper process identity is invalid")
    parent_pid = int(values["--parent-pid"])
    parent_created = int(values["--parent-created"])
    if parent_pid <= 0 or parent_created <= 0 or parent_created >= 1 << 64:
        raise ValueError("Helper process identity is invalid")
    return pipe_name, parent_pid, parent_created, digest


def _retry_transaction(operation: Callable[[], Any]) -> Any:
    return _retry_cleanup(operation)


def _send_helper_error(connection: _PipeConnection, code: str, error: BaseException, report: Any | None = None) -> None:
    detail = str(error).replace("\x00", " ")[:1000] or type(error).__name__
    if report is None:
        report = getattr(error, "rollback", None)
    try:
        connection.send({"type": "error", "code": code[:80], "message": detail, "report": None if report is None else _report_dict(report)})
    except Exception:
        pass


def _helper_protocol(
    connection: _PipeConnection,
    *,
    parent_pid: int,
    parent_created: int,
    expected_digest: str,
) -> int:
    files = _files_module()
    transaction = None
    committed = False
    closed = False
    try:
        request = connection.receive()
        _exact_keys(request, {"type", "version", "parent_pid", "parent_created", "plan_sha256", "plan"}, "Plan message")
        if request.get("type") != "plan" or type(request.get("version")) is not int or request["version"] != 1:
            raise ProtocolError("Expected one version 1 plan")
        if type(request.get("parent_pid")) is not int or request["parent_pid"] != parent_pid:
            raise PeerRejected("Plan parent PID does not match the authenticated server")
        if type(request.get("parent_created")) is not int or request["parent_created"] != parent_created:
            raise PeerRejected("Plan parent creation time does not match the authenticated server")
        digest = request.get("plan_sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or digest != expected_digest:
            raise ProtocolError("Plan hash does not match the helper invocation")
        plan = request.get("plan")
        if not isinstance(plan, dict):
            raise ProtocolError("Plan payload must be an object")
        plan_payload = files.encode_plan(plan)
        if hashlib.sha256(plan_payload).hexdigest() != expected_digest:
            raise ProtocolError("Canonical plan hash does not match")
        plan = files.decode_plan(plan_payload)
        transaction = files.prepare(plan)
        connection.send({"type": "prepared", "target_path": str(transaction.target_path)})

        while True:
            command = connection.receive(idle=committed)
            if set(command) != {"type"} or not isinstance(command.get("type"), str):
                raise ProtocolError("Command message contains invalid fields")
            command_type = command["type"]
            if not committed and command_type == "commit":
                report = _retry_transaction(transaction.commit)
                committed = True
                connection.send({"type": "committed", "report": _report_dict(report)})
            elif not committed and command_type == "abort":
                report = _retry_transaction(transaction.abort)
                connection.send({"type": "closed", "report": _report_dict(report)})
                closed = True
                return 0 if getattr(report, "complete", False) else 1
            elif committed and command_type == "cleanup":
                report = _retry_cleanup(transaction.cleanup)
                connection.send({"type": "closed", "report": _report_dict(report)})
                closed = True
                return 0 if getattr(report, "complete", False) else 1
            else:
                raise ProtocolError("Command is invalid for the current transaction state")
    except _PipeDisconnected:
        if transaction is not None and not closed:
            report = _retry_cleanup(transaction.cleanup if committed else transaction.abort)
            return 0 if getattr(report, "complete", False) else 1
        return 0
    except BaseException as error:
        if transaction is not None and not closed:
            report = _retry_cleanup(transaction.cleanup if committed else transaction.abort)
            _send_helper_error(connection, "protocol_or_transaction", error, report)
            return 0 if getattr(report, "complete", False) else 1
        _send_helper_error(connection, "invalid_plan_or_peer", error)
        return 1


def helper_main(argv: list[str]) -> int:
    """Authenticate the parent, accept one plan, and serve one transaction."""
    try:
        pipe_name, parent_pid, parent_created, expected_digest = _read_helper_args(argv)
        if os.name != "nt":
            raise ElevationError("Native UAC transport requires Windows")
        connection = _connect_client(pipe_name, _CLIENT_WAIT_SECONDS)
        try:
            _verify_server(connection.handle, parent_pid, parent_created)
            connection.send({"type": "hello", "version": 1})
            return _helper_protocol(
                connection,
                parent_pid=parent_pid,
                parent_created=parent_created,
                expected_digest=expected_digest,
            )
        finally:
            connection.close()
    except BaseException:
        return 1


if __name__ == "__main__" and "--filesystem-helper" in sys.argv[1:]:
    raise SystemExit(helper_main(sys.argv[1:]))
