"""Behavior for executable names that already have a simulation file."""

from contextlib import contextmanager
import shutil
import sys
from unittest.mock import patch

import pytest

from orbfarmer.faker import GameFaker, _timer_script_for


def _source_faker(tmp_path) -> GameFaker:
    source = tmp_path / "pythonw.exe"
    source.write_bytes(b"source executable")
    faker = GameFaker()
    faker._frozen = False
    faker._source_exe = source
    faker.chosen_path = tmp_path
    return faker


def test_existing_source_simulation_defaults_to_run_and_preserves_files(tmp_path):
    previous_faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "Game.exe"
    previous_faker.copy_exe_to(target)
    previous_exe = target.read_bytes()
    timer_script = _timer_script_for(target)
    previous_timer = timer_script.read_bytes()
    faker = _source_faker(tmp_path)

    with patch("orbfarmer.config.FAKE_EXE_DIR", "simulations"), \
         patch("orbfarmer.faker.loading_animation"), \
         patch("builtins.input", return_value=""):
        result = faker.create_fake_game("Game.exe")

    assert result == target
    assert target.read_bytes() == previous_exe
    assert timer_script.read_bytes() == previous_timer
    assert faker._created_files == []


def test_existing_executable_can_be_replaced_with_verified_helper_files(tmp_path):
    creator = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "Game.exe"
    creator.copy_exe_to(target)
    target.write_bytes(b"old executable bytes")
    old_timer = _timer_script_for(target).read_text(encoding="utf-8")
    old_timer = old_timer.replace("TIMER_MINUTES = 15", "TIMER_MINUTES = 99")
    _timer_script_for(target).write_text(old_timer, encoding="utf-8")

    faker = _source_faker(tmp_path)
    with patch("builtins.input", return_value="replace"):
        result = faker.prepare_executable(target)

    assert result == target
    assert target.read_bytes() == b"source executable"
    assert _timer_script_for(target).read_text(encoding="utf-8") != old_timer
    assert faker._created_files == [target, _timer_script_for(target)]
    assert not list(target.parent.glob("*.bak"))


