"""Standalone UAC file transaction contract tests using temporary libraries."""

import base64
import json
import os
from pathlib import Path

import pytest

import _orbfarmer_files as files


PNG = b"\x89PNG\r\n\x1a\n" + b"test image payload"
TIMER_SOURCE = b"def run_timer(*args, **kwargs):\n    return None\n"


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Steam"
    (root / "steamapps" / "common").mkdir(parents=True)
    return root


def _args(root, *, action="create", assets=None, installdir="Game", executable="Bin/Game.exe"):
    return {
        "library_root": root,
        "appid": 123456,
        "name": "Game",
        "installdir": installdir,
        "executable": executable,
        "action": action,
        "minutes": 17,
        "theme": {"name": "Game", "accent": "#aabbcc", "steam_appid": 123456},
        "assets": assets or {},
    }


def _runtime(*, frozen=False):
    return files._runtime_for_tests(
        executable=b"trusted executable bytes",
        timer_source=TIMER_SOURCE,
        frozen=frozen,
        python_home="C:/Python312",
    )


def _target(root, relative="Bin/Game.exe"):
    return root / "steamapps" / "common" / "Game" / Path(relative)


def test_plan_is_canonical_and_has_only_derived_destinations(library):
    with _runtime():
        plan = files.build_plan(**_args(library, assets={"hero": PNG, "icon": PNG}))
        encoded = files.encode_plan(plan)
        decoded = files.decode_plan(encoded)

    assert decoded == plan
    assert encoded == json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert set(plan) == {
        "version", "transaction_id", "library_root", "appid", "name", "installdir",
        "executable", "action", "minutes", "theme", "assets", "manifest_action", "before",
    }
    assert set(plan["before"]) == {"exe", "timer", "hero", "icon", "manifest"}
    assert plan["assets"]["hero"] == base64.b64encode(PNG).decode("ascii")
    assert plan["manifest_action"] == "create"


@pytest.mark.parametrize("executable", [
    "../outside.exe", "/absolute.exe", "C:/drive.exe", "//server/share.exe",
    "folder/../escape.exe", "folder/NUL.exe", "folder/Game.exe:stream",
    "\\\\?\\C:\\device.exe", "folder/Game.txt",
])
def test_plan_rejects_unsafe_or_non_executable_relative_paths(library, executable):
    with _runtime(), pytest.raises(ValueError):
        files.build_plan(**_args(library, executable=executable))


def test_plan_rejects_unknown_keys_duplicate_json_and_noncanonical_encoding(library):
    with _runtime():
        plan = files.build_plan(**_args(library))
        tampered = dict(plan, source_path="C:/attacker.exe")
        with pytest.raises(files.PlanError, match="unknown keys"):
            files.encode_plan(tampered)
        canonical = files.encode_plan(plan)
        with pytest.raises(files.PlanError, match="canonical"):
            files.decode_plan(canonical + b" ")
        with pytest.raises(files.PlanError, match="duplicate"):
            files.decode_plan(b'{"version":1,"version":1}')


@pytest.mark.parametrize("appid,name,installdir,expected", [
    (4080220, "Ignored input title", "EA SPORTS FC 27", b'"name"\t\t"EA SPORTS FC\xe2\x84\xa2 27"'),
    (123456, 'A "quoted" game', "Quoted Game", b'"StateFlags"\t\t"4"'),
])
def test_manifest_renderer_keeps_fc27_and_generic_recipes(appid, name, installdir, expected):
    content = files.render_manifest(appid, name, installdir)
    assert expected in content
    assert files.parse_manifest(content, appid) == installdir
    if appid == 4080220:
        assert b"StateFlags" not in content
        assert content.count(b"\n") == 6


def test_manifest_parser_preserves_validation_for_duplicates_and_escape_values():
    valid = files.render_manifest(123, "Game", "Install")
    duplicate = valid.replace(b'"appid"\t\t"123"', b'"appid"\t\t"123"\n\t"appid"\t\t"123"')
    escaping = valid.replace(b'"Install"', b'"../outside"')
    with pytest.raises(ValueError, match="one appid"):
        files.parse_manifest(duplicate, 123)
    with pytest.raises(ValueError, match="single relative folder"):
        files.parse_manifest(escaping, 123)
    with pytest.raises(ValueError, match="does not match"):
        files.parse_manifest(valid, 456)


def test_create_abort_removes_all_owned_outputs_and_new_directories(library):
    args = _args(library, assets={"hero": PNG})
    with _runtime():
        plan = files.build_plan(**args)
        transaction = files.prepare(plan)
        assert transaction.target_path == _target(library)
        assert transaction.target_path.read_bytes() == b"trusted executable bytes"
        assert (transaction.target_path.parent / "_Game_orbfarmer_timer.pyw").exists()
        assert (transaction.target_path.parent / "_Game_hero.png").read_bytes() == PNG
        manifest = library / "steamapps" / "appmanifest_123456.acf"
        assert files.parse_manifest(manifest.read_bytes(), 123456) == "Game"
        report = transaction.abort()
        repeated = transaction.cleanup()

    assert report.complete
    assert repeated.complete
    assert not _target(library).exists()
    assert not (library / "steamapps" / "common" / "Game").exists()
    assert not manifest.exists()


