"""
Custom exceptions for orbfarmer.

Keep it small — one base class + a few specific ones.
"""


class OrbfarmerError(Exception):
    """Base error for all orbfarmer errors."""


class NetworkError(OrbfarmerError):
    """An API / HTTP call failed."""


class SteamNotFoundError(OrbfarmerError):
    """Steam installation could not be located."""


class DatabaseLoadError(OrbfarmerError):
    """Failed to load the games database from any source."""
