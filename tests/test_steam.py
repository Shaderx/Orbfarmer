"""Tests for steam.py – pure functions only (no network calls)."""

from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize("appid", [123, 4080220])
def test_generate_appmanifest_refuses_to_overwrite(tmp_path, appid):
    steam_path = tmp_path / "Steam"
    manifest = steam_path / "steamapps" / f"appmanifest_{appid}.acf"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("original", encoding="utf-8")

    with patch("orbfarmer.steam.get_steam_user_id", return_value="123"):
        result = generate_appmanifest(appid, "Game", "Game", steam_path)

    assert result is None
    assert manifest.read_text(encoding="utf-8") == "original"


def test_generate_appmanifest_writes_minimal_installed_state_and_escapes_values(tmp_path):
    steam_path = tmp_path / "Steam"
    steamapps = steam_path / "steamapps"
    steamapps.mkdir(parents=True)

    manifest = generate_appmanifest(123, 'Game "One"', 'Game Folder', steam_path)

    assert manifest == steamapps / "appmanifest_123.acf"
    assert manifest.read_text(encoding="utf-8") == (
        '"AppState"\n{\n'
        '\t"appid"\t\t"123"\n'
        '\t"name"\t\t"Game \\"One\\""\n'
        '\t"StateFlags"\t\t"4"\n'
        '\t"installdir"\t\t"Game Folder"\n'
        '}\n'
    )


@pytest.mark.parametrize("name", ["EA SPORTS FC™ 27", "EA SPORTS FC 27"])
def test_generate_appmanifest_fc27_recipe_contains_only_three_fields(tmp_path, name):
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()

    manifest = generate_appmanifest(4080220, name, "EA SPORTS FC 27", tmp_path)

    assert manifest == steamapps / "appmanifest_4080220.acf"
    assert manifest.read_text(encoding="utf-8") == (
        '"AppState"\n{\n'
        '\t"appid"\t\t"4080220"\n'
        '\t"name"\t\t"EA SPORTS FC™ 27"\n'
        '\t"installdir"\t\t"EA SPORTS FC 27"\n'
        '}\n'
    )


def test_generate_appmanifest_requires_positive_appid(tmp_path):
    (tmp_path / "steamapps").mkdir()

    import pytest
    with pytest.raises(ValueError):
        generate_appmanifest(0, "Game", "Game", tmp_path)


def test_generate_appmanifest_removes_partial_file_after_write_failure(tmp_path):
    from contextlib import contextmanager

    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    manifest = steamapps / "appmanifest_123.acf"
    original_open = open

    @contextmanager
    def failing_writer():
        with original_open(manifest, "x", encoding="utf-8") as file:
            class PartialWriter:
                def write(self, content):
                    file.write(content[:16])
                    file.flush()
                    raise OSError("simulated manifest disk failure")
            yield PartialWriter()

    def open_file(path, mode="r", *args, **kwargs):
        if path == manifest and mode == "x":
            return failing_writer()
        return original_open(path, mode, *args, **kwargs)

    with patch("builtins.open", side_effect=open_file):
        assert generate_appmanifest(123, "Game", "Game", tmp_path) is None

    assert not manifest.exists()


def _minimal_acf(appid: int, installdir: str) -> str:
    return (
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{appid}"\n'
        '\t"name"\t\t"Existing name"\n'
        '\t"StateFlags"\t\t"4"\n'
        f'\t"installdir"\t\t"{installdir}"\n'
        '}\n'
    )


@pytest.mark.parametrize("acf", [
    _minimal_acf(123, "Folder").replace('"appid"\t\t"123"', '"appid"\t\t"123"\n\t"appid"\t\t"123"'),
    _minimal_acf(456, "Folder"),
    _minimal_acf(123, "C:\\outside"),
    _minimal_acf(123, "/outside"),
    _minimal_acf(123, "Folder").replace('"AppState"', '"AppState"', 1) + '"AppState" { "appid" "123" "installdir" "Second" }',
])
def test_read_appmanifest_rejects_ambiguous_or_escaping_values(tmp_path, acf):
    from orbfarmer.steam import _read_appmanifest

    manifest = tmp_path / "appmanifest_123.acf"
    manifest.write_text(acf, encoding="utf-8")

    with pytest.raises(ValueError):
        _read_appmanifest(manifest, 123)