def test_create_collision_after_planning_preserves_the_new_file(library):
    with _runtime():
        plan = files.build_plan(**_args(library))
        target = _target(library)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"user raced the plan")
        with pytest.raises(files.PreparationError) as caught:
            files.prepare(plan)

    assert not caught.value.permission_denied
    assert target.read_bytes() == b"user raced the plan"
    assert not (target.parent / "_Game_orbfarmer_timer.pyw").exists()


def test_abort_preserves_files_changed_after_prepare(library):
    with _runtime():
        transaction = files.prepare(files.build_plan(**_args(library)))
        transaction.target_path.write_bytes(b"user replacement")
        report = transaction.abort()

    assert str(transaction.target_path) in report.preserved
    assert report.complete
    assert transaction.target_path.read_bytes() == b"user replacement"
    assert not (transaction.target_path.parent / "_Game_orbfarmer_timer.pyw").exists()


def test_replace_abort_restores_executable_and_verified_timer_backup(library):
    with _runtime():
        original = files.prepare(files.build_plan(**_args(library)))
        original.commit()
        target = original.target_path
        target.write_bytes(b"user selected this executable for replacement")
        timer = target.parent / "_Game_orbfarmer_timer.pyw"
        timer_before = timer.read_bytes()

        replacement_plan = files.build_plan(**_args(library, action="replace"))
        replacement = files.prepare(replacement_plan)
        backups = list(target.parent.glob("*.bak"))
        assert len(backups) == 2
        assert target.read_bytes() == b"trusted executable bytes"
        report = replacement.abort()

    assert report.complete
    assert target.read_bytes() == b"user selected this executable for replacement"
    assert timer.read_bytes() == timer_before
    assert not list(target.parent.glob("*.bak"))


def test_replace_commit_discards_backups_and_cleanup_preserves_unselected_sidecars(library):
    with _runtime():
        original = files.prepare(files.build_plan(**_args(library, assets={"hero": PNG})))
        original.commit()
        target = original.target_path
        target.write_bytes(b"old exe")
        hero = target.parent / "_Game_hero.png"
        hero_before = hero.read_bytes()

        replacement = files.prepare(files.build_plan(**_args(library, action="replace")))
        assert len(list(target.parent.glob("*.bak"))) == 2
        committed = replacement.commit()
        assert committed.complete
        assert not list(target.parent.glob("*.bak"))
        report = replacement.cleanup()

    assert report.complete
    assert not target.exists()
    assert not (target.parent / "_Game_orbfarmer_timer.pyw").exists()
    assert hero.read_bytes() == hero_before
    assert files.parse_manifest((library / "steamapps" / "appmanifest_123456.acf").read_bytes(), 123456) == "Game"


def test_prepare_rejects_unrecognized_timer_sidecar_without_modifying_originals(library):
    target = _target(library)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user exe")
    timer = target.parent / "_Game_orbfarmer_timer.pyw"
    timer.write_text("user-owned script", encoding="utf-8")
    with _runtime(), pytest.raises(ValueError, match="unrecognized Orbfarmer timer"):
        files.build_plan(**_args(library, action="replace"))
    assert target.read_bytes() == b"user exe"
    assert timer.read_text(encoding="utf-8") == "user-owned script"


def test_run_with_existing_manifest_has_no_probe_and_no_write(library, monkeypatch):
    target = _target(library)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing executable")
    manifest = library / "steamapps" / "appmanifest_123456.acf"
    manifest.write_bytes(files.render_manifest(123456, "Game", "Game"))
    with _runtime():
        plan = files.build_plan(**_args(library, action="run"))

    assert not files.has_writes(plan)

    def fail_if_probed():
        raise AssertionError("a no-write plan must not create a permission probe")

    monkeypatch.setattr(files, "_get_filesystem", fail_if_probed)
    assert files.probe_permissions(plan) == files.ProbeResult(False, ())


def test_probe_maps_access_denied_to_elevation_but_sharing_violations_raise(library, monkeypatch):
    with _runtime():
        plan = files.build_plan(**_args(library))

    class DeniedFilesystem:
        def probe(self, path, root, *, replace_existing):
            raise PermissionError(5, "access denied", str(path))

    monkeypatch.setattr(files, "_get_filesystem", DeniedFilesystem)
    result = files.probe_permissions(plan)
    assert result.needs_elevation
    assert result.denied_paths == (
        str(_target(library)),
        str(_target(library).with_name("_Game_orbfarmer_timer.pyw")),
        str(library / "steamapps" / "appmanifest_123456.acf"),
    )

    class SharingFilesystem:
        def probe(self, path, root, *, replace_existing):
            error = OSError(13, "sharing violation", str(path))
            error.winerror = 32
            raise error

    monkeypatch.setattr(files, "_get_filesystem", SharingFilesystem)
    with pytest.raises(OSError, match="sharing violation"):
        files.probe_permissions(plan)


