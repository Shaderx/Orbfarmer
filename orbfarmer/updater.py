"""
updater.py – Safe, notification-only release checks.

The application never downloads, replaces, or executes release assets. A
secure binary updater requires signed release metadata and a pinned publisher
key; HTTPS and an unsigned checksum alone are not sufficient.
"""

from typing import TypedDict

import requests
from packaging.version import InvalidVersion, Version

from . import config, ui


class GitHubRelease(TypedDict, total=False):
    tag_name: str
    html_url: str


def _parse_version(tag: str) -> Version:
    """Parse a version tag like ``v2.1.0`` or ``2.1.0``."""
    return Version(tag.lstrip("v"))


def _get_latest_release() -> GitHubRelease | None:
    """Fetch minimal release metadata from the configured repository."""
    url = (
        "https://api.github.com/repos/"
        f"{config.GITHUB_REPO_OWNER}/{config.GITHUB_REPO_NAME}/releases/latest"
    )
    try:
        response = requests.get(url, timeout=config.REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return None
        return data
    except (requests.RequestException, ValueError):
        return None


def auto_update() -> None:
    """Report an available release without downloading or executing it."""
    release = _get_latest_release()
    if not release:
        return

    tag = release.get("tag_name", "")
    try:
        remote_version = _parse_version(tag)
        local_version = _parse_version(config.VERSION)
    except InvalidVersion:
        return

    if remote_version <= local_version:
        return

    release_url = release.get("html_url", "")
    expected_prefix = f"{config.REPO_URL}/releases/"
    if not isinstance(release_url, str) or not release_url.startswith(expected_prefix):
        release_url = f"{config.REPO_URL}/releases/latest"

    ui.print_color(
        f"[UPDATE] Version {tag} is available; automatic installation is disabled.",
        ui.Colors.YELLOW,
        bold=True,
    )
    ui.print_color(f"[UPDATE] Review it manually: {release_url}", ui.Colors.GRAY)
