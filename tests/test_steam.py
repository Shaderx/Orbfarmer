"""Tests for steam.py – pure functions only (no network calls)."""

from unittest.mock import MagicMock, patch

from orbfarmer.steam import _pick_windows_exe, generate_appmanifest


class TestPickWindowsExe:
    def test_finds_first_windows_exe(self):
        launch = {
            "0": {
                "executable": "game.exe",
                "config": {"oslist": "windows"},
            },
            "1": {
                "executable": "game_server.exe",
                "config": {"oslist": "windows"},
            },
        }
        assert _pick_windows_exe(launch) == "game.exe"

    def test_skips_non_windows(self):
        launch = {
            "0": {
                "executable": "game.app",
                "config": {"oslist": "macos"},
            },
            "1": {
                "executable": "game.exe",
                "config": {"oslist": "windows"},
            },
        }
        assert _pick_windows_exe(launch) == "game.exe"

    def test_skips_non_exe(self):
        launch = {
            "0": {
                "executable": "game.sh",
                "config": {"oslist": "windows"},
            },
        }
        assert _pick_windows_exe(launch) is None

    def test_empty_launch(self):
        assert _pick_windows_exe({}) is None

    def test_normalises_backslashes(self):
        launch = {
            "0": {
                "executable": "Bin\\Win64\\game.exe",
                "config": {"oslist": "windows"},
            },
        }
        assert _pick_windows_exe(launch) == "Bin/Win64/game.exe"

    def test_empty_oslist_counts_as_windows(self):
        launch = {
            "0": {
                "executable": "game.exe",
                "config": {"oslist": ""},
            },
        }
        assert _pick_windows_exe(launch) == "game.exe"

    def test_handles_special_symbols_in_launch(self):
        launch = {
            "0": {
                "executable": "Bin\\Win64™\\Game®:Quest.exe",
                "config": {"oslist": "windows"},
            },
        }
        from orbfarmer.path_utils import sanitize_relative_path
        raw_exe = _pick_windows_exe(launch)
        assert sanitize_relative_path(raw_exe) == "Bin/Win64/GameQuest.exe"


def test_generate_appmanifest_refuses_to_overwrite(tmp_path):
    steam_path = tmp_path / "Steam"
    manifest = steam_path / "steamapps" / "appmanifest_123.acf"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("original", encoding="utf-8")

    with patch("orbfarmer.steam.get_steam_user_id", return_value="123"):
        result = generate_appmanifest(123, "Game", "Game", steam_path)

    assert result is None
    assert manifest.read_text(encoding="utf-8") == "original"


def test_steam_quest_mode_stops_and_cleans_its_scope(tmp_path):
    from orbfarmer.steam import steam_quest_mode

    faker = MagicMock()
    faker.chosen_path = tmp_path
    scope = object()
    faker.begin_scope.return_value = scope
    faker.launch_executable.return_value = True
    game = {"id": 2054970, "name": "Dragon's Dogma 2"}
    info = {
        "name": "Dragon's Dogma 2",
        "installdir": "Dragons Dogma 2",
        "executable": "DD2.exe",
        "depot_id": "2054971",
    }
    manifest = tmp_path / "steamapps" / "appmanifest_2054970.acf"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("manifest", encoding="utf-8")

    with patch("orbfarmer.steam._resolve_steam_path") as resolve_steam, \
         patch("orbfarmer.steam._pick_steam_game", return_value=game), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value=info), \
         patch("orbfarmer.steam.generate_appmanifest") as generate_manifest, \
         patch("orbfarmer.config.FAKE_EXE_DIR", "simulations"), \
         patch("orbfarmer.steam.ask_confirm", return_value=True), \
         patch("orbfarmer.steam.loading_animation"), \
         patch("builtins.input", side_effect=["dragon", "", ""]):
        steam_quest_mode(faker)

    faker.cleanup_scope.assert_called_once_with(scope)
    faker.copy_exe_to.assert_called_once_with(tmp_path / "simulations" / "Dragons Dogma 2" / "DD2.exe", game_name=game['name'], steam_appid=2054970)
    faker.launch_executable.assert_called_once_with(tmp_path / "simulations" / "Dragons Dogma 2" / "DD2.exe")
    resolve_steam.assert_not_called()
    generate_manifest.assert_not_called()
    assert manifest.read_text(encoding="utf-8") == "manifest"

