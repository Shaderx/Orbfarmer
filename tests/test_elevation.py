"""Tests for the standalone Windows UAC broker and its IPC boundary."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import _orbfarmer_elevation as elevation


PLAN_KEYS = {
    "version",
    "transaction_id",
    "library_root",
    "appid",
    "name",
    "installdir",
    "executable",
    "action",
    "minutes",
    "theme",
    "assets",
    "manifest_action",
    "before",
}


class Report:
    def __init__(self, *, complete: bool = True):
        self.removed = ()
        self.preserved = ()
        self.leftovers = () if complete else ("held-file",)
        self.backups = ()
        self.complete = complete

    def to_dict(self):
        return {
            "removed": list(self.removed),
            "preserved": list(self.preserved),
            "leftovers": list(self.leftovers),
            "backups": list(self.backups),
            "complete": self.complete,
        }


class FakeTransaction:
    def __init__(self, target_path: Path):
        self.target_path = target_path
        self.calls: list[str] = []
        self.commit_reports = [Report()]
        self.cleanup_reports = [Report()]
        self.abort_reports = [Report()]

    def commit(self):
        self.calls.append("commit")
        return self.commit_reports.pop(0)

    def abort(self):
        self.calls.append("abort")
        return self.abort_reports.pop(0)

    def cleanup(self):
        self.calls.append("cleanup")
        return self.cleanup_reports.pop(0)


class PreparationError(Exception):
    def __init__(self, *, permission_denied: bool, denied_paths: tuple[str, ...], rollback: Report):
        super().__init__("permission denied while preparing")
        self.permission_denied = permission_denied
        self.denied_paths = denied_paths
        self.rollback = rollback


class FakeCore:
    PreparationError = PreparationError

    class CleanupReport:
        @staticmethod
        def from_dict(value):
            assert set(value) == {"removed", "preserved", "leftovers", "backups", "complete"}
            result = Report(complete=not value["leftovers"] and not value["backups"])
            result.removed = tuple(value["removed"])
            result.preserved = tuple(value["preserved"])
            result.leftovers = tuple(value["leftovers"])
            result.backups = tuple(value["backups"])
            assert value["complete"] == result.complete
            return result

    def __init__(self, plan: dict, *, has_writes: bool = True, probe=None, transaction=None, prepare_error=None):
        self.plan = plan
        self.writes = has_writes
        self.probe_result = probe or SimpleNamespace(needs_elevation=False, denied_paths=())
        self.transaction = transaction
        self.prepare_error = prepare_error
        self.calls: list[str] = []

    def encode_plan(self, plan):
        if set(plan) != PLAN_KEYS:
            raise ValueError("plan keys differ from the contract")
        return json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("ascii")

    def decode_plan(self, payload):
        result = json.loads(payload)
        if set(result) != PLAN_KEYS:
            raise ValueError("plan keys differ from the contract")
        return result

    def has_writes(self, plan):
        self.calls.append("has_writes")
        return self.writes

    def probe_permissions(self, plan):
        self.calls.append("probe")
        return self.probe_result

    def prepare(self, plan):
        self.calls.append("prepare")
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.transaction


def _plan(tmp_path: Path) -> dict:
    return {
        "version": 1,
        "transaction_id": "a" * 32,
        "library_root": str(tmp_path.resolve()),
        "appid": 123,
        "name": "Example",
        "installdir": "Example",
        "executable": "Example.exe",
        "action": "create",
        "minutes": 60,
        "theme": {"name": "blue", "accent": "#123456"},
        "assets": {},
        "manifest_action": "create",
        "before": {"exe": None, "timer": None, "hero": None, "icon": None, "manifest": None},
    }


def _target(plan: dict) -> Path:
    return Path(plan["library_root"], "steamapps", "common", plan["installdir"], plan["executable"])


def _install_core(monkeypatch, core: FakeCore):
    monkeypatch.setattr(elevation, "_files_module", lambda: core)


def test_empty_plan_skips_permission_probe_and_uses_local_transaction(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    transaction = FakeTransaction(_target(plan))
    core = FakeCore(plan, has_writes=False, transaction=transaction)
    _install_core(monkeypatch, core)
    monkeypatch.setattr(
        core,
        "probe_permissions",
        lambda _: pytest.fail("empty plan must not probe for write permission"),
    )

    session = elevation.prepare_session(plan, on_elevation_required=lambda _: pytest.fail("empty plan must not elevate"))

    assert session is not None
    assert session.target_path == _target(plan)
    assert session.elevated is False
    assert core.calls == ["has_writes", "prepare"]
    assert session.commit().complete
    assert session.close().complete
    assert transaction.calls == ["commit", "cleanup"]


def test_probe_denial_warns_before_uac_and_cancellation_returns_none(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    core = FakeCore(
        plan,
        probe=SimpleNamespace(needs_elevation=True, denied_paths=(r"C:\Steam\steamapps",)),
    )
    _install_core(monkeypatch, core)
    events: list[tuple[str, object]] = []

    def request_uac(*args):
        events.append(("uac", args))
        return None

    monkeypatch.setattr(elevation, "_launch_elevated", request_uac)
    result = elevation.prepare_session(
        plan,
        on_elevation_required=lambda paths: events.append(("warning", paths)),
    )

    assert result is None
    assert events == [
        ("warning", (r"C:\Steam\steamapps",)),
        ("uac", (plan, core.encode_plan(plan), hashlib.sha256(core.encode_plan(plan)).hexdigest())),
    ]
    assert core.calls == ["has_writes", "probe"]


def test_local_permission_failure_elevates_only_after_complete_rollback(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    incomplete = FakeCore(
        plan,
        prepare_error=PreparationError(
            permission_denied=True,
            denied_paths=(r"C:\Steam\steamapps\common",),
            rollback=Report(complete=False),
        ),
    )
    _install_core(monkeypatch, incomplete)
    elevated = []
    monkeypatch.setattr(elevation, "_launch_elevated", lambda *args: elevated.append(args) or None)

    with pytest.raises(PreparationError):
        elevation.prepare_session(plan, on_elevation_required=lambda _: None)
    assert elevated == []

    complete = FakeCore(
        plan,
        prepare_error=PreparationError(
            permission_denied=True,
            denied_paths=(r"C:\Steam\steamapps\common",),
            rollback=Report(),
        ),
    )
    _install_core(monkeypatch, complete)
    warnings = []
    monkeypatch.setattr(elevation, "_launch_elevated", lambda *args: None)

    assert elevation.prepare_session(plan, on_elevation_required=warnings.append) is None
    assert warnings == [(r"C:\Steam\steamapps\common",)]
    assert complete.calls == ["has_writes", "probe", "prepare"]


class MemoryPipe:
    """Two-endpoint in-memory stream for deterministic protocol state tests."""

    def __init__(self, incoming: queue.Queue, outgoing: queue.Queue):
        self.incoming = incoming
        self.outgoing = outgoing
        self.closed = False

    @classmethod
    def pair(cls):
        left, right = queue.Queue(), queue.Queue()
        return cls(left, right), cls(right, left)

    def send(self, value):
        if self.closed:
            raise elevation._PipeDisconnected("closed")
        self.outgoing.put(value)

    def receive(self, *, idle=False):
        value = self.incoming.get(timeout=2)
        if value is _CLOSED:
            raise elevation._PipeDisconnected("closed")
        return value

    def close(self):
        if not self.closed:
            self.closed = True
            self.outgoing.put(_CLOSED)


_CLOSED = object()


def _start_helper_protocol(plan: dict, core: FakeCore, monkeypatch):
    _install_core(monkeypatch, core)
    helper, parent = MemoryPipe.pair()
    payload = core.encode_plan(plan)
    digest = hashlib.sha256(payload).hexdigest()
    results: list[int] = []
    thread = threading.Thread(
        target=lambda: results.append(
            elevation._helper_protocol(
                helper,
                parent_pid=77,
                parent_created=1234,
                expected_digest=digest,
            )
        ),
        daemon=True,
    )
    thread.start()
    parent.send(
        {
            "type": "plan",
            "version": 1,
            "parent_pid": 77,
            "parent_created": 1234,
            "plan_sha256": digest,
            "plan": plan,
        }
    )
    assert parent.receive()["type"] == "prepared"
    return parent, thread, results


def test_helper_protocol_allows_one_plan_then_commit_and_cleanup(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    transaction = FakeTransaction(_target(plan))
    core = FakeCore(plan, transaction=transaction)
    parent, thread, results = _start_helper_protocol(plan, core, monkeypatch)

    parent.send({"type": "commit"})
    assert parent.receive()["type"] == "committed"
    parent.send({"type": "cleanup"})
    closed = parent.receive()
    thread.join(timeout=2)

    assert closed["type"] == "closed"
    assert results == [0]
    assert transaction.calls == ["commit", "cleanup"]


def test_helper_disconnect_before_commit_aborts_owned_files(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    transaction = FakeTransaction(_target(plan))
    core = FakeCore(plan, transaction=transaction)
    parent, thread, results = _start_helper_protocol(plan, core, monkeypatch)

    parent.close()
    thread.join(timeout=2)

    assert results == [0]
    assert transaction.calls == ["abort"]


def test_helper_rejects_plan_hash_mismatch_before_filesystem_prepare(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    transaction = FakeTransaction(_target(plan))
    core = FakeCore(plan, transaction=transaction)
    _install_core(monkeypatch, core)
    helper, parent = MemoryPipe.pair()
    good_digest = hashlib.sha256(core.encode_plan(plan)).hexdigest()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            elevation._helper_protocol(
                helper,
                parent_pid=77,
                parent_created=1234,
                expected_digest=good_digest,
            )
        ),
        daemon=True,
    )
    thread.start()
    parent.send(
        {
            "type": "plan",
            "version": 1,
            "parent_pid": 77,
            "parent_created": 1234,
            "plan_sha256": "b" * 64,
            "plan": plan,
        }
    )
    response = parent.receive()
    thread.join(timeout=2)

    assert response["type"] == "error"
    assert results == [1]
    assert core.calls == []
    assert transaction.calls == []


def test_helper_rejects_unknown_plan_fields_before_prepare(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    core = FakeCore(plan)
    _install_core(monkeypatch, core)
    helper, parent = MemoryPipe.pair()
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            elevation._helper_protocol(
                helper,
                parent_pid=77,
                parent_created=1234,
                expected_digest=digest,
            )
        ),
        daemon=True,
    )
    thread.start()
    plan["cleanup_paths"] = [str(tmp_path / "untrusted.txt")]
    parent.send(
        {
            "type": "plan",
            "version": 1,
            "parent_pid": 77,
            "parent_created": 1234,
            "plan_sha256": digest,
            "plan": plan,
        }
    )
    response = parent.receive()
    thread.join(timeout=2)

    assert response["type"] == "error"
    assert results == [1]
    assert core.calls == []


def test_helper_rejects_server_pid_creation_time_and_image_mismatch(monkeypatch):
    monkeypatch.setattr(elevation, "_named_pipe_server_pid", lambda _: 91)
    monkeypatch.setattr(elevation, "_process_created", lambda _: 1234)
    with pytest.raises(elevation.PeerRejected, match="initiating process"):
        elevation._verify_server(3, 92, 1234)
    with pytest.raises(elevation.PeerRejected, match="creation time"):
        elevation._verify_server(3, 91, 1235)

    monkeypatch.setattr(
        elevation,
        "_process_image",
        lambda _: r"C:\Other\program.exe",
    )
    with pytest.raises(elevation.PeerRejected, match="image"):
        elevation._verify_server(3, 91, 1234)


def test_helper_rejects_extra_command_fields_and_aborts(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    transaction = FakeTransaction(_target(plan))
    core = FakeCore(plan, transaction=transaction)
    parent, thread, results = _start_helper_protocol(plan, core, monkeypatch)

    parent.send({"type": "cleanup", "path": str(tmp_path / "arbitrary.txt")})
    response = parent.receive()
    thread.join(timeout=2)

    assert response["type"] == "error"
    assert results == [0]
    assert transaction.calls == ["abort"]


def test_helper_arguments_reject_unknown_paths_and_bad_pipe_names():
    valid = [
        "--filesystem-helper",
        "--pipe",
        elevation._PIPE_PREFIX + "a" * 32,
        "--parent-pid",
        "12",
        "--parent-created",
        "1234",
        "--plan-sha256",
        "b" * 64,
    ]
    assert elevation._read_helper_args(valid) == (
        elevation._PIPE_PREFIX + "a" * 32,
        12,
        1234,
        "b" * 64,
    )
    with pytest.raises(ValueError):
        elevation._read_helper_args(valid + ["--output", str(Path.cwd())])
    invalid_pipe = valid.copy()
    invalid_pipe[2] = r"\.pipeOrbfarmerUac-" + "a" * 32
    with pytest.raises(ValueError):
        elevation._read_helper_args(invalid_pipe)


def test_source_shell_launch_uses_isolated_hidden_helper_and_native_cancel_code(monkeypatch):
    captured = {}

    def shell_execute(pointer):
        info = ctypes.cast(pointer, ctypes.POINTER(elevation._ShellExecuteInfoW)).contents
        captured.update(
            verb=info.lpVerb,
            executable=info.lpFile,
            parameters=info.lpParameters,
            directory=info.lpDirectory,
            show=info.nShow,
        )
        return 0

    import ctypes

    monkeypatch.setattr(elevation, "_safe_cwd", lambda: r"C:\Windows\System32")
    monkeypatch.setattr(elevation.ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(ShellExecuteExW=shell_execute))
    monkeypatch.setattr(elevation.ctypes, "get_last_error", lambda: 1223)
    monkeypatch.delattr(sys, "frozen", raising=False)

    result = elevation._shell_execute_helper(
        elevation._PIPE_PREFIX + "c" * 32,
        12,
        1234,
        "d" * 64,
    )

    assert result is None
    assert captured["verb"] == "runas"
    assert Path(captured["executable"]).is_absolute()
    assert captured["parameters"].startswith("-I -S ")
    assert str(Path(elevation.__file__).resolve()) in captured["parameters"]
    assert captured["directory"] == r"C:\Windows\System32"
    assert captured["show"] == 0


def test_frozen_shell_launch_sets_then_restores_pyinstaller_environment(monkeypatch):
    old_value = os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "keep-parent-value"
    observed = []

    def shell_execute(pointer):
        info = ctypes.cast(pointer, ctypes.POINTER(elevation._ShellExecuteInfoW)).contents
        observed.append((os.environ.get("PYINSTALLER_RESET_ENVIRONMENT"), info.lpParameters))
        info.hProcess = 1234
        return 1

    import ctypes

    monkeypatch.setattr(elevation, "_safe_cwd", lambda: r"C:\Windows\System32")
    monkeypatch.setattr(elevation.ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(ShellExecuteExW=shell_execute))
    monkeypatch.setattr(elevation.sys, "frozen", True, raising=False)
    try:
        result = elevation._shell_execute_helper(
            elevation._PIPE_PREFIX + "e" * 32,
            12,
            1234,
            "f" * 64,
        )
        assert result == 1234
        assert observed[0][0] == "1"
        assert "--filesystem-helper" in observed[0][1]
        assert os.environ["PYINSTALLER_RESET_ENVIRONMENT"] == "keep-parent-value"
    finally:
        if old_value is None:
            os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        else:
            os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = old_value


@pytest.mark.skipif(os.name != "nt", reason="the Windows CreateFile API is required")
def test_helper_pipe_client_requests_identification_only_sqos(monkeypatch):
    captured = {}

    def wait_named_pipe(name, timeout):
        return 1

    def create_file(*args):
        captured["args"] = args
        return 123

    fake_kernel32 = SimpleNamespace(WaitNamedPipeW=wait_named_pipe, CreateFileW=create_file)
    monkeypatch.setattr(elevation, "_kernel32", lambda: fake_kernel32)
    client = elevation._connect_client(elevation._PIPE_PREFIX + "9" * 32, 1.0)

    assert client.handle == 123
    assert captured["args"][1] == 0x00120083
    assert captured["args"][5] == 0x40000000 | 0x00100000 | 0x00010000

    # Avoid calling CloseHandle on the mock handle.
    client.closed = True


def test_pipe_json_rejects_duplicate_keys_oversize_and_non_object_frames():
    with pytest.raises(elevation.ProtocolError, match="duplicate"):
        elevation._parse_json(b'{"type":"plan","type":"commit"}')
    with pytest.raises(elevation.ProtocolError, match="size"):
        elevation._parse_json(b" " * (elevation._MAX_MESSAGE + 1))
    with pytest.raises(elevation.ProtocolError, match="object"):
        elevation._parse_json(b"[]")


def test_parent_allows_alternate_admin_but_rejects_token_or_process_spoofing(monkeypatch):
    monkeypatch.setattr(elevation, "_named_pipe_client_pid", lambda _: 91)
    monkeypatch.setattr(elevation, "_pipe_client_token_info", lambda _: ("S-1-5-21-other-admin", False, True))
    with pytest.raises(elevation.PeerRejected, match="elevated administrator"):
        elevation._verify_client(3, 4)

    monkeypatch.setattr(elevation, "_pipe_client_token_info", lambda _: ("S-1-5-21-other-admin", True, True))
    monkeypatch.setattr(elevation, "_peer_token_info", lambda _: pytest.fail("cross-account process token must not be opened"))
    monkeypatch.setattr(elevation, "_process_image", lambda _: sys_exe())
    monkeypatch.setattr(elevation, "_kernel32", lambda: SimpleNamespace(
        GetProcessId=lambda _: 101,
        WaitForSingleObject=lambda *_: 0x00000102,
    ))
    monkeypatch.setattr(elevation, "_is_descendant_or_same", lambda *_: False)
    with pytest.raises(elevation.PeerRejected, match="descendant"):
        elevation._verify_client(3, 4)
    monkeypatch.setattr(elevation, "_is_descendant_or_same", lambda *_: True)
    assert elevation._verify_client(3, 4) == 91
    monkeypatch.setattr(elevation, "_process_image", lambda _: r"C:\Wrong\python.exe")
    with pytest.raises(elevation.PeerRejected, match="image"):
        elevation._verify_client(3, 4)


def sys_exe() -> str:
    return str(elevation._helper_executable())


@pytest.mark.skipif(os.name != "nt", reason="native Windows token query")
def test_native_primary_token_query_does_not_require_impersonation_token():
    sid, elevated, administrator, image = elevation._peer_token_info(os.getpid())

    assert sid.startswith("S-1-")
    assert type(elevated) is bool
    assert type(administrator) is bool
    assert elevation._normalize_image(image) == elevation._normalize_image(sys_exe())


def test_pipe_identification_token_query_reverts_even_after_failure(monkeypatch):
    import ctypes
    events = []

    def open_thread_token(thread, access, open_as_self, output):
        events.append(("open", thread, access, open_as_self))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 123
        return 1

    advapi = SimpleNamespace(
        ImpersonateNamedPipeClient=lambda handle: events.append(("identify", handle)) or 1,
        OpenThreadToken=open_thread_token,
        RevertToSelf=lambda: events.append(("revert",)) or 1,
    )
    monkeypatch.setattr(elevation, "_advapi32", lambda: advapi)
    monkeypatch.setattr(elevation, "_kernel32", lambda: SimpleNamespace(
        GetCurrentThread=lambda: 5, CloseHandle=lambda handle: events.append(("close", handle.value)),
    ))
    monkeypatch.setattr(elevation, "_token_user_sid", lambda _: "S-1-5-21-other-admin")
    monkeypatch.setattr(elevation, "_token_security_info", lambda _: (True, True))

    assert elevation._pipe_client_token_info(9) == ("S-1-5-21-other-admin", True, True)
    assert events == [("identify", 9), ("open", 5, 8, 1), ("close", 123), ("revert",)]
    events.clear()
    monkeypatch.setattr(elevation, "_token_security_info", lambda _: (_ for _ in ()).throw(ValueError("bad token")))
    with pytest.raises(ValueError, match="bad token"):
        elevation._pipe_client_token_info(9)
    assert events[-2:] == [("close", 123), ("revert",)]


def test_helper_sends_hello_after_server_authentication_without_same_sid_gate(monkeypatch):
    events = []
    connection = SimpleNamespace(
        handle=9,
        send=lambda value: events.append(("send", value)),
        close=lambda: events.append(("close",)),
    )
    monkeypatch.setattr(elevation, "_read_helper_args", lambda _: (elevation._PIPE_PREFIX + "a" * 32, 77, 1234, "b" * 64))
    monkeypatch.setattr(elevation, "_connect_client", lambda *_: connection)
    monkeypatch.setattr(elevation, "_verify_server", lambda *args: events.append(("verify", args)))
    monkeypatch.setattr(elevation, "_current_user_sid", lambda: pytest.fail("alternate-admin identity is permitted"))
    monkeypatch.setattr(elevation, "_peer_token_info", lambda _: pytest.fail("server token need not be opened"))
    monkeypatch.setattr(elevation, "_helper_protocol", lambda *args, **kwargs: events.append(("protocol",)) or 0)

    assert elevation.helper_main([]) == 0
    assert events == [("verify", (9, 77, 1234)), ("send", {"type": "hello", "version": 1}), ("protocol",), ("close",)]


def test_parent_reads_hello_and_authenticates_before_sending_bound_plan(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    events = []
    messages = iter([{"type": "hello", "version": 1}, {"type": "prepared", "target_path": str(_target(plan))}])
    connection = SimpleNamespace(
        handle=9,
        receive=lambda: events.append(("receive",)) or next(messages),
        send=lambda value: events.append(("send", value["type"])),
        close=lambda: None,
    )
    monkeypatch.setattr(elevation, "_current_user_sid", lambda: "S-1-5-21-current")
    monkeypatch.setattr(elevation, "_create_pipe_server", lambda *_: 9)
    monkeypatch.setattr(elevation, "_process_id", lambda: 77)
    monkeypatch.setattr(elevation, "_process_created", lambda _: 1234)
    monkeypatch.setattr(elevation, "_shell_execute_helper", lambda *_: 4)
    monkeypatch.setattr(elevation, "_connect_server", lambda *_: None)
    monkeypatch.setattr(elevation, "_PipeConnection", lambda _: connection)
    monkeypatch.setattr(elevation, "_verify_client", lambda *args: events.append(("verify", args)))

    session = elevation._launch_elevated(plan, b"plan", "b" * 64)

    assert session.target_path == _target(plan)
    assert events == [("receive",), ("verify", (9, 4)), ("send", "plan"), ("receive",)]
    session._helper_process = None


def test_timed_out_overlapped_operation_is_cancelled_and_drained(monkeypatch):
    events = []
    def drain(*args):
        events.append(("drain", args[-1]))
        return 0
    monkeypatch.setattr(elevation.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(elevation, "_kernel32", lambda: SimpleNamespace(
        WaitForSingleObject=lambda handle, timeout: events.append(("wait", timeout)) or 0x102,
        CancelIoEx=lambda *_: events.append(("cancel",)) or 1,
        GetOverlappedResult=drain,
    ))
    with pytest.raises(TimeoutError, match="named-pipe read"):
        elevation._wait_for_io(9, 10, elevation._OVERLAPPED(), 11.0, "named-pipe read")
    assert events == [("wait", 1000), ("cancel",), ("drain", 1)]


def test_helper_error_keeps_incomplete_rollback_report(tmp_path, monkeypatch):
    core = FakeCore(_plan(tmp_path))
    _install_core(monkeypatch, core)
    helper, parent = MemoryPipe.pair()
    report = Report(complete=False)
    report.backups = (str(tmp_path / "original-game.bak"),)
    error = PreparationError(permission_denied=True, denied_paths=(), rollback=report)

    elevation._send_helper_error(helper, "preparation", error)
    message = parent.receive()
    with pytest.raises(elevation.ElevationError) as result:
        elevation._error_message(message)
    assert result.value.report.backups == report.backups
    assert result.value.report.leftovers == report.leftovers
    assert not result.value.report.complete


@pytest.mark.skipif(os.name != "nt", reason="native named pipes require Windows")
def test_native_named_pipe_round_trip_uses_the_current_user_acl():
    name = elevation._pipe_name()
    server_handle = elevation._create_pipe_server(name, elevation._current_user_sid())
    server = elevation._PipeConnection(server_handle)
    errors: list[BaseException] = []

    def client_work():
        try:
            client = elevation._connect_client(name, 3.0)
            assert elevation._named_pipe_server_pid(client.handle) == os.getpid()
            client.send({"type": "hello", "value": 7})
            client.send({"type": "bulk", "value": "x" * (256 * 1024)})
            assert client.receive() == {"type": "reply", "value": 8}
            client.close()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=client_work, daemon=True)
    thread.start()
    elevation._connect_server(server.handle, 0, 3.0)
    assert elevation._named_pipe_client_pid(server.handle) == os.getpid()
    assert server.receive() == {"type": "hello", "value": 7}
    bulk = server.receive()
    assert bulk["type"] == "bulk"
    assert len(bulk["value"]) == 256 * 1024
    server.send({"type": "reply", "value": 8})
    thread.join(timeout=3)
    server.close()

    assert not errors
    assert not thread.is_alive()