def test_library_session_reuses_valid_manifest_folder_without_modifying_manifest(tmp_path, capsys):
    from orbfarmer.steam import run_steam_library_session

    steam_root = tmp_path / "Steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    manifest = steamapps / "appmanifest_4080220.acf"
    original = _minimal_acf(4080220, "Installed FC27 Folder")
    manifest.write_text(original, encoding="utf-8")
    faker = MagicMock()
    scope = object()
    target = steamapps / "common" / "Installed FC27 Folder" / "fc27.exe"
    faker.choose_executable_action.return_value = "run"
    faker.begin_scope.return_value = scope
    events = []
    faker.launch_executable.side_effect = lambda path: events.append(("launch", path)) or True
    faker.stop_scope_processes.side_effect = lambda value: events.append(("stop", value))
    report = SimpleNamespace(complete=True, preserved=(), leftovers=(), backups=())
    session = MagicMock(target_path=target)
    session.commit.side_effect = lambda: events.append(("commit", None)) or report
    session.close.side_effect = lambda: events.append(("close", None)) or report

    with patch("orbfarmer.steam.get_steam_path", return_value=steam_root), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value={
             "name": "EA SPORTS FC 27", "installdir": "wrong fetched folder",
             "executable": "wrong.exe", "depot_id": None,
         }), \
         patch("orbfarmer.artwork.prepare_artwork", return_value=({"name": "EA SPORTS FC 27", "accent": "#aac9b5", "steam_appid": 4080220}, {})), \
         patch("_orbfarmer_files.build_plan", return_value={"action": "run"}) as build_plan, \
         patch("_orbfarmer_elevation.prepare_session", return_value=session) as prepare_session, \
         patch("orbfarmer.steam.generate_appmanifest") as generate_manifest, \
         patch("orbfarmer.steam.ask_confirm", return_value=True), \
         patch("builtins.input", side_effect=["", "", ""]), \
         patch("orbfarmer.steam.loading_animation"):
        run_steam_library_session(faker, 4080220, game_name="EA SPORTS FC 27")

    build_plan.assert_called_once()
    assert build_plan.call_args.kwargs["library_root"] == steam_root.resolve()
    assert build_plan.call_args.kwargs["installdir"] == "Installed FC27 Folder"
    assert build_plan.call_args.kwargs["executable"] == "fc27.exe"
    assert build_plan.call_args.kwargs["action"] == "run"
    assert prepare_session.call_count == 1
    assert prepare_session.call_args.args == ({"action": "run"},)
    assert callable(prepare_session.call_args.kwargs["on_elevation_required"])
    generate_manifest.assert_not_called()
    faker.launch_executable.assert_called_once_with(target)
    assert events == [("launch", target), ("commit", None), ("stop", scope), ("close", None)]
    session.commit.assert_called_once()
    session.close.assert_called_once()
    assert manifest.read_text(encoding="utf-8") == original
    output = capsys.readouterr().out
    assert str(manifest) in output
    assert str(target) in output


@pytest.mark.parametrize("override, exe_relative", [("", "fc27.exe"), ("Bin\\custom.exe", "Bin/custom.exe")])
def test_fc27_profile_sets_defaults_before_executable_override(tmp_path, override, exe_relative):
    from orbfarmer.steam import run_steam_library_session

    steam_root = tmp_path / "Steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    target = steamapps / "common" / "EA SPORTS FC 27" / exe_relative
    faker = MagicMock()
    faker.choose_executable_action.return_value = "create"
    faker.begin_scope.return_value = object()
    faker.launch_executable.return_value = True
    report = SimpleNamespace(complete=True, preserved=(), leftovers=(), backups=())
    session = MagicMock(target_path=target)
    session.commit.return_value = report
    session.close.return_value = report

    with patch("orbfarmer.steam.get_steam_path", return_value=steam_root), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value={
             "name": "EA SPORTS FC 27", "installdir": "API folder",
             "executable": "api.exe", "depot_id": None,
         }), \
         patch("orbfarmer.artwork.prepare_artwork", return_value=({"name": "EA SPORTS FC 27", "accent": "#aac9b5", "steam_appid": 4080220}, {})), \
         patch("_orbfarmer_files.build_plan", return_value={"action": "create"}) as build_plan, \
         patch("_orbfarmer_elevation.prepare_session", return_value=session), \
         patch("orbfarmer.steam.ask_confirm", return_value=True), \
         patch("builtins.input", side_effect=["", override, ""]), \
         patch("orbfarmer.steam.loading_animation"):
        run_steam_library_session(faker, 4080220, game_name="EA SPORTS FC 27")

    build_plan.assert_called_once()
    assert build_plan.call_args.kwargs["installdir"] == "EA SPORTS FC 27"
    assert build_plan.call_args.kwargs["executable"] == exe_relative
    assert build_plan.call_args.kwargs["action"] == "create"
    faker.launch_executable.assert_called_once_with(target)
    session.commit.assert_called_once()


