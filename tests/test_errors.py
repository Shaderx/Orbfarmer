"""Tests for errors.py – exception hierarchy."""

import pytest
from orbfarmer.errors import (
    OrbfarmerError,
    NetworkError,
    SteamNotFoundError,
    DatabaseLoadError,
)


def test_network_error_is_orbfarmer_error():
    assert issubclass(NetworkError, OrbfarmerError)


def test_steam_not_found_is_orbfarmer_error():
    assert issubclass(SteamNotFoundError, OrbfarmerError)


def test_database_load_error_is_orbfarmer_error():
    assert issubclass(DatabaseLoadError, OrbfarmerError)


def test_can_catch_all_with_base():
    with pytest.raises(OrbfarmerError):
        raise NetworkError("test")
