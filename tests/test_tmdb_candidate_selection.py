from unittest import TestCase
from unittest.mock import patch

from app.media.media import Media
from app.media.tmdbv3api.as_obj import AsObj
from app.media.recognition import cache as recognition_cache
from app.media.recognition.records import recognition_scope
from app.utils.types import MatchMode, MediaType


class _Search:
    total_results = 0

    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error

    def tv_shows(self, _params):
        if self.error:
            raise self.error
        self.total_results = len(self.results)
        return self.results


class TmdbCandidateSelectionTest(TestCase):
    def setUp(self):
        self.media = object.__new__(Media)
        self.media.tmdb = object()
        self.media.search = _Search()
        self.media._tmdb_clients = []
        self.media._rmt_match_mode = MatchMode.NORMAL

    def _resolve(self, title, results=None, error=None, aliases=None, parsed_season=None,
                 parsed_episode=None, tmdb_details=None):
        self.media.search = _Search(results, error)
        tmdb_details = tmdb_details or {}

        def get_tmdb_details(mtype=None, tmdbid=None, **_kwargs):
            return tmdb_details.get(tmdbid) or tmdb_details.get(str(tmdbid))

        config = {"recognition": {"decision": {"title_evidence": {
            "name_sources": ["primary", "original", "alternative", "translation"],
            "normalization": ["nfkc", "casefold", "simplified_chinese",
                              "separators", "whitespace"],
            "require_latin_word_boundary": True,
            "min_latin_chars_for_strong": 4,
            "min_cjk_chars_for_strong": 3,
        }}}, "laboratory": {}}
        with patch("app.media.recognition.records._runtime_config_snapshot",
                   return_value=config), \
                patch("app.media.recognition.records._recognition_config_snapshot",
                      return_value=("candidate-selection-test", {"recognition": {}, "runtime": {}})), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper"), \
                patch.object(recognition_cache, "get", return_value=recognition_cache.CACHE_MISS), \
                patch.object(recognition_cache, "put", return_value=None), \
                patch.object(self.media, "get_tmdb_info", side_effect=get_tmdb_details), \
                patch.object(self.media, "_Media__search_tmdb_allnames",
                             return_value=(None, aliases or [])) as alias_lookup:
            with recognition_scope(title, source="candidate-test") as recorder:
                parsed_meta = type("ParsedMeta", (), {
                    "begin_season": parsed_season,
                    "begin_episode": parsed_episode,
                })()
                self.media._Media__set_active_parser_season(parsed_meta)
                result = self.media._Media__search_tmdb(
                    "Parsed Name", MediaType.TV)
        return result, recorder, alias_lookup

    def test_unique_title_match_wins_from_multiple_search_results(self):
        result, recorder, _ = self._resolve(
            "The Alpha Show S01E01 1080p.mkv",
            results=[
                {"id": 1, "name": "Unrelated Show", "original_name": "Unrelated Show"},
                {"id": 2, "name": "Alpha Show", "original_name": "Alpha Show"},
            ])

        self.assertEqual(2, result["id"])
        selection = next(action for action in recorder.actions
                         if action["action_type"] == "tmdb_candidate_selection")
        self.assertEqual(2, selection["output"]["selected_id"])
        self.assertEqual(1, selection["output"]["pending_count"])
        self.assertEqual("success", recorder.tmdb_results[-1]["status"])

    def test_tmdb_as_obj_search_rows_are_not_discarded(self):
        result, recorder, _ = self._resolve(
            "Grand Blue 2025 S02E02-[1080p][BDRIP][x265.OPUS].mkv",
            results=[AsObj(id=79166, name="碧蓝之海", original_name="ぐらんぶる")],
            aliases=["Grand Blue", "Grand Blue Dreaming"],
        )

        self.assertEqual(79166, result["id"])
        selection = next(action for action in recorder.actions
                         if action["action_type"] == "tmdb_candidate_selection")
        self.assertEqual(1, selection["output"]["candidate_count"])
        self.assertEqual(1, selection["output"]["pending_count"])
        self.assertEqual("success", recorder.tmdb_results[-1]["status"])

    def test_episode_suffix_does_not_block_anime_tmdb_candidate_without_parsed_season(self):
        result, recorder, _ = self._resolve(
            "[Nekomoe kissaten&LoliHouse] Medalist - 14 "
            "[WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
            results=[{"id": 1, "name": "Medalist"}],
            parsed_season=None)

        self.assertEqual(1, result["id"])
        selection = next(action for action in recorder.actions
                         if action["action_type"] == "tmdb_candidate_selection")
        self.assertEqual(1, selection["output"]["pending_count"])
        self.assertEqual("success", recorder.tmdb_results[-1]["status"])

    def test_explicit_anitopy_season_can_mark_a_matching_numeric_suffix_as_sequel(self):
        result, recorder, _ = self._resolve(
            "Example Story 2 2024 1080p.mkv",
            results=[{"id": 1, "name": "Example Story"}],
            parsed_season=2)

        self.assertEqual({}, result)
        selection = next(action for action in recorder.actions
                         if action["action_type"] == "tmdb_candidate_selection")
        self.assertEqual(0, selection["output"]["pending_count"])
        self.assertTrue(selection["output"]["examined"][0]["evidence"]["weak_matches"][0]["sequel_prefix"])
        self.assertEqual("no_tmdb_match", recorder.tmdb_results[-1]["status"])

    def test_tmdb_as_obj_aliases_are_read_from_detail_response(self):
        detail = AsObj(
            alternative_titles=AsObj(results=[AsObj(title="Grand Blue")]),
            translations=AsObj(translations=[AsObj(data=AsObj(name="Grand Blue Dreaming"))]),
        )
        with patch.object(self.media, "get_tmdb_info", return_value=detail):
            _, aliases = self.media._Media__search_tmdb_allnames(
                MediaType.TV, 79166, alias_sources={"alternative", "translation"})

        self.assertEqual(["Grand Blue", "Grand Blue Dreaming"], aliases)

    def test_anime_category_uses_tv_search_instead_of_multi_search(self):
        class Meta:
            type = MediaType.ANIME
            year = None
            begin_season = 2
            tmdb_info = None

            @staticmethod
            def get_name():
                return "Grand Blue"

            def set_tmdb_info(self, value):
                self.tmdb_info = value

        meta = Meta()
        tmdb_result = {"id": 79166, "name": "碧蓝之海", "media_type": MediaType.TV,
                       "genres": [{"id": 16, "name": "动画"}]}
        with patch.object(self.media, "_Media__make_cache_key", return_value="meta-key"), \
                patch.object(self.media, "_Media__search_tmdb", return_value=tmdb_result) as tv_search, \
                patch.object(self.media, "_Media__search_multi_tmdb",
                             side_effect=AssertionError("anime must use TMDB TV search")) as multi_search, \
                patch.object(self.media, "_Media__insert_media_cache"), \
                patch("app.media.media.recognition_config", side_effect=lambda section: {
                    "recognition": {"decision": {}}, "laboratory": {},
                }.get(section, {})), \
                patch.object(recognition_cache, "cache_key", return_value="query-key"), \
                patch.object(recognition_cache, "get", return_value=recognition_cache.CACHE_MISS), \
                patch.object(recognition_cache, "put", return_value=None):
            result = self.media._Media__search_meta_tmdb(meta, cache=False)

        self.assertIs(tmdb_result, result)
        self.assertIs(MediaType.ANIME, meta.type)
        tv_search.assert_called_once_with(file_media_name="Grand Blue", first_media_year=None,
                                          search_type=MediaType.TV, media_year=None,
                                          season_number=2)
        multi_search.assert_not_called()

    def test_alias_match_is_considered_and_multiple_matches_fail(self):
        result, recorder, alias_lookup = self._resolve(
            "Alpha Alias and Beta Show 1080p.mkv",
            results=[
                {"id": 10, "name": "Alpha Primary", "original_name": "Alpha Primary"},
                {"id": 20, "name": "Beta Show", "original_name": "Beta Show"},
            ], aliases=["Alpha Alias"])

        self.assertFalse(result)
        alias_lookup.assert_called_once_with(MediaType.TV, 10,
                                             alias_sources={"alternative", "translation"})
        self.assertEqual("ambiguous_tmdb", recorder.context["decision_reason"])
        self.assertEqual("ambiguous_tmdb", recorder.tmdb_results[-1]["status"])
        selection = next(action for action in recorder.actions
                         if action["action_type"] == "tmdb_candidate_selection")
        self.assertEqual(2, selection["output"]["pending_count"])

    def test_episode_number_filters_candidates_by_total_tmdb_episode_count(self):
        result, recorder, _ = self._resolve(
            "Kusuriya no Hitorigoto 薬屋少女の呢喃 44 WebRip.mkv",
            results=[
                {"id": 220542, "name": "药屋少女的呢喃"},
                {"id": 333686, "name": "薬屋のひとりごと"},
            ],
            aliases=["Kusuriya no Hitorigoto"],
            parsed_episode=44,
            tmdb_details={
                220542: AsObj(number_of_episodes=60, seasons=[]),
                333686: AsObj(number_of_episodes=1, seasons=[]),
            })

        self.assertEqual(220542, result["id"])
        selection = next(action for action in recorder.actions
                         if action["action_type"] == "tmdb_candidate_selection")
        self.assertEqual([220542], selection["output"]["pending_ids"])
        filter_result = selection["output"]["season_episode_filter"]
        self.assertEqual(44, filter_result["episode"])
        self.assertEqual([220542, 333686], filter_result["before_ids"])
        self.assertEqual([220542], filter_result["after_ids"])
        self.assertEqual("mismatch", filter_result["candidates"][1]["status"])

    def test_season_and_episode_filter_uses_the_matching_season_count(self):
        result, recorder, _ = self._resolve(
            "Example Show S02E08 1080p.mkv",
            results=[
                {"id": 1, "name": "Example Show"},
                {"id": 2, "name": "Example Show"},
            ],
            parsed_season=2,
            parsed_episode=8,
            tmdb_details={
                1: AsObj(seasons=[AsObj(season_number=2, episode_count=12)]),
                2: AsObj(seasons=[AsObj(season_number=1, episode_count=24)]),
            })

        self.assertEqual(1, result["id"])
        selection = next(action for action in recorder.actions
                         if action["action_type"] == "tmdb_candidate_selection")
        self.assertEqual([1], selection["output"]["pending_ids"])
        self.assertEqual("season_not_found",
                         selection["output"]["season_episode_filter"]["candidates"][1]["reason"])

    def test_unavailable_tmdb_episode_data_keeps_name_matched_candidates(self):
        result, recorder, _ = self._resolve(
            "Example Show 44 1080p.mkv",
            results=[
                {"id": 1, "name": "Example Show"},
                {"id": 2, "name": "Example Show"},
            ],
            parsed_episode=44)

        self.assertFalse(result)
        selection = next(action for action in recorder.actions
                         if action["action_type"] == "tmdb_candidate_selection")
        self.assertEqual([1, 2], selection["output"]["pending_ids"])
        self.assertEqual("unknown",
                         selection["output"]["season_episode_filter"]["candidates"][0]["status"])

    def test_empty_results_and_network_error_have_distinct_reasons(self):
        empty_result, empty_recorder, _ = self._resolve("Example Show", results=[])
        network_result, network_recorder, _ = self._resolve(
            "Example Show", error=RuntimeError("connection refused"))

        self.assertEqual({}, empty_result)
        self.assertEqual("tmdb_no_results", empty_recorder.tmdb_results[-1]["status"])
        self.assertEqual("tmdb_no_results", empty_recorder.tmdb_results[-1]["reason"])
        self.assertIsNone(network_result)
        self.assertEqual("tmdb_network_error", network_recorder.tmdb_results[-1]["status"])
        self.assertIn("connection refused", network_recorder.tmdb_results[-1]["reason"])