def test_replace_refuses_unrecognized_sidecar_and_preserves_all_files(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "Game.exe"
    target.parent.mkdir()
    target.write_bytes(b"user executable")
    timer_script = _timer_script_for(target)
    timer_script.write_text("user script", encoding="utf-8")

    with patch("builtins.input", return_value="replace"):
        result = faker.prepare_executable(target)

    assert result is None
    assert target.read_bytes() == b"user executable"
    assert timer_script.read_text(encoding="utf-8") == "user script"
    assert not list(target.parent.glob("*.bak"))


def test_replace_restores_previous_files_if_new_copy_fails(tmp_path):
    creator = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "Game.exe"
    creator.copy_exe_to(target)
    old_executable = target.read_bytes()
    old_timer = _timer_script_for(target).read_bytes()
    faker = _source_faker(tmp_path)

    def fail_after_backup(path, theme, assets):
        if path.exists():
            raise FileExistsError(path)
        raise OSError("simulated disk failure")

    with patch.object(faker, "_copy_prepared_exe_to", side_effect=fail_after_backup), \
         patch("builtins.input", return_value="replace"), \
         pytest.raises(OSError, match="simulated disk failure"):
        faker.prepare_executable(target)

    assert target.read_bytes() == old_executable
    assert _timer_script_for(target).read_bytes() == old_timer
    assert not list(target.parent.glob("*.bak"))


def test_replace_restores_originals_after_partial_timer_write(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "Game.exe"
    faker.copy_exe_to(target)
    timer_script = _timer_script_for(target)
    original_executable = target.read_bytes()
    original_timer = timer_script.read_bytes()
    original_generations = dict(faker._created_file_generations)
    original_open = open

    @contextmanager
    def failing_writer():
        with original_open(timer_script, "x", encoding="utf-8") as file:
            class PartialWriter:
                def write(self, content):
                    file.write(content[:16])
                    file.flush()
                    raise OSError("simulated timer disk failure")
            yield PartialWriter()

    def open_file(path, mode="r", *args, **kwargs):
        if path == timer_script and mode == "x":
            return failing_writer()
        return original_open(path, mode, *args, **kwargs)

    with patch("builtins.input", return_value="replace"), \
         patch("builtins.open", side_effect=open_file), \
         pytest.raises(OSError, match="simulated timer disk failure"):
        faker.prepare_executable(target)

    assert target.read_bytes() == original_executable
    assert timer_script.read_bytes() == original_timer
    assert faker._created_file_generations == original_generations
    assert not list(target.parent.glob("*.bak"))


def test_replace_restores_originals_when_ownership_hash_fails(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "Game.exe"
    faker.copy_exe_to(target)
    timer_script = _timer_script_for(target)
    original_executable = target.read_bytes()
    original_timer = timer_script.read_bytes()
    original_hashes = dict(faker._created_file_hashes)
    original_generations = dict(faker._created_file_generations)

    with patch("builtins.input", return_value="replace"), \
         patch("orbfarmer.faker._sha256_file", side_effect=OSError("simulated hash failure")), \
         pytest.raises(OSError, match="simulated hash failure"):
        faker.prepare_executable(target)

    assert target.read_bytes() == original_executable
    assert timer_script.read_bytes() == original_timer
    assert faker._created_file_hashes == original_hashes
    assert faker._created_file_generations == original_generations
    assert not list(target.parent.glob("*.bak"))


def test_cancel_preserves_existing_executable(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "Game.exe"
    target.parent.mkdir()
    target.write_bytes(b"user executable")

    with patch("builtins.input", return_value="cancel"):
        result = faker.prepare_executable(target)

    assert result is None
    assert target.read_bytes() == b"user executable"
    assert faker._created_files == []


def test_choose_executable_action_returns_create_without_prompt(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "NewGame.exe"

    with patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
        assert faker.choose_executable_action(target) == "create"


def test_choose_executable_action_keeps_run_as_existing_file_default(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "Existing.exe"
    target.parent.mkdir()
    target.write_bytes(b"existing executable")

    with patch("builtins.input", return_value=""):
        assert faker.choose_executable_action(target) == "run"


def test_scope_cleanup_removes_replacement_generation(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "Game.exe"
    faker.copy_exe_to(target)
    scope = faker.begin_scope()

    with patch("builtins.input", return_value="replace"):
        result = faker.prepare_executable(target)
    faker.cleanup_scope(scope)

    assert result == target
    assert not target.exists()
    assert not _timer_script_for(target).exists()


def test_source_mode_launches_existing_native_executable_without_timer_arguments(tmp_path):
    faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "RealGame.exe"
    target.parent.mkdir()
    target.write_bytes(b"real executable")
    _timer_script_for(target).write_text("user script", encoding="utf-8")

    with patch("orbfarmer.faker.loading_animation"), \
         patch("orbfarmer.faker.subprocess.Popen") as popen:
        assert faker.launch_executable(target)

    args = popen.call_args.args[0]
    assert args == [str(target.resolve())]
    assert popen.call_args.kwargs["env"] is None
    assert popen.call_args.kwargs["cwd"] == str(target.parent.resolve())


def test_frozen_mode_launches_existing_source_helper_from_its_sidecar(tmp_path):
    source_faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "SourceGame.exe"
    source_faker.copy_exe_to(target)

    frozen_faker = _source_faker(tmp_path)
    frozen_faker._frozen = True
    with patch("orbfarmer.faker.loading_animation"), \
         patch("orbfarmer.faker.subprocess.Popen") as popen:
        assert frozen_faker.launch_executable(target)

    args = popen.call_args.args[0]
    env = popen.call_args.kwargs["env"]
    assert args == [str(target.resolve()), str(_timer_script_for(target).resolve())]
    assert env["PYTHONHOME"] == str(sys.base_prefix)


def test_build_bundles_timer_source_for_frozen_source_helper_reuse(tmp_path, monkeypatch):
    import build
    from orbfarmer import timer

    source_faker = _source_faker(tmp_path)
    target = tmp_path / "simulations" / "SourceGame.exe"
    source_faker.copy_exe_to(target)
    project = tmp_path / "project"
    package = project / "orbfarmer"
    package.mkdir(parents=True)
    (package / "_version.py").write_text('VERSION = "1.2.0"\n', encoding="utf-8")
    shutil.copyfile(timer.__file__, package / "timer.py")
    monkeypatch.setattr(build, "__file__", str(project / "build.py"))
    monkeypatch.setattr(build, "get_git_version", lambda: "1.2.0")
    bundle = tmp_path / "_MEI_test"
    bundled_timer = bundle / "orbfarmer" / "timer.py"
    monkeypatch.setattr(timer, "__file__", str(bundled_timer))
    assert not bundled_timer.exists()

    def simulate_build(command, **kwargs):
        if "PyInstaller" in command:
            assert "--add-data" in command, "The frozen build lacks timer source for helper detection"
            hidden_imports = {
                command[index + 1]
                for index, value in enumerate(command[:-1])
                if value == "--hidden-import"
            }
            assert hidden_imports == {"_orbfarmer_files", "_orbfarmer_elevation"}
            data_arg = command[command.index("--add-data") + 1]
            source, destination = data_arg.rsplit(":", 1)
            output = bundle / destination / "timer.py"
            output.parent.mkdir(parents=True)
            shutil.copyfile(source, output)

    with patch("build.shutil.copytree"), \
         patch("build.subprocess.run", side_effect=simulate_build):
        build.main()

    frozen_faker = _source_faker(tmp_path)
    frozen_faker._frozen = True
    with patch("orbfarmer.faker.loading_animation"), \
         patch("orbfarmer.faker.subprocess.Popen") as popen:
        assert frozen_faker.launch_executable(target)

    assert popen.call_args.args[0] == [str(target.resolve()), str(_timer_script_for(target).resolve())]
    assert popen.call_args.kwargs["env"]["PYTHONHOME"] == str(sys.base_prefix)


def test_source_mode_launches_existing_frozen_helper_without_a_sidecar(tmp_path):
    frozen_faker = _source_faker(tmp_path)
    frozen_faker._frozen = True
    target = tmp_path / "simulations" / "FrozenGame.exe"
    frozen_faker.copy_exe_to(target)

    source_faker = _source_faker(tmp_path)
    with patch("orbfarmer.faker.loading_animation"), \
         patch("orbfarmer.faker.subprocess.Popen") as popen:
        assert source_faker.launch_executable(target)

    args = popen.call_args.args[0]
    assert args == [str(target.resolve())]
    assert popen.call_args.kwargs["env"] is None
