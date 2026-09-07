"""Tests for config.py and faker.py settings loading and cleanup behavior."""

import sys
import json
import time
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from orbfarmer.config import _load_settings
from orbfarmer.faker import GameFaker


def test_load_settings_fallback_in_dev():
    # In normal dev mode (not frozen), if settings.json does not exist, it falls back to importing settings.py
    with patch("builtins.__import__") as mock_import:
        _load_settings()
        called_modules = [args[0] for args, _ in mock_import.call_args_list if args]
        assert "settings" in called_modules


def test_load_settings_json_in_dev(tmp_path):
    # If settings.json exists in dev mode, it should load it
    json_path = tmp_path / "settings.json"
    json_path.write_text('{"CHOSEN_FOLDER": "CustomDir"}', encoding="utf-8")

    # We patch __file__ in config module to point to our temp folder structure
    fake_config_file = tmp_path / "orbfarmer" / "config.py"
    with patch("orbfarmer.config.__file__", str(fake_config_file)):
        settings = _load_settings()
        assert isinstance(settings, dict)
        assert settings.get("CHOSEN_FOLDER") == "CustomDir"


def test_load_settings_frozen_creates_json_and_loads(tmp_path):
    # Simulate frozen mode where settings.json is missing next to the exe
    exe_path = tmp_path / "orbfarmer.exe"
    exe_path.touch()

    json_file = tmp_path / "settings.json"

    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", str(exe_path)):

        settings = _load_settings()

        # Verify it created a default settings.json next to the executable
        assert json_file.exists()
        # Verify it loaded the dictionary correctly
        assert isinstance(settings, dict)
        assert settings.get("CHOSEN_FOLDER") == "."
        assert settings.get("FAKE_EXE_DIR") == "simulations"
        assert settings.get("TIMER_MINUTES") == 15


def test_load_settings_frozen_existing_json(tmp_path):
    # Simulate frozen mode where settings.json already exists next to the exe
    exe_path = tmp_path / "orbfarmer.exe"
    exe_path.touch()

    json_file = tmp_path / "settings.json"
    json_file.write_text('{"CHOSEN_FOLDER": "FrozenDir"}', encoding="utf-8")

    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", str(exe_path)):

        settings = _load_settings()
        assert isinstance(settings, dict)
        assert settings.get("CHOSEN_FOLDER") == "FrozenDir"


def test_faker_cleanup_deletes_files_and_processes(tmp_path):
    # Mock config.AUTO_DELETE to True for testing
    with patch("orbfarmer.config.AUTO_DELETE", True):
        faker = GameFaker()

        # Mock process
        mock_proc = MagicMock()
        faker._processes.append(mock_proc)

        # Create dummy file to delete
        dummy_file = tmp_path / "faked_game.exe"
        dummy_file.touch()
        faker.register_created_file(dummy_file)

        # Create dummy directory to delete
        dummy_dir = tmp_path / "Win64"
        dummy_dir.mkdir()
        faker._created_dirs.append(dummy_dir)

        # Run cleanup
        faker.cleanup()

        # Assert process was terminated and killed
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

        # Assert file was deleted
        assert not dummy_file.exists()

        # Assert directory was deleted
        assert not dummy_dir.exists()


def test_faker_custom_timer_minutes(tmp_path):
    import orbfarmer.config as config
    from orbfarmer.faker import GameFaker

    # 1. Test source mode replacement
    with patch("orbfarmer.config.TIMER_MINUTES", 25), \
         patch("orbfarmer.config.AUTO_DELETE", False):

        faker = GameFaker()
        # Set dummy source exe
        dummy_src = tmp_path / "pythonw.exe"
        dummy_src.touch()
        faker._source_exe = dummy_src
        faker._frozen = False

        target_exe = tmp_path / "Win64" / "Game.exe"
        faker.copy_exe_to(target_exe)

        timer_script = tmp_path / "Win64" / "_Game_orbfarmer_timer.pyw"
        assert timer_script.exists()
        script_code = timer_script.read_text(encoding="utf-8")
        assert "TIMER_MINUTES = 25" in script_code
        assert "subprocess" not in script_code

    # 2. Test frozen mode launcher arguments
    with patch("orbfarmer.config.TIMER_MINUTES", 35):
        faker = GameFaker()
        faker._frozen = True

        with patch("subprocess.Popen") as mock_popen:
            faker.launch_executable(Path("C:/Dummy/Game.exe"))

            # Assert subprocess.Popen was called with just the executable path
            mock_popen.assert_called_once()
            called_args = mock_popen.call_args[1].get("args", mock_popen.call_args[0][0])
            assert called_args == [str(Path("C:/Dummy/Game.exe"))]


def test_is_faked_game():
    # Load orbfarmer entrypoint dynamically to test its functions
    spec = importlib.util.spec_from_file_location("orbfarmer_script", "orbfarmer.py")
    orb_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orb_module)

    # 1. Dev mode cases
    with patch("sys.frozen", False, create=True), \
         patch("sys.argv", ["orbfarmer.py"]):
        assert not orb_module.is_faked_game()

    with patch("sys.frozen", False, create=True), \
         patch("sys.argv", ["TslGame.py"]):
        assert orb_module.is_faked_game()

    # 2. Frozen mode cases
    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", "C:\\Users\\jjjda\\Desktop\\orbfarmer.exe"):
        assert not orb_module.is_faked_game()

    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", "C:\\Users\\jjjda\\Desktop\\Win64\\TslGame.exe"):
        assert orb_module.is_faked_game()


def test_non_windows_timer_only_waits_and_exits():
    from orbfarmer.timer import run_timer

    with patch("orbfarmer.timer.sys.platform", "linux"), \
         patch("orbfarmer.timer.time.sleep") as sleep:
        run_timer(2)

    sleep.assert_called_once_with(120)


def test_load_settings_baked_frozen(tmp_path):
    # Simulate a faked game with embedded settings at the end of the exe
    exe_path = tmp_path / "TslGame.exe"
    
    config_data = {
        "CHOSEN_FOLDER": "BakedDir",
        "AUTO_DELETE": True,
        "TIMER_MINUTES": 45
    }
    
    # Write the exe with appended marker and JSON settings
    import json
    marker = b"__ORBFARMER_BAKED_CONFIG__"
    json_bytes = json.dumps(config_data).encode("utf-8")
    
    exe_path.write_bytes(b"MZ_DUMMY_EXE_BYTES..." + marker + json_bytes + marker)
    
    with patch("sys.frozen", True, create=True), \
         patch("sys.executable", str(exe_path)):
         
        settings = _load_settings()
        assert isinstance(settings, dict)
        assert settings.get("CHOSEN_FOLDER") == "BakedDir"
        assert settings.get("AUTO_DELETE") is True
        assert settings.get("TIMER_MINUTES") == 45


def test_frozen_settings_python_sidecar_is_never_executed(tmp_path):
    exe_path = tmp_path / "orbfarmer.exe"
    exe_path.touch()
    marker = tmp_path / "executed.txt"
    (tmp_path / "settings.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )

    with patch("sys.frozen", True, create=True), patch("sys.executable", str(exe_path)):
        _load_settings()

    assert not marker.exists()
