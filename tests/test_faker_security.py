"""Regression tests for file ownership and overwrite protections."""

from unittest.mock import MagicMock, patch

import pytest

from orbfarmer.faker import GameFaker


def _source_faker(tmp_path) -> GameFaker:
    source = tmp_path / "pythonw.exe"
    source.write_bytes(b"source executable")
    faker = GameFaker()
    faker._frozen = False
    faker._source_exe = source
    return faker


def test_copy_exe_refuses_existing_target(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "chosen" / "Game.exe"
    target.parent.mkdir()
    target.write_bytes(b"original game")

    with pytest.raises(FileExistsError):
        faker.copy_exe_to(target)

    assert target.read_bytes() == b"original game"
    assert target not in faker._created_files


def test_timer_script_collision_rolls_back_executable(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "chosen" / "Game.exe"
    target.parent.mkdir()
    timer_script = target.parent / "_Game_orbfarmer_timer.pyw"
    timer_script.write_text("original", encoding="utf-8")

    with pytest.raises(FileExistsError):
        faker.copy_exe_to(target)

    assert not target.exists()
    assert timer_script.read_text(encoding="utf-8") == "original"


def test_cleanup_preserves_file_that_replaced_an_owned_path(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "chosen" / "Game.exe"
    faker.copy_exe_to(target)
    target.unlink()
    target.write_bytes(b"replacement")

    with patch("orbfarmer.config.AUTO_DELETE", True):
        faker.cleanup()

    assert target.read_bytes() == b"replacement"


def test_cleanup_removes_only_owned_files(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "chosen" / "Game.exe"
    faker.copy_exe_to(target)
    sibling = target.parent / "keep.txt"
    sibling.write_text("keep", encoding="utf-8")

    with patch("orbfarmer.config.AUTO_DELETE", True):
        faker.cleanup()

    assert not target.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_cleanup_preserves_preexisting_empty_parent(tmp_path):
    faker = _source_faker(tmp_path)
    parent = tmp_path / "chosen"
    parent.mkdir()
    faker.copy_exe_to(parent / "Game.exe")

    with patch("orbfarmer.config.AUTO_DELETE", True):
        faker.cleanup()

    assert parent.exists()


def test_cleanup_scope_ignores_auto_delete_and_removes_only_new_resources(tmp_path):
    faker = _source_faker(tmp_path)
    old_file = tmp_path / "old.exe"
    old_file.write_bytes(b"old")
    faker.register_created_file(old_file)
    old_process = MagicMock()
    faker._processes.append(old_process)
    scope = faker.begin_scope()

    new_file = tmp_path / "new" / "Game.exe"
    faker.copy_exe_to(new_file)
    new_process = MagicMock()
    faker._processes.append(new_process)

    with patch("orbfarmer.config.AUTO_DELETE", False), \
         patch("orbfarmer.faker.time.sleep"):
        faker.cleanup_scope(scope)

    assert old_file.exists()
    assert old_process in faker._processes
    old_process.terminate.assert_not_called()
    assert not new_file.exists()
    assert not (new_file.parent / "_Game_orbfarmer_timer.pyw").exists()
    assert not new_file.parent.exists()
    new_process.terminate.assert_called_once()
    new_process.kill.assert_called_once()


def test_cleanup_scope_preserves_replaced_file(tmp_path):
    faker = _source_faker(tmp_path)
    scope = faker.begin_scope()
    target = tmp_path / "new" / "Game.exe"
    faker.copy_exe_to(target)
    target.unlink()
    target.write_bytes(b"replacement")

    with patch("orbfarmer.faker.time.sleep"):
        faker.cleanup_scope(scope)

    assert target.read_bytes() == b"replacement"
    assert target.parent.exists()


def test_windows_process_tree_stop_targets_only_live_owned_pid():
    live_process = MagicMock(pid=1234)
    live_process.poll.return_value = None
    exited_process = MagicMock(pid=5678)
    exited_process.poll.return_value = 0
    taskkill_result = MagicMock(returncode=0)

    with patch("orbfarmer.faker.sys.platform", "win32"), \
         patch("orbfarmer.faker.subprocess.run", return_value=taskkill_result) as run, \
         patch("orbfarmer.faker.time.sleep"):
        GameFaker._terminate_processes([live_process, exited_process])

    run.assert_called_once()
    assert run.call_args.args[0] == ["taskkill", "/PID", "1234", "/T", "/F"]
    exited_process.terminate.assert_not_called()
