from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.subscribe import Subscribe
from app.utils.types import MediaType


@pytest.mark.parametrize(("download_setting", "expected"), [("", None), ("7", 7)])
def test_add_tv_subscription_normalizes_download_setting(download_setting, expected):
    media_info = SimpleNamespace(
        type=MediaType.TV,
        tmdb_info={"id": 123},
        title="Test series",
        begin_season=None,
        total_episodes=12,
        overview="Test overview",
    )
    media = Mock()
    media.get_media_info_original_title.return_value = media_info
    media.get_tmdb_season_episodes_num.return_value = 12
    dbhelper = Mock()
    dbhelper.insert_rss_tv.return_value = 0
    subscribe = Subscribe.__new__(Subscribe)
    subscribe.media = media
    subscribe.dbhelper = dbhelper

    with patch.object(Subscribe, "gen_rss_note", return_value="{}"):
        code, _, _ = subscribe.add_rss_subscribe(
            mtype=MediaType.TV,
            name="Test series",
            year="2025",
            season="1",
            download_setting=download_setting,
        )

    assert code == 0
    assert dbhelper.insert_rss_tv.call_args.kwargs["download_setting"] == expected