def test_native_adapter_inspects_hashes_exclusive_files_and_deletes_by_identity(library):
    fs = files._get_filesystem()
    parent = library / "steamapps" / "common" / "Native Adapter"
    made_dirs = fs.ensure_parent_dirs(parent, library)
    path = parent / "checked.bin"
    expected = fs.create_file(path, b"adapter payload", library)
    actual, content = fs.inspect(path, read_bytes=True)
    assert actual == expected
    assert content == b"adapter payload"
    assert fs.delete_if_matches(path, expected, library) == "removed"
    assert not path.exists()
    for owned in reversed(made_dirs):
        assert fs.remove_directory_if_matches(owned, library) == "removed"


def test_cleanup_report_round_trip_rejects_inconsistent_completion():
    report = files.CleanupReport(("one",), ("two",), ("three",), ("backup",))
    assert files.CleanupReport.from_dict(report.to_dict()) == report
    invalid = {**report.to_dict(), "complete": True}
    with pytest.raises(ValueError, match="inconsistent"):
        files.CleanupReport.from_dict(invalid)


@pytest.mark.skipif(os.name != "nt", reason="Windows native directory access masks")
def test_native_file_only_operations_request_only_selected_directory_rights(library, monkeypatch):
    target = _target(library)
    target.parent.mkdir(parents=True)
    fs = files._get_filesystem()
    original_open = fs._nt_open
    directory_opens = []

    def record_open(parent, name, path, **kwargs):
        if kwargs["options"] & files._FILE_DIRECTORY_FILE:
            directory_opens.append((Path(path), kwargs["access"], kwargs.get("share", files._FILE_SHARE_READ), kwargs["disposition"]))
        return original_open(parent, name, path, **kwargs)

    monkeypatch.setattr(fs, "_nt_open", record_open)
    monkeypatch.setattr(files, "_get_filesystem", lambda: fs)
    with _runtime():
        plan = files.build_plan(**_args(library))
        assert not files.probe_permissions(plan).needs_elevation
        transaction = files.prepare(plan)
        transaction.commit()
        assert transaction.cleanup().complete

    assert directory_opens
    assert all(share == files._FILE_SHARE_READ for _, _, share, _ in directory_opens)
    assert all(access & files._FILE_TRAVERSE for _, access, _, _ in directory_opens)
    assert all(not access & (files._FILE_ADD_SUBDIRECTORY | files._FILE_DELETE_CHILD | files._FILE_LIST_DIRECTORY)
               for _, access, _, disposition in directory_opens if disposition == files._FILE_OPEN)
    parents = {target.parent, library / "steamapps"}
    assert all(not access & files._FILE_ADD_FILE for path, access, _, _ in directory_opens if path not in parents)


@pytest.mark.skipif(os.name != "nt", reason="Windows native directory pinning")
@pytest.mark.parametrize("desired_access", [0x40000000, 0x00010000])
@pytest.mark.parametrize("ancestor", [False, True])
def test_native_pins_block_concurrent_directory_write_or_delete_open(library, desired_access, ancestor):
    import ctypes

    parent = _target(library).parent
    parent.mkdir(parents=True)
    fs = files._get_filesystem()
    _, directories = fs._pin_directory(parent, library)
    try:
        opened_path = library / "steamapps" if ancestor else parent
        handle = fs.api.kernel32.CreateFileW(
            str(opened_path), desired_access, 7, None, 3,
            files._FILE_FLAG_BACKUP_SEMANTICS | files._FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if handle != ctypes.c_void_p(-1).value:
            fs.api.kernel32.CloseHandle(handle)
            pytest.fail("The pinned directory permitted a concurrent writer or delete handle")
        assert ctypes.get_last_error() == 32
    finally:
        fs._close_directories(directories)


def test_cleanup_retries_retain_the_locked_file_ledger(library, monkeypatch):
    with _runtime():
        transaction = files.prepare(files.build_plan(**_args(library)))
    target = transaction.target_path
    transaction.commit()
    original_delete = transaction._fs.delete_if_matches
    locked = True

    def delete_after_unlock(path, expected, root):
        if path == target and locked:
            raise PermissionError("file is temporarily locked")
        return original_delete(path, expected, root)

    monkeypatch.setattr(transaction._fs, "delete_if_matches", delete_after_unlock)
    first = transaction.cleanup()
    assert not first.complete
    assert str(target) in first.leftovers
    assert target.exists()
    locked = False
    second = transaction.cleanup()
    assert second.complete
    assert not target.exists()
    assert not target.parent.exists()
    assert not target.parent.parent.exists()
    assert str(target) in second.removed
