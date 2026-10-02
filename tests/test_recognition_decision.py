# -*- coding: utf-8 -*-
from unittest import TestCase
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.media.recognition.decision import candidate_features, select_title_evidence
from app.media.media import Media
from app.utils.types import MediaType


def candidate(provider, tmdb_id, media_type="movie"):
    parsed = {"name": f"parsed by {provider}"}
    tmdb = {"id": tmdb_id, "media_type": media_type}
    return provider, parsed, tmdb


def evidence(candidate_value, level, score=None):
    value = {"level": level}
    if score is not None:
        value["score"] = score
    return candidate_value, value


class RecognitionDecisionTest(TestCase):
    def test_no_candidate_and_missing_evidence_have_distinct_failure_reasons(self):
        no_candidates = select_title_evidence([])
        no_title_evidence = select_title_evidence([
            evidence(candidate("local_rules", 10), "none"),
        ])
        unavailable_title = select_title_evidence([
            evidence(candidate("anitopy_ml", 20), "unavailable"),
        ])
        weak_only = select_title_evidence([
            evidence(candidate("local_rules", 30), "weak"),
        ])

        self.assertEqual(("failed", "no_tmdb_match"),
                         (no_candidates["status"], no_candidates["reason"]))
        self.assertEqual(("failed", "insufficient_title_evidence"),
                         (no_title_evidence["status"], no_title_evidence["reason"]))
        self.assertEqual(("failed", "insufficient_title_evidence"),
                         (unavailable_title["status"], unavailable_title["reason"]))
        self.assertEqual(("failed", "insufficient_title_evidence"),
                         (weak_only["status"], weak_only["reason"]))
        self.assertIsNone(unavailable_title["selected"])
        self.assertIsNone(weak_only["selected"])

    def test_single_explicit_fuzzy_fallback_can_win_but_not_beat_strong_evidence(self):
        fuzzy_only = select_title_evidence([
            evidence(candidate("model_a", 10), "fuzzy", 0.91),
        ])
        strong_and_fuzzy = select_title_evidence([
            evidence(candidate("model_a", 10), "fuzzy", 0.99),
            evidence(candidate("local_rules", 20), "strong", 0.20),
        ])

        self.assertEqual("success", fuzzy_only["status"])
        self.assertEqual("10", str(fuzzy_only["selected"][2]["id"]))
        self.assertEqual("success", strong_and_fuzzy["status"])
        self.assertEqual("20", str(strong_and_fuzzy["selected"][2]["id"]))

    def test_two_strong_entities_fail_even_with_different_scores(self):
        first = candidate("local_rules", 10)
        second = candidate("model_b", 20)

        result = select_title_evidence([
            evidence(first, "strong", 0.99),
            evidence(second, "strong", 0.60),
        ])

        self.assertEqual("failed", result["status"])
        self.assertEqual("ambiguous_tmdb", result["reason"])
        self.assertIsNone(result["selected"])
        self.assertEqual({("movie", "10"), ("movie", "20")}, set(result["entities"]))

    def test_weighted_score_selects_provider_only_after_entity_deduplication(self):
        local = candidate("local_rules", 10)
        model = candidate("anitopy_ml", 10)
        results = [
            evidence(local, "strong", 0.72),
            evidence(model, "strong", 0.91),
        ]

        forward = select_title_evidence(results)
        reverse = select_title_evidence(list(reversed(results)))

        self.assertEqual("anitopy_ml", forward["selected"][0])
        self.assertEqual("anitopy_ml", reverse["selected"][0])
        self.assertEqual(0.91, forward["confidence"])
        self.assertEqual(2, forward["agreement_count"])

    def test_agreement_bonus_is_diagnostic_and_cannot_override_distinct_entities(self):
        shared = select_title_evidence([
            evidence(candidate("local_rules", 10), "strong", 0.7),
            evidence(candidate("anitopy_ml", 10), "strong", 0.7),
        ], agreement_bonus=0.1)
        ambiguous = select_title_evidence([
            evidence(candidate("local_rules", 10), "strong", 0.99),
            evidence(candidate("anitopy_ml", 20), "strong", 0.1),
        ], agreement_bonus=1.0)

        self.assertEqual(0.8, shared["confidence"])
        self.assertEqual("failed", ambiguous["status"])
        self.assertEqual("ambiguous_tmdb", ambiguous["reason"])
        self.assertIsNone(ambiguous["selected"])

    def test_title_match_without_tmdb_id_or_media_type_cannot_be_selected(self):
        missing_id = ("local_rules", {"name": "Example"}, {"media_type": "movie"})
        missing_type = ("anitopy_ml", {"name": "Example"}, {"id": 10})

        result = select_title_evidence([
            evidence(missing_id, "strong", 1.0),
            evidence(missing_type, "strong", 1.0),
        ])

        self.assertEqual("failed", result["status"])
        self.assertEqual("invalid_tmdb_entity", result["reason"])
        self.assertIsNone(result["selected"])


