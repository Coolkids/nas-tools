from types import SimpleNamespace
import os
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from requests.exceptions import Timeout
from app.media.media import Media
from app.media.recognition import cache as recognition_cache
from app.media.recognition.decision import select_provider_candidates
from app.media.recognition.records import current_recorder
from app.media.tmdbv3api.as_obj import AsObj
from app.utils.types import MatchMode, MediaType


def item(provider, tmdb_id, score=0, *, genres=(16,), adult=False, media_type="tv"):
    info = {"id": tmdb_id, "media_type": media_type, "name": f"Title {tmdb_id}",
            "genre_ids": list(genres), "adult": adult}
    evidence = {"level": "none", "similarity_score": score,
                "similarity_matches": [{"name": info["name"], "score": score}]}
    return ((provider, None, info), evidence)


class ProviderCandidateDecisionTest(TestCase):
    def test_unique_agreement_bypasses_title_threshold(self):
        decision = select_provider_candidates([
            item("local_rules", 1), item("anitopy_ml", 1),
        ])
        self.assertEqual("success", decision["status"])
        self.assertEqual("provider_agreement", decision["selection_method"])
        self.assertEqual(2, decision["agreement_count"])
        self.assertEqual(0, decision["candidate_similarities"][0]["similarity_score"])

    def test_duplicate_rows_in_one_provider_do_not_count_as_agreement(self):
        decision = select_provider_candidates([item("local_rules", 1), item("local_rules", 1)])
        self.assertEqual("insufficient_title_evidence", decision["reason"])

    def test_preferred_genre_can_make_both_routes_unique_and_equal(self):
        decision = select_provider_candidates([
            item("local_rules", 1), item("local_rules", 2, 1, genres=(18,)),
            item("anitopy_ml", 1), item("anitopy_ml", 3, 1, genres=(18,)),
        ])
        self.assertEqual(1, decision["selected"][2]["id"])
        self.assertEqual("priority_provider_agreement", decision["selection_method"])
        self.assertEqual(3, len(decision["candidate_similarities"]))

    def test_non_adult_content_has_priority_over_an_adult_preferred_genre(self):
        decision = select_provider_candidates([
            item("local_rules", 1, 1, adult=True),
            item("local_rules", 2, 0.9, genres=(18,)),
            item("anitopy_ml", 1, 1, adult=True),
            item("anitopy_ml", 3, 0.7, genres=(18,)),
        ])
        self.assertEqual(2, decision["selected"][2]["id"])
        self.assertFalse(next(row for row in decision["candidate_similarities"]
                              if row["id"] == 1)["retained"])

    def test_adult_candidates_remain_available_when_no_non_adult_content_exists(self):
        decision = select_provider_candidates([
            item("local_rules", 1, 0.9, adult=True),
            item("anitopy_ml", 2, 0.5, adult=True),
        ])
        self.assertEqual(1, decision["selected"][2]["id"])

    def test_shared_candidate_is_compared_before_higher_scoring_distinct_candidate(self):
        decision = select_provider_candidates([
            item("local_rules", 1, 0.91), item("local_rules", 2, 1),
            item("anitopy_ml", 1, 0.91), item("anitopy_ml", 3, 0.8),
        ])
        self.assertEqual(1, decision["selected"][2]["id"])
        self.assertEqual("shared_similarity", decision["selection_method"])

    def test_distinct_candidates_are_compared_after_shared_candidates_miss_threshold(self):
        decision = select_provider_candidates([
            item("local_rules", 1, 0.5), item("local_rules", 2, 0.96),
            item("anitopy_ml", 1, 0.5), item("anitopy_ml", 3, 0.93),
        ])
        self.assertEqual(2, decision["selected"][2]["id"])
        self.assertEqual("distinct_similarity", decision["selection_method"])

    def test_best_score_wins_without_old_minimum_margin(self):
        decision = select_provider_candidates([
            item("local_rules", 1, 0.95), item("anitopy_ml", 2, 0.94),
        ])
        self.assertEqual(1, decision["selected"][2]["id"])

    def test_fuzzy_fallback_score_participates_in_candidate_selection(self):
        first = item("local_rules", 1, 0.60)
        second = item("anitopy_ml", 2, 0.91)
        first[1]["level"], first[1]["fuzzy_score"] = "fuzzy", 0.93
        second[1]["level"], second[1]["fuzzy_score"] = "fuzzy", 0.91
        decision = select_provider_candidates([first, second])

        self.assertEqual("success", decision["status"])
        self.assertEqual(1, decision["selected"][2]["id"])
        self.assertEqual(0.93, next(row["similarity_score"]
                                    for row in decision["candidate_similarities"]
                                    if row["id"] == 1))

    def test_threshold_is_configurable_and_failure_keeps_all_title_scores(self):
        candidates = [item("local_rules", 1, 0.89), item("anitopy_ml", 2, 0.85)]
        for minimum, status in ((0.9, "failed"), (0.88, "success")):
            with self.subTest(minimum=minimum):
                decision = select_provider_candidates(candidates, minimum_score=minimum)
                self.assertEqual(status, decision["status"])
                self.assertEqual([0.89, 0.85], [row["similarity_score"]
                                             for row in decision["candidate_similarities"]])
                self.assertTrue(all(row["title_similarities"]
                                    for row in decision["candidate_similarities"]))

    def test_equal_highest_scores_fail_instead_of_using_provider_order(self):
        candidates = [item("local_rules", 1, 0.95), item("anitopy_ml", 2, 0.95)]
        for values in (candidates, list(reversed(candidates))):
            self.assertEqual("ambiguous_tmdb", select_provider_candidates(values)["reason"])

    def test_a_shared_tie_does_not_fall_through_to_distinct_candidates(self):
        candidates = [item(provider, tmdb_id, 0.95)
                      for provider in ("local_rules", "anitopy_ml") for tmdb_id in (1, 2)]
        candidates.append(item("local_rules", 3, 1))
        self.assertEqual("ambiguous_tmdb", select_provider_candidates(candidates)["reason"])

    def test_ids_are_scoped_by_media_type(self):
        decision = select_provider_candidates([
            item("local_rules", 1, media_type="tv"),
            item("anitopy_ml", 1, media_type="movie"),
        ])
        self.assertEqual("failed", decision["status"])
        self.assertEqual(2, len(decision["candidate_similarities"]))

    def test_invalid_entities_and_no_results_have_specific_reasons(self):
        self.assertEqual("no_tmdb_match", select_provider_candidates([])["reason"])
        self.assertEqual("invalid_tmdb_entity", select_provider_candidates([
            item("local_rules", None)])["reason"])

    def test_preference_can_be_disabled(self):
        decision = select_provider_candidates([
            item("local_rules", 1, 0.89), item("anitopy_ml", 2, 1, genres=(18,)),
        ], preferred_genre_id=0)
        self.assertEqual(2, decision["selected"][2]["id"])


