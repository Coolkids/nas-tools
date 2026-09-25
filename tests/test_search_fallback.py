# -*- coding: utf-8 -*-
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse


ROOT_PATH = Path(__file__).resolve().parents[1]
os.environ.setdefault("NASTOOL_CONFIG", str(ROOT_PATH / "config" / "config.yaml"))

from app.media.media import Media
from app.media.meta import MetaInfo
from app.utils import TmdbWebSearchCache
from app.utils.types import MediaType


class SearchFallbackTest(TestCase):
    def test_mixed_title_keeps_numbers_and_aliases(self):
        meta_info = MetaInfo("小丑 2 Joker 2 2024 1080p")

        self.assertEqual("小丑 2", meta_info.cn_name)
        self.assertEqual("Joker 2", meta_info.en_name)
        self.assertEqual("小丑 2", meta_info.get_name())
        self.assertEqual(
            ["小丑 2", "Joker 2"],
            Media._Media__get_search_names(meta_info)
        )

    def test_number_prefix_is_kept_for_both_title_aliases(self):
        meta_info = MetaInfo("3体 3 Body Problem 2024 S01E01")

        self.assertEqual("3体", meta_info.cn_name)
        self.assertEqual("3 Body Problem", meta_info.en_name)
        self.assertEqual("3体", meta_info.get_name())

    def test_anime_title_keeps_a_leading_number(self):
        meta_info = MetaInfo(
            "[Nekomoe kissaten&LoliHouse] 20 Seiki Denki Mokuroku - 10 "
            "[WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
        )

        self.assertEqual("20 Seiki Denki Mokuroku", meta_info.en_name)
        self.assertEqual("20 Seiki Denki Mokuroku", meta_info.get_name())
        self.assertEqual(["20 Seiki Denki Mokuroku"], meta_info.alternative_names)
        self.assertEqual("E10", meta_info.get_episode_string())

    def test_anime_unique_tmdb_result_is_used_after_exact_match_fails(self):
        meta_info = MetaInfo(
            "[Nekomoe kissaten&LoliHouse] 20 Seiki Denki Mokuroku - 10 "
            "[WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
        )
        media = object.__new__(Media)
        media._Media__search_tmdb = Mock(return_value={})
        media._Media__search_multi_tmdb = Mock()
        media.search = SimpleNamespace(tv_shows=Mock(return_value=[{
            "id": 153217,
            "name": "二十世纪电气目录"
        }]))

        result = media._Media__search_by_title_aliases(meta_info)

        self.assertEqual(153217, result["id"])
        self.assertEqual(MediaType.TV, result["media_type"])
        media.search.tv_shows.assert_called_once_with({"query": "20 Seiki Denki Mokuroku"})

    def test_anime_non_unique_tmdb_results_are_not_used(self):
        meta_info = MetaInfo(
            "[Nekomoe kissaten&LoliHouse] 20 Seiki Denki Mokuroku - 10 "
            "[WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
        )
        media = object.__new__(Media)
        media._Media__search_tmdb = Mock(return_value={})
        media._Media__search_multi_tmdb = Mock()
        media.search = SimpleNamespace(tv_shows=Mock(return_value=[
            {"id": 1, "name": "候选一"},
            {"id": 2, "name": "候选二"}
        ]))

        self.assertIsNone(media._Media__search_by_title_aliases(meta_info))

    def test_anime_title_aliases_are_tried_by_normal_tmdb_search(self):
        meta_info = MetaInfo(
            "[喵萌奶茶屋&LoliHouse] 二十世纪电气目录 / 20 Seiki Denki Mokuroku / "
            "Nijusseiki Denki Mokuroku - 10 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
        )
        media = object.__new__(Media)
        media._Media__search_tmdb = Mock(return_value={"id": 204046})
        media._Media__search_multi_tmdb = Mock()

        result = media._Media__search_by_title_aliases(meta_info)

        self.assertEqual(["Nijusseiki Denki Mokuroku", "二十世纪电气目录", "20 Seiki Denki Mokuroku"],
                         Media._Media__get_search_names(meta_info))
        self.assertEqual({"id": 204046}, result)
        media._Media__search_tmdb.assert_called_once_with(
            file_media_name="二十世纪电气目录", search_type=MediaType.TV
        )
        self.assertEqual(
            "[电视剧]二十世纪电气目录|20 Seiki Denki Mokuroku|Nijusseiki Denki Mokuroku-None-None",
            Media._Media__make_cache_key(meta_info)
        )

    def test_episode_number_is_not_treated_as_a_title_number(self):
        meta_info = MetaInfo("剧名 01 1080p")

        self.assertEqual("剧名", meta_info.cn_name)
        self.assertEqual("E01", meta_info.get_episode_string())

    def test_fallback_tries_the_complete_chinese_and_english_aliases(self):
        meta_info = MetaInfo("小丑 2 Joker 2 2024 1080p")
        media = object.__new__(Media)
        media._search_tmdbweb = True
        searched_names = []

        def search_tmdb_web(file_media_name, mtype):
            searched_names.append((file_media_name, mtype))
            return {"id": 475557} if file_media_name == "Joker 2" else None

        media._Media__search_tmdb_web = search_tmdb_web

        result = media._Media__search_fallback(meta_info, mtype=MediaType.MOVIE)

        self.assertEqual({"id": 475557}, result)
        self.assertEqual(
            [("小丑 2", MediaType.MOVIE), ("Joker 2", MediaType.MOVIE)],
            searched_names
        )

    def test_tmdb_web_search_allows_chinese_and_encodes_the_query(self):
        calls = []
        media = object.__new__(Media)
        media.get_tmdb_info = Mock(return_value={
            "id": 603,
            "media_type": MediaType.MOVIE,
            "title": "黑客帝国"
        })

        class FakeRequestUtils:
            def __init__(self, timeout):
                self.timeout = timeout

            def get_res(self, url):
                calls.append(url)
                return SimpleNamespace(
                    status_code=200,
                    text='<html><body><a data-id="603" href="/movie/603">The Matrix</a></body></html>'
                )

        query = "黑客帝国 1 & The Matrix"
        cache_key = (query, MediaType.MOVIE.value)
        TmdbWebSearchCache.delete(cache_key)
        try:
            with patch("app.media.media.RequestUtils", FakeRequestUtils):
                result = media._Media__search_tmdb_web(query, MediaType.MOVIE)
        finally:
            TmdbWebSearchCache.delete(cache_key)

        self.assertEqual(603, result["id"])
        self.assertEqual(query, parse_qs(urlparse(calls[0]).query)["query"][0])
        media.get_tmdb_info.assert_called_once_with(mtype=MediaType.MOVIE, tmdbid="603")
