from types import SimpleNamespace
from unittest.mock import patch

from app.utils import Torrent
from web.action import WebAction


def test_magnet_only_download_command_accepts_missing_files_list():
    calls = []

    class FixtureMedia:
        title = "test magnet"

        def set_torrent_info(self, **kwargs):
            self.torrent_info = kwargs

    def fake_download(self, media_info, **kwargs):
        calls.append((media_info, kwargs))
        return True, ""

    class FixtureDownloader:
        download = fake_download

    with patch("web.action.Media", return_value=SimpleNamespace(
            get_media_info=lambda **kwargs: FixtureMedia())), \
            patch("web.action.Downloader", return_value=FixtureDownloader()), \
            patch("web.action.Message", return_value=SimpleNamespace(
                send_download_message=lambda *args, **kwargs: None)), \
            patch.object(Torrent, "get_magnet_title", return_value="test magnet"), \
            patch("web.action.current_user", SimpleNamespace(username="tester")):
        result = WebAction._WebAction__download_torrent({
            "magnets": ["magnet:?xt=urn:btih:test"], "files": None,
        })

    assert result["code"] == 0
    assert len(calls) == 1
    assert calls[0][1]["torrent_file"] is None
