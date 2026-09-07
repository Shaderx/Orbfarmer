"""Security-focused tests for notification-only update checks."""

from unittest.mock import MagicMock, patch

from orbfarmer import config
from orbfarmer.updater import _get_latest_release, auto_update


def test_release_check_uses_the_audited_repository():
    response = MagicMock()
    response.json.return_value = {"tag_name": "v9.9.9"}
    with patch("orbfarmer.updater.requests.get", return_value=response) as request:
        _get_latest_release()

    request.assert_called_once_with(
        "https://api.github.com/repos/Shaderx/Orbfarmer/releases/latest",
        timeout=config.REQUEST_TIMEOUT,
    )


def test_new_release_is_notification_only_and_rejects_foreign_url():
    release = {
        "tag_name": "v999.0.0",
        "html_url": "https://evil.example/payload.exe",
        "assets": [{"name": "payload.exe", "browser_download_url": "https://evil.example/payload.exe"}],
    }
    with patch("orbfarmer.updater._get_latest_release", return_value=release), \
         patch("orbfarmer.updater.ui.print_color") as output:
        auto_update()

    rendered = " ".join(str(call.args[0]) for call in output.call_args_list)
    assert "automatic installation is disabled" in rendered
    assert "https://github.com/Shaderx/Orbfarmer/releases/latest" in rendered
    assert "evil.example" not in rendered