def test_library_session_rejects_existing_manifest_path_escape_before_copy(tmp_path):
    import pytest
    from orbfarmer.steam import run_steam_library_session

    steam_root = tmp_path / "Steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "appmanifest_123.acf").write_text(
        _minimal_acf(123, "../outside"), encoding="utf-8"
    )
    faker = MagicMock()

    with patch("orbfarmer.steam.get_steam_path", return_value=steam_root), \
         patch("builtins.input", return_value=""), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value={
             "name": "Game", "installdir": "Game", "executable": "Game.exe", "depot_id": None,
         }), \
         patch("orbfarmer.steam.print_color"):
        run_steam_library_session(faker, 123, game_name="Game")

    faker.choose_executable_action.assert_not_called()
    faker.launch_executable.assert_not_called()
    faker.stop_scope_processes.assert_not_called()


def test_library_session_cancel_returns_before_plan_or_permission_probe(tmp_path):
    from orbfarmer.steam import run_steam_library_session

    steam_root = tmp_path / "Steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    faker = MagicMock()
    faker.choose_executable_action.return_value = "cancel"

    with patch("orbfarmer.steam.get_steam_path", return_value=steam_root), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value={
             "name": "Game", "installdir": "Game", "executable": "Game.exe", "depot_id": None,
         }), \
         patch("_orbfarmer_files.build_plan") as build_plan, \
         patch("_orbfarmer_elevation.prepare_session") as prepare_session, \
         patch("builtins.input", side_effect=["", ""]), \
         patch("orbfarmer.steam.ask_confirm", return_value=True), \
         patch("orbfarmer.steam.loading_animation"), \
         patch("orbfarmer.steam.print_color"):
        run_steam_library_session(faker, 123, game_name="Game")

    assert not (steamapps / "appmanifest_123.acf").exists()
    build_plan.assert_not_called()
    prepare_session.assert_not_called()
    faker.launch_executable.assert_not_called()
    faker.begin_scope.assert_not_called()


def test_library_session_warns_before_a_cancelled_elevation_request(tmp_path, capsys):
    from orbfarmer.steam import run_steam_library_session

    steam_root = tmp_path / "Steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    faker = MagicMock()
    faker.choose_executable_action.return_value = "create"
    protected = str(steamapps / "common" / "Game" / "Game.exe")

    def refuse_after_warning(plan, *, on_elevation_required):
        on_elevation_required((protected,))
        return None

    with patch("orbfarmer.steam.get_steam_path", return_value=steam_root), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value={
             "name": "Game", "installdir": "Game", "executable": "Game.exe", "depot_id": None,
         }), \
         patch("orbfarmer.artwork.prepare_artwork", return_value=({"name": "Game", "accent": "#aac9b5"}, {})), \
         patch("_orbfarmer_files.build_plan", return_value={"action": "create"}), \
         patch("_orbfarmer_elevation.prepare_session", side_effect=refuse_after_warning), \
         patch("builtins.input", side_effect=["", ""]), \
         patch("orbfarmer.steam.ask_confirm", return_value=True), \
         patch("orbfarmer.steam.loading_animation"):
        run_steam_library_session(faker, 123, game_name="Game")

    output = capsys.readouterr().out
    assert "administrator permission" in output
    assert "cancel at the Windows prompt" in output
    assert protected in output
    assert "No game was launched" in output
    faker.launch_executable.assert_not_called()
    faker.begin_scope.assert_not_called()


def test_library_plan_reports_unrestored_backups_on_preparation_failure(tmp_path, capsys):
    from _orbfarmer_elevation import ElevationError
    from orbfarmer.steam import _run_library_plan

    backup = str(tmp_path / "original-game.bak")
    report = SimpleNamespace(complete=False, preserved=(), leftovers=(), backups=(backup,))
    with patch("_orbfarmer_elevation.prepare_session", side_effect=ElevationError("prepare failed", report=report)):
        _run_library_plan(MagicMock(), {})

    output = capsys.readouterr().out
    assert backup in output
    assert "cleanup was incomplete" in output