class _Search:
    def __init__(self, results, pages=1):
        self.results = {name.casefold(): rows for name, rows in results.items()}
        self.total_pages = pages
        self.queries = []

    def tv_shows(self, params):
        self.queries.append(dict(params))
        result = self.results.get(params["query"].casefold(), [])
        if self.total_pages > 1:
            return result[params.get("page", 1) - 1:params.get("page", 1)]
        return result


class ProviderCandidateFlowTest(TestCase):
    def _resolve(self, title, local_results, ml_results=None, *, ai_title="ML Query",
                 threshold=0.88, aliases=None, pages=1, file_transfer=False):
        from app.media.meta.metaanime import MetaAnime

        local_query = MetaAnime(title).get_name()
        search = _Search({local_query: local_results, ai_title: ml_results or []}, pages)
        media = object.__new__(Media)
        media.tmdb = object()
        media.search = search
        media._tmdb_clients = []
        media._rmt_match_mode = MatchMode.NORMAL
        media._search_tmdbweb = False
        runtime = {
            "recognition": {
                "records": {"mode": "all"},
                "decision": {"strategy": "title_evidence", "title_evidence": {
                    "preferred_genre_id": 16, "fuzzy_min_score": threshold}},
                "providers": {"anitopy_ml": {"enabled": ml_results is not None,
                                             "endpoint": "http://ml.test"}},
            }, "laboratory": {},
        }

        def parse_ai(_title, **_kwargs):
            if ml_results is None:
                return None
            result = {"title": ai_title, "type": "anime", "episodes": [6]}
            current_recorder().add_provider_result("anitopy_ml", "success", normalized_result=result)
            return result

        details = {}
        for row in local_results + (ml_results or []):
            info = dict(row.items())
            details[info["id"]] = {
                **info, "genres": [{"id": genre} for genre in info.get("genre_ids", [16])],
                "media_type": MediaType.TV,
                "alternative_titles": {"results": [{"title": name}
                    for name in (aliases or {}).get(info["id"], [])]},
            }
        database = Mock()
        database.insert_recognition_record.side_effect = lambda record: record["request_id"]
        with patch("app.media.recognition.records._runtime_config_snapshot", return_value=runtime), \
                patch("app.media.recognition.records._recognition_config_snapshot", return_value=("test", {})), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database), \
                patch.object(recognition_cache, "get", return_value=recognition_cache.CACHE_MISS), \
                patch.object(recognition_cache, "put"), \
                patch.object(media, "_Media__request_ai_parse", side_effect=parse_ai), \
                patch.object(media, "_Media__select_tmdb_search_candidates",
                             side_effect=AssertionError("must keep the complete provider candidate set")), \
                patch.object(media, "get_tmdb_info", side_effect=lambda tmdbid, **_: details[tmdbid]):
            if file_transfer:
                with tempfile.TemporaryDirectory() as directory:
                    path = os.path.join(directory, title)
                    with open(path, "wb"):
                        pass
                    result = media.get_media_info_on_files(path, media_type=MediaType.ANIME).get(path)
            else:
                result = media.get_media_info(title, mtype=MediaType.ANIME)
        record = database.insert_recognition_record.call_args.args[0]
        selection = next(action["output"] for action in record["actions"]
                         if action["action_type"] == "tmdb_candidate_selection")
        self.assertEqual(selection["candidate_similarities"],
                         record["overall_result"]["candidate_similarities"])
        return result, record, selection, search

    def test_romaji_title_with_a_unique_shared_chinese_result_succeeds(self):
        title = ("[Nekomoe kissaten&LoliHouse] 20 Seiki Denki Mokuroku - 06 "
                 "[WebRip 1080p HEVC-10bit AAC ASSx2].mkv")
        rows = [AsObj(id=153217, name="二十世纪电气目录", genre_ids=[16])]
        result, record, selection, _ = self._resolve(title, rows, rows)
        self.assertEqual(153217, result.tmdb_id)
        self.assertEqual(6, result.begin_episode)
        self.assertEqual("success", record["overall_result"]["status"])
        self.assertEqual("provider_agreement", selection["selection_method"])
        self.assertEqual(0, selection["candidate_similarities"][0]["similarity_score"])
        from app.helper.db_helper import DbHelper
        summary = DbHelper.recognition_summary(record)["overall_result"]
        self.assertEqual(0, summary["candidate_similarities"][0]["similarity_score"])
        self.assertEqual("provider_agreement", summary["selection_method"])
        self.assertNotIn("title_similarities", summary["candidate_similarities"][0])

    def test_priority_agreement_preserves_scores_for_rejected_results(self):
        shared = {"id": 1, "name": "完全不同的名称", "genre_ids": [16]}
        result, _, selection, _ = self._resolve(
            "[Group] Alpha Story - 06 [1080p].mkv",
            [shared, {"id": 2, "name": "Alpha Story", "genre_ids": [18]}],
            [shared, {"id": 3, "name": "Alpha Story", "genre_ids": [18]}])
        self.assertEqual(1, result.tmdb_id)
        self.assertEqual("priority_provider_agreement", selection["selection_method"])
        self.assertEqual(3, len(selection["candidate_similarities"]))
        self.assertTrue(all(row["title_similarities"] for row in selection["candidate_similarities"]))

    def test_file_transfer_does_not_repeat_title_rejection_after_provider_agreement(self):
        title = ("[Nekomoe kissaten&LoliHouse] 20 Seiki Denki Mokuroku - 06 "
                 "[WebRip 1080p HEVC-10bit AAC ASSx2].mkv")
        rows = [{"id": 153217, "name": "二十世纪电气目录", "genre_ids": [16]}]
        result, record, selection, _ = self._resolve(title, rows, rows, file_transfer=True)
        self.assertEqual(153217, result.tmdb_id)
        self.assertEqual("success", record["overall_result"]["status"])
        self.assertEqual("provider_agreement", selection["selection_method"])

    def test_shared_miss_falls_back_to_distinct_candidates_with_recorded_scores(self):
        shared = {"id": 1, "name": "Another Show", "genre_ids": [16]}
        result, _, selection, _ = self._resolve(
            "[Group] Alpha Story - 06 [1080p].mkv",
            [shared, {"id": 2, "name": "Alpha Story", "genre_ids": [16]}],
            [shared, {"id": 3, "name": "Unrelated", "genre_ids": [16]}])
        self.assertEqual(2, result.tmdb_id)
        self.assertEqual("distinct_similarity", selection["selection_method"])
        rows = {row["id"]: row for row in selection["candidate_similarities"]}
        self.assertEqual(["anitopy_ml", "local_rules"], rows[1]["provider_ids"])
        self.assertEqual(1, rows[2]["similarity_score"])

    def test_single_route_uses_alias_similarity_instead_of_claiming_agreement(self):
        result, _, selection, _ = self._resolve(
            "[Group] Alpha Story - 06 [1080p].mkv",
            [{"id": 1, "name": "中文译名", "genre_ids": [16]}], aliases={1: ["Alpha Story"]})
        self.assertEqual(1, result.tmdb_id)
        self.assertEqual("distinct_similarity", selection["selection_method"])
        scores = {value["name"]: value["score"]
                  for value in selection["candidate_similarities"][0]["title_similarities"]}
        self.assertEqual(1, scores["Alpha Story"])
        self.assertEqual(0, scores["中文译名"])

    def test_all_scores_below_threshold_fail_and_remain_in_the_recognition_record(self):
        result, record, selection, _ = self._resolve(
            "[Group] Alpha Story - 06 [1080p].mkv",
            [{"id": 1, "name": "Unrelated", "genre_ids": [16]}],
            [{"id": 2, "name": "Another Show", "genre_ids": [16]}], threshold=0.99)
        self.assertIsNone(result)
        self.assertEqual("insufficient_title_evidence", record["overall_result"]["reason"])
        self.assertEqual(2, len(selection["candidate_similarities"]))
        self.assertTrue(all(row["similarity_score"] < 0.99
                            for row in selection["candidate_similarities"]))

    def test_pagination_prevents_treating_a_first_page_as_a_unique_result(self):
        rows = [{"id": 1, "name": "Another Show", "genre_ids": [16]},
                {"id": 2, "name": "Alpha Story", "genre_ids": [16]}]
        result, _, selection, search = self._resolve(
            "[Group] Alpha Story - 06 [1080p].mkv", rows, rows, pages=2)
        self.assertEqual(2, result.tmdb_id)
        self.assertEqual("shared_similarity", selection["selection_method"])
        self.assertEqual(2, len(selection["candidate_similarities"]))
        self.assertEqual(2, sum(query.get("page") == 2 for query in search.queries))

    def test_unfinished_pagination_never_caches_a_partial_candidate_set(self):
        media = object.__new__(Media)
        media._tmdb_clients = []
        media.search = SimpleNamespace(total_pages=2, tv_shows=Mock(side_effect=[
            [{"id": 1, "name": "Alpha Story"}], Timeout("page two failed")]))
        with patch.object(recognition_cache, "get", return_value=recognition_cache.CACHE_MISS), \
                patch.object(recognition_cache, "put") as cache_write:
            with self.assertRaises(Timeout):
                media._Media__query_tmdb_candidates("Alpha Story", MediaType.TV)
        cache_write.assert_not_called()

    def test_multi_search_keeps_movie_and_tv_with_equal_ids_and_skips_people(self):
        media = object.__new__(Media)
        media._tmdb_clients = []
        media.search = SimpleNamespace(total_pages=1, multi=Mock(return_value=[
            AsObj(id=1, media_type="person", name="Alpha"),
            AsObj(id=1, media_type="movie", title="Alpha"),
            AsObj(id=1, media_type="tv", name="Alpha"),
            AsObj(id=1, media_type="tv", name="Alpha"),
        ]))
        with patch.object(recognition_cache, "put"):
            results = media._Media__query_tmdb_candidates("Alpha", None, cache=False)
        self.assertEqual({MediaType.MOVIE, MediaType.TV}, {row["media_type"] for row in results})
        self.assertEqual(2, len(results))