class TmdbTitleEvidenceTest(TestCase):
    def setUp(self):
        self.media = object.__new__(Media)
        self.media._Media__search_tmdb_allnames = Mock(return_value=({}, []))

    def test_separators_and_case_normalize_to_a_strong_match(self):
        result = self.media._Media__tmdb_title_evidence(
            "The.Matrix.1999.1080p", {"title": "The Matrix"})
        self.assertEqual("strong", result["level"])

    def test_latin_match_requires_word_boundaries(self):
        result = self.media._Media__tmdb_title_evidence(
            "Titanic.1997.1080p", {"title": "It"})
        self.assertEqual("none", result["level"])

    def test_sequel_number_makes_the_original_title_a_weak_prefix(self):
        result = self.media._Media__tmdb_title_evidence(
            "Example Story 2 2024 1080p", {"title": "Example Story"})
        self.assertEqual("weak", result["level"])
        self.assertTrue(result["weak_matches"][0]["sequel_prefix"])

    def test_short_numeric_and_technical_titles_are_weak_evidence(self):
        for title, tmdb_name in (
                ("Up.2023.1080p", "Up"),
                ("1917.2019.WEB-DL", "1917"),
                ("H265.2024.BluRay", "H265")):
            with self.subTest(tmdb_name=tmdb_name):
                result = self.media._Media__tmdb_title_evidence(
                    title, {"title": tmdb_name})
                self.assertEqual("weak", result["level"])

    def test_mixed_release_technical_token_contexts_are_weak(self):
        cases = (
            ("Example.2024.1080p.WEB-DL.x265-GROUP", "WEB-DL"),
            ("[GROUP] Example 2024 2160p HDR10+ Blu-ray", "HDR10+"),
            ("Example.2024.HDTV.XviD", "HDTV"),
            ("Example.2024.DTS-HD.MA.5.1", "DTS-HD"),
            ("Example.2024.Dolby.Vision.TrueHD", "Dolby Vision"),
            ("Example.2024.WEBRip.DDP5.1", "DDP5.1"),
        )
        for raw_title, technical_name in cases:
            with self.subTest(technical_name=technical_name):
                result = self.media._Media__tmdb_title_evidence(
                    raw_title, {"title": technical_name})
                self.assertEqual("weak", result["level"])

    def test_custom_technical_weak_tokens_are_configurable(self):
        with patch("app.media.media.recognition_config", return_value={
                "decision": {"title_evidence": {"weak_technical_tokens": ["mycodec"]}}}):
            result = self.media._Media__tmdb_title_evidence(
                "Example.2024.MyCodec-GROUP", {"title": "MyCodec"})

        self.assertEqual("weak", result["level"])

    def test_release_group_match_is_weak_only_when_parser_identified_it_as_a_group(self):
        release_group = self.media._Media__tmdb_title_evidence(
            "Example.2024.[FLEET]", {"title": "FLEET"}, release_groups="FLEET")
        title_word = self.media._Media__tmdb_title_evidence(
            "FLEET.2024", {"title": "FLEET"})

        self.assertEqual("weak", release_group["level"])
        self.assertEqual("strong", title_word["level"])

    def test_traditional_chinese_tmdb_name_matches_simplified_title(self):
        result = self.media._Media__tmdb_title_evidence(
            "黑客帝國 1999", {"title": "黑客帝国"})
        self.assertEqual("strong", result["level"])

    def test_normalization_options_control_casefold_and_underscore_separators(self):
        info = {"title": "The Matrix"}
        normal_options = {
            "normalization": ["nfkc", "casefold", "simplified_chinese", "separators", "whitespace"]
        }
        exact_case_options = {"normalization": ["nfkc", "separators", "whitespace"]}
        with patch("app.media.media.recognition_config", return_value={
                "decision": {"title_evidence": normal_options}}):
            normalized = self.media._Media__tmdb_title_evidence("the_Matrix_1999", info)
        with patch("app.media.media.recognition_config", return_value={
                "decision": {"title_evidence": exact_case_options}}):
            case_sensitive = self.media._Media__tmdb_title_evidence("the Matrix", info)

        self.assertEqual("strong", normalized["level"])
        self.assertEqual("the_Matrix", normalized["matched_names"][0]["match_text"])
        self.assertEqual((0, 10), (normalized["matched_names"][0]["match_start"],
                                  normalized["matched_names"][0]["match_end"]))
        self.assertEqual("none", case_sensitive["level"])

    def test_traditional_chinese_evidence_points_back_to_raw_title_range(self):
        result = self.media._Media__tmdb_title_evidence(
            "黑客帝國_1999", {"title": "黑客帝国"})

        match = result["matched_names"][0]
        self.assertEqual("黑客帝國", match["match_text"])
        self.assertEqual((0, 4), (match["match_start"], match["match_end"]))

    def test_missing_tmdb_supplementary_fields_leave_title_evidence_available(self):
        result = self.media._Media__tmdb_title_evidence(
            "Example Story 2024", {"title": "Example Story"})
        features = candidate_features(
            SimpleNamespace(type=None, year=None, begin_season="unknown"),
            {"title": "Example Story"}, result)

        self.assertEqual("strong", result["level"])
        self.assertEqual(1.0, features["title_match"])
        self.assertEqual(0.5, features["year_match"])
        self.assertEqual(0.5, features["type_match"])
        self.assertEqual(0.5, features["season_episode_match"])

    def test_name_sources_limit_alias_lookup(self):
        info = {"title": "Primary", "media_type": "movie", "id": 99}
        self.media._Media__search_tmdb_allnames = Mock(return_value=({}, ["Alias"]))
        with patch("app.media.media.recognition_config", return_value={
                "decision": {"title_evidence": {"name_sources": ["primary"]}}}):
            primary_only = self.media._Media__tmdb_title_evidence("Alias", info)
        self.media._Media__search_tmdb_allnames.assert_not_called()

        with patch("app.media.media.recognition_config", return_value={
                "decision": {"title_evidence": {"name_sources": ["translation"]}}}):
            translation = self.media._Media__tmdb_title_evidence("Alias", info)
        self.media._Media__search_tmdb_allnames.assert_called_once_with(
            "movie", 99, alias_sources={"translation"})
        self.assertEqual("none", primary_only["level"])
        self.assertEqual("strong", translation["level"])

    def test_malformed_optional_alias_metadata_is_ignored(self):
        details = {
            "alternative_titles": {"titles": [None, {"title": "Alternative"}]},
            "translations": {"translations": [None, {"data": None},
                                               {"data": {"title": "Translation"}}]},
        }
        media = object.__new__(Media)
        with patch.object(media, "get_tmdb_info", return_value=details) as get_details:
            _, names = media._Media__search_tmdb_allnames(MediaType.MOVIE, 10)

        get_details.assert_called_once()
        self.assertEqual(["Alternative", "Translation"], names)

    def test_same_entity_from_multiple_providers_is_deduplicated(self):
        local = candidate("local_rules", 10)
        model = candidate("model_b", 10)

        result = select_title_evidence([
            evidence(local, "strong"), evidence(model, "strong")
        ])

        self.assertEqual("success", result["status"])
        self.assertEqual({("movie", "10")}, set(result["entities"]))
        self.assertEqual("10", str(result["selected"][2]["id"]))

    def test_provider_order_does_not_change_selected_tmdb_entity(self):
        local = candidate("local_rules", 10)
        model = candidate("model_b", 20)
        values = [evidence(local, "strong"), evidence(model, "none")]

        forward = select_title_evidence(values)
        reversed_result = select_title_evidence(list(reversed(values)))

        self.assertEqual("10", str(forward["selected"][2]["id"]))
        self.assertEqual("10", str(reversed_result["selected"][2]["id"]))

    def test_fuzzy_fallback_requires_a_single_entity_and_never_overrides_strong(self):
        strong = candidate("local_rules", 10)
        fuzzy = candidate("model_b", 20)
        strong_result = select_title_evidence([
            evidence(fuzzy, "fuzzy"), evidence(strong, "strong")
        ])
        self.assertEqual("10", str(strong_result["selected"][2]["id"]))

        ambiguous_fuzzy = select_title_evidence([
            evidence(candidate("provider_a", 10), "fuzzy"),
            evidence(candidate("provider_b", 20), "fuzzy"),
        ])
        self.assertEqual("ambiguous_tmdb", ambiguous_fuzzy["reason"])
        self.assertIsNone(ambiguous_fuzzy["selected"])

    def test_movie_and_tv_ids_are_distinct_entities(self):
        result = select_title_evidence([
            evidence(candidate("movie_parser", 10, "movie"), "strong"),
            evidence(candidate("tv_parser", 10, "tv"), "strong"),
        ])

        self.assertEqual("ambiguous_tmdb", result["reason"])
