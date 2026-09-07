"""Artwork is optional, data-only, and follows simulation file ownership."""
from io import BytesIO
from unittest.mock import patch

from PIL import Image
import pytest

from orbfarmer.artwork import _asset_url, prepare_artwork
from orbfarmer.errors import NetworkError
from orbfarmer.faker import GameFaker


def test_missing_steam_mapping_never_uses_network():
    with patch("orbfarmer.artwork.fetch_json") as fetch, patch("orbfarmer.artwork._download_image") as download:
        theme, assets = prepare_artwork("Manual game")
    assert theme["name"] == "Manual game"
    assert not assets
    fetch.assert_not_called()
    download.assert_not_called()


def test_offline_artwork_keeps_named_fallback():
    with patch("orbfarmer.artwork.fetch_json", side_effect=NetworkError("offline")), \
         patch("orbfarmer.artwork._download_image", return_value=None):
        theme, assets = prepare_artwork("Game", 2054970)
    assert theme["name"] == "Game"
    assert assets == {}


@pytest.mark.parametrize("path", ["https://elsewhere/image.jpg", "../secret", "/image.jpg", "folder/../image.jpg", "a?b"])
def test_metadata_cannot_redirect_artwork_download(path):
    assert _asset_url(2054970, path) is None


def test_hashed_hero_and_icon_create_bounded_pngs():
    common = {"icon": "a" * 40, "library_assets_full": {"library_hero": {"image": {"english": "abc123/library_hero.jpg"}}}}
    with patch("orbfarmer.artwork.fetch_json", return_value={"data": {"123": {"common": common}}}), \
         patch("orbfarmer.artwork._download_image", return_value=Image.new("RGB", (900, 400), "#af6522")) as download:
        theme, assets = prepare_artwork("Game", 123)
    assert "abc123/library_hero.jpg" in download.call_args_list[0].args[0]
    assert "a" * 40 in download.call_args_list[1].args[0]
    assert Image.open(BytesIO(assets["hero"])).size == (820, 320)
    assert Image.open(BytesIO(assets["icon"])).size == (64, 64)
    assert theme["accent"] != "#aac9b5"


def test_artwork_collision_rolls_back_only_new_files(tmp_path):
    source = tmp_path / "pythonw.exe"
    source.write_bytes(b"source")
    faker = GameFaker()
    faker._source_exe = source
    target = tmp_path / "Game.exe"
    icon = tmp_path / "_Game_icon.png"
    icon.write_bytes(b"existing")
    with patch("orbfarmer.faker.prepare_artwork", return_value=({"name": "Game"}, {"hero": b"hero", "icon": b"icon"})):
        with pytest.raises(FileExistsError):
            faker.copy_exe_to(target, game_name="Game", steam_appid=123)
    assert not target.exists()
    assert not (tmp_path / "_Game_hero.png").exists()
    assert icon.read_bytes() == b"existing"


def test_scoped_cleanup_owns_artwork_and_preserves_changes(tmp_path):
    source = tmp_path / "pythonw.exe"
    source.write_bytes(b"source")
    faker = GameFaker()
    faker._source_exe = source
    scope = faker.begin_scope()
    target = tmp_path / "Game.exe"
    with patch("orbfarmer.faker.prepare_artwork", return_value=({"name": "Game"}, {"hero": b"hero", "icon": b"icon"})):
        faker.copy_exe_to(target, game_name="Game", steam_appid=123)
    (tmp_path / "_Game_icon.png").write_bytes(b"changed")
    faker.cleanup_scope(scope)
    assert not target.exists()
    assert not (tmp_path / "_Game_hero.png").exists()
    assert (tmp_path / "_Game_icon.png").read_bytes() == b"changed"