def test_library_plan_reports_unresolved_backups_when_close_raises(tmp_path, capsys):
    from _orbfarmer_elevation import ElevationError
    from orbfarmer.steam import _run_library_plan

    backup = str(tmp_path / "original-game.bak")
    report = SimpleNamespace(complete=False, preserved=(), leftovers=(), backups=(backup,))
    session = MagicMock(target_path=tmp_path / "Game.exe")
    session.commit.return_value = SimpleNamespace(complete=True, preserved=(), leftovers=(), backups=())
    session.close.side_effect = ElevationError("close failed", report=report)
    faker = MagicMock()
    faker.launch_executable.return_value = True
    with patch("_orbfarmer_elevation.prepare_session", return_value=session), \
         patch("orbfarmer.steam.loading_animation"), \
         patch("builtins.input", return_value=""):
        _run_library_plan(faker, {})

    output = capsys.readouterr().out
    assert backup in output
    assert "cleanup was incomplete" in output


@pytest.mark.parametrize("value", [0, 0.04, -5, 10081, "15"])
def test_library_plan_uses_safe_minutes_when_legacy_setting_is_invalid(value):
    from orbfarmer.steam import _library_minutes

    with patch("orbfarmer.steam.config.TIMER_MINUTES", value):
        assert _library_minutes() == 15


def test_library_root_can_override_registry_suggestion(tmp_path):
    from orbfarmer.steam import _resolve_steam_library

    suggested = tmp_path / "Steam"
    alternate = tmp_path / "Library"
    (suggested / "steamapps").mkdir(parents=True)
    (alternate / "steamapps").mkdir(parents=True)

    with patch("orbfarmer.steam.get_steam_path", return_value=suggested), \
         patch("builtins.input", return_value=str(alternate)):
        assert _resolve_steam_library() == alternate


def test_library_session_launch_failure_aborts_without_commit(tmp_path):
    from orbfarmer.steam import run_steam_library_session

    steam_root = tmp_path / "Steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    faker = MagicMock()
    target = steamapps / "common" / "Game" / "Game.exe"
    scope = object()
    faker.choose_executable_action.return_value = "create"
    faker.begin_scope.return_value = scope
    faker.launch_executable.return_value = False
    faker.stop_scope_processes.side_effect = lambda value: events.append(("stop", value))
    report = SimpleNamespace(complete=True, preserved=(), leftovers=(), backups=())
    session = MagicMock(target_path=target)
    session.close.side_effect = lambda: events.append(("close", None)) or report
    events = []

    with patch("orbfarmer.steam.get_steam_path", return_value=steam_root), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value={
             "name": "Game", "installdir": "Game", "executable": "Game.exe", "depot_id": None,
         }), \
         patch("orbfarmer.artwork.prepare_artwork", return_value=({"name": "Game", "accent": "#aac9b5"}, {})), \
         patch("_orbfarmer_files.build_plan", return_value={"action": "create"}), \
         patch("_orbfarmer_elevation.prepare_session", return_value=session), \
         patch("builtins.input", side_effect=["", ""]), \
         patch("orbfarmer.steam.ask_confirm", return_value=True), \
         patch("orbfarmer.steam.loading_animation"), \
         patch("orbfarmer.steam.print_color"):
        run_steam_library_session(faker, 123, game_name="Game")

    session.commit.assert_not_called()
    session.close.assert_called_once()
    faker.stop_scope_processes.assert_called_once_with(scope)
    assert events == [("stop", scope), ("close", None)]


