# -*- coding: utf-8 -*-
import os
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.recognition.records import current_recorder, recognition_scope
from app.utils.types import MatchMode, MediaType


class FakeMetaInfo:
    def __init__(self, name=None, year=None, media_type=None, season=None):
        self.cn_name = None
        self.en_name = name
        self.year = year
        self.type = media_type
        self.begin_season = season
        self.begin_episode = None
        self.end_episode = None
        self.tmdb_info = None

    def get_name(self):
        return self.cn_name or self.en_name

    def set_tmdb_info(self, result):
        self.tmdb_info = result


class MediaParentFallbackTest(TestCase):
    def _media(self):
        media = object.__new__(Media)
        media.tmdb = object()
        media.meta = Mock()
        media.meta.get_meta_data_by_key.return_value = None
        media._rmt_match_mode = MatchMode.NORMAL
        media._search_tmdbweb = False
        media._ai_inference = False
        media._ai_inference_url = None
        return media

    def test_unparsed_file_uses_parent_then_grandparent_and_records_each_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            series = os.path.join(directory, "Series.2020")
            season = os.path.join(series, "Season 01")
            os.makedirs(season)
            file_path = os.path.join(season, "episode.mkv")
            with open(file_path, "w", encoding="utf-8"):
                pass

            parsed = {
                "episode.mkv": FakeMetaInfo(),
                "Season 01": FakeMetaInfo(),
                "Series.2020": FakeMetaInfo("Series", "2020", MediaType.TV, 1),
            }
            media = self._media()
            tmdb = {"id": 7, "media_type": MediaType.TV, "name": "Series",
                    "first_air_date": "2020-01-01", "genres": ["Drama"]}
            database = Mock()
            database.insert_recognition_record.side_effect = lambda record: record["request_id"]

            with patch("app.media.media.MetaInfo", side_effect=lambda title, **kwargs: parsed[title]), \
                    patch.object(media, "_Media__make_cache_key", return_value="series-key"), \
                    patch.object(media, "_Media__search_tmdb", return_value=tmdb), \
                    patch.object(media, "_Media__insert_media_cache"), \
                    patch.object(media, "_Media__tmdb_title_evidence",
                                 return_value={"level": "strong", "matched_names": []}), \
                    patch.object(Media, "_Media__meta_snapshot", return_value={"name": "Series"}), \
                    patch.object(Media, "_Media__json_safe", side_effect=lambda value: value), \
                    patch("app.media.media.recognition_config", side_effect=lambda section: {
                        "laboratory": {"ai_inference": False},
                        "recognition": {"decision": {"strategy": "legacy"}},
                    }.get(section, {})), \
                    patch("app.media.recognition.records._write_spool", return_value=None), \
                    patch("app.helper.db_helper.DbHelper", return_value=database):
                result = media.get_media_info_on_files(file_path)

            resolved = result[file_path]
            self.assertEqual("Series", resolved.get_name())
            self.assertEqual("2020", resolved.year)
            self.assertEqual(MediaType.TV, resolved.type)
            self.assertEqual(1, resolved.begin_season)
            payload = database.insert_recognition_record.call_args.args[0]
            actions = [action for action in payload["actions"]
                       if action["action_type"] == "fallback"]
            self.assertEqual(["file_name_insufficient", "parent_name_insufficient"],
                             [action["reason"] for action in actions])
            self.assertEqual("success", payload["overall_result"]["status"])
            self.assertEqual("episode.mkv", payload["original_name"])

    def test_ambiguous_ai_result_stops_before_parent_fallback(self):
        media = self._media()
        with tempfile.TemporaryDirectory() as directory:
            series = os.path.join(directory, "Series.2020")
            season = os.path.join(series, "Season 01")
            os.makedirs(season)
            file_path = os.path.join(season, "episode.mkv")
            with open(file_path, "w", encoding="utf-8"):
                pass
            media._ai_inference = True
            media._ai_inference_url = "http://ai.local"
            database = Mock()
            database.insert_recognition_record.side_effect = lambda record: record["request_id"]

            def ambiguous_result(*args, **kwargs):
                current_recorder().context["decision_reason"] = "ambiguous_tmdb"
                return None

            with patch.object(media, "get_media_info", side_effect=ambiguous_result), \
                    patch("app.media.media.recognition_config", side_effect=lambda section: {
                        "laboratory": {"ai_inference": True, "ai_inference_url": "http://ai.local"},
                        "recognition": {},
                    }.get(section, {})), \
                    patch("app.media.media.MetaInfo") as meta_info, \
                    patch("app.media.recognition.records._write_spool", return_value=None), \
                    patch("app.helper.db_helper.DbHelper", return_value=database):
                with recognition_scope("episode.mkv", source="test", stage="resolve") as recorder:
                    result = media._get_media_info_on_files_impl(file_path)
                    fallback_actions = [action for action in recorder.actions
                                        if action["action_type"] == "fallback"]

            self.assertEqual({}, result)
            self.assertEqual([], fallback_actions)
            meta_info.assert_not_called()

    def test_public_file_resolution_records_supplied_tmdb_and_season_overrides(self):
        media = self._media()
        tmdb_info = {"id": 77, "media_type": MediaType.TV, "name": "Supplied Show"}
        episode_format = Mock()
        episode_format.split_episode.return_value = (3, 5)
        parsed_meta = FakeMetaInfo("Supplied Show", media_type=MediaType.TV)
        database = Mock()
        database.insert_recognition_record.side_effect = lambda record: record["request_id"]
        config = Mock()
        config.get_config.return_value = {"recognition": {}}
        config.get_config_path.return_value = "/tmp/nas-tools-parent-fallback-test"

        with tempfile.TemporaryDirectory() as directory:
            file_path = os.path.join(directory, "episode.mkv")
            with open(file_path, "w", encoding="utf-8"):
                pass
            with patch("app.media.media.MetaInfo", return_value=parsed_meta) as meta_factory, \
                    patch.object(media, "save_rename_cache"), \
                    patch.object(Media, "_Media__meta_snapshot", return_value={"name": "Supplied Show"}), \
                    patch.object(Media, "_Media__json_safe", side_effect=lambda value: value), \
                    patch("config.Config", return_value=config), \
                    patch("app.media.recognition.records._write_spool", return_value=None), \
                    patch("app.helper.db_helper.DbHelper", return_value=database):
                result = media.get_media_info_on_files(
                    file_path, tmdb_info=tmdb_info, media_type=MediaType.TV,
                    season=2, episode_format=episode_format)

        self.assertIn(file_path, result)
        self.assertEqual(2, result[file_path].begin_season)
        self.assertEqual(3, result[file_path].begin_episode)
        self.assertEqual(5, result[file_path].end_episode)
        self.assertEqual("episode.mkv", meta_factory.call_args.kwargs["title"])
        payload = database.insert_recognition_record.call_args.args[0]
        self.assertEqual("episode.mkv", payload["original_name"])
        self.assertTrue(payload["context"]["tmdb_info_supplied"])
        self.assertEqual("success", payload["overall_result"]["status"])
        supplied_action = next(action for action in payload["actions"]
                               if action["action_type"] == "provided_tmdb")
        self.assertEqual({"id": 77, "media_type": MediaType.TV.value,
                          "name": "Supplied Show"}, supplied_action["input"]["tmdb_info"])