def test_run_existing_with_valid_manifest_skips_permission_probe_and_uac(tmp_path):
    import _orbfarmer_files
    from _orbfarmer_files import _runtime_for_tests, has_writes
    from orbfarmer.steam import run_steam_library_session

    steam_root = tmp_path / "Steam"
    steamapps = steam_root / "steamapps"
    target = steamapps / "common" / "Game" / "Game.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing executable")
    manifest = steamapps / "appmanifest_123.acf"
    manifest.write_text(_minimal_acf(123, "Game"), encoding="utf-8")
    faker = MagicMock()
    faker.choose_executable_action.return_value = "run"
    faker.begin_scope.return_value = object()
    faker.launch_executable.return_value = True
    built_plans = []
    original_build_plan = _orbfarmer_files.build_plan

    def capture_plan(**kwargs):
        plan = original_build_plan(**kwargs)
        built_plans.append(plan)
        return plan

    with _runtime_for_tests(executable=b"runtime", timer_source=b"", frozen=True), \
         patch("orbfarmer.steam.get_steam_path", return_value=steam_root), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value={
             "name": "Game", "installdir": "Game", "executable": "Game.exe", "depot_id": None,
         }), \
         patch("orbfarmer.artwork.prepare_artwork", return_value=({"name": "Game", "accent": "#aac9b5", "steam_appid": 123}, {})), \
         patch("_orbfarmer_files.build_plan", side_effect=capture_plan) as build_plan, \
         patch("orbfarmer.steam.ask_confirm", return_value=True), \
         patch("orbfarmer.steam.loading_animation"), \
         patch("builtins.input", side_effect=["", "", ""]), \
         patch("_orbfarmer_files.probe_permissions", side_effect=AssertionError("empty plan was probed")) as probe, \
         patch("_orbfarmer_elevation._launch_elevated", side_effect=AssertionError("empty plan requested UAC")) as elevate:
        run_steam_library_session(faker, 123, game_name="Game")

    probe.assert_not_called()
    elevate.assert_not_called()
    build_plan.assert_called_once()
    assert len(built_plans) == 1
    assert not has_writes(built_plans[0])
    faker.launch_executable.assert_called_once_with(target)
    assert manifest.read_text(encoding="utf-8") == _minimal_acf(123, "Game")


def test_replacement_is_restored_when_normal_launch_fails(tmp_path):
    from _orbfarmer_files import _runtime_for_tests
    from orbfarmer.steam import run_steam_library_session

    steam_root = tmp_path / "Steam"
    steamapps = steam_root / "steamapps"
    target = steamapps / "common" / "Game" / "Game.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original executable")
    faker = MagicMock()
    faker.choose_executable_action.return_value = "replace"
    faker.begin_scope.return_value = object()
    faker.launch_executable.return_value = False

    with _runtime_for_tests(
        executable=b"replacement executable",
        timer_source=b"def run_timer(minutes, theme=None): pass\n",
        frozen=False,
    ), patch("orbfarmer.steam.get_steam_path", return_value=steam_root), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value={
             "name": "Game", "installdir": "Game", "executable": "Game.exe", "depot_id": None,
         }), \
         patch("orbfarmer.artwork.prepare_artwork", return_value=({"name": "Game", "accent": "#aac9b5", "steam_appid": 123}, {})), \
         patch("orbfarmer.steam.ask_confirm", return_value=True), \
         patch("orbfarmer.steam.loading_animation"), \
         patch("builtins.input", side_effect=["", ""]), \
         patch("_orbfarmer_elevation._launch_elevated", side_effect=AssertionError("writable temp path requested UAC")) as elevate:
        run_steam_library_session(faker, 123, game_name="Game")

    elevate.assert_not_called()
    faker.launch_executable.assert_called_once_with(target)
    assert target.read_bytes() == b"original executable"
    assert not (target.parent / "_Game_orbfarmer_timer.pyw").exists()
    assert not (steamapps / "appmanifest_123.acf").exists()
    assert not list(target.parent.glob("*.bak"))


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

    with patch("orbfarmer.steam.get_steam_path") as get_steam_path, \
         patch("orbfarmer.steam._pick_steam_game", return_value=game), \
         patch("orbfarmer.steam.fetch_steam_app_info", return_value=info), \
         patch("orbfarmer.steam.generate_appmanifest") as generate_manifest, \
         patch("orbfarmer.config.FAKE_EXE_DIR", "simulations"), \
         patch("orbfarmer.steam.ask_confirm", return_value=True), \
         patch("orbfarmer.steam.loading_animation"), \
         patch("builtins.input", side_effect=["dragon", "", "", ""]):
        steam_quest_mode(faker)

    faker.cleanup_scope.assert_called_once_with(scope)
    fake_exe = tmp_path / "simulations" / "Dragons Dogma 2" / "DD2.exe"
    faker.prepare_executable.assert_called_once_with(fake_exe, game_name=game['name'], steam_appid=2054970)
    faker.launch_executable.assert_called_once_with(faker.prepare_executable.return_value)
    get_steam_path.assert_not_called()
    generate_manifest.assert_not_called()
    assert manifest.read_text(encoding="utf-8") == "manifest"

