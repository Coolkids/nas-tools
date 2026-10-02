from unittest import TestCase
from unittest.mock import Mock, patch

from app.media import media as media_module
from app.media.media import Media


class _Meta:
    def __init__(self, name):
        self.name = name
        self.type = None
        self.tmdb_info = None

    def get_name(self):
        return self.name

    def set_tmdb_info(self, info):
        self.tmdb_info = info


class RecognitionLegacyBehaviorTest(TestCase):
    def _run_legacy(self, local_result, ai_result, local_name="Local", ai_name="AI"):
        media = object.__new__(Media)
        local_meta = _Meta(local_name)
        ai_meta = _Meta(ai_name) if ai_name else None
        tmdb_responses = [local_result]
        if ai_meta:
            tmdb_responses.append(ai_result)
        config = {
            "recognition": {
                "execution": {"total_timeout_seconds": 10},
                "decision": {"strategy": "legacy", "shadow": {"enabled": False}},
            },
            "laboratory": {},
        }
        with patch("app.media.media.prepare_media_title",
                   return_value=("Original title", None, {"ignored": [], "replaced": [], "offset": []})) as preprocess, \
                patch("app.media.media.MetaInfo", return_value=local_meta) as parse_local, \
                patch.object(media, "_Media__request_ai_parse", return_value={"raw": "response"}), \
                patch.object(media, "_Media__meta_from_ai_result", return_value=ai_meta), \
                patch.object(media, "_Media__search_meta_tmdb", side_effect=tmdb_responses), \
                patch.object(media, "_Media__recognition_provider_enabled", return_value=True), \
                patch.object(media, "_Media__has_additional_recognizer", return_value=False), \
                patch.object(media_module.registry, "discover", return_value={}), \
                patch("app.media.media.recognition_config",
                      side_effect=lambda section=None: config.get(section, config)):
            result = media._Media__get_media_info_with_providers("Original title")

        preprocess.assert_called_once_with("Original title", None)
        parse_local.assert_called_once_with(
            "Original title", subtitle=None, mtype=None, apply_custom_words=False)
        return result, local_meta, ai_meta

    def test_legacy_selection_matrix_is_preserved_during_shadow_rollout(self):
        same = {"id": 1, "media_type": "tv"}
        different = {"id": 2, "media_type": "tv"}
        cases = (
            # A hit shared by both providers keeps the local result.
            (same, same, "Local", "AI", "Local", same),
            # Legacy behavior intentionally keeps its historical AI preference on conflict.
            (same, different, "Local", "AI", "AI", different),
            # A local hit survives an AI TMDB miss or parser failure.
            (same, None, "Local", "AI", "Local", same),
            (same, None, "Local", None, "Local", same),
            # An AI-only TMDB hit selects the AI parse.
            (None, different, "Local", "AI", "AI", different),
            # With no TMDB hit, retain a named local parse, then fall back to AI.
            (None, None, "Local", "AI", "Local", None),
            (None, None, "", "AI", "AI", None),
        )
        for local_result, ai_result, local_name, ai_name, expected_name, expected_tmdb in cases:
            with self.subTest(local_result=local_result, ai_result=ai_result,
                              local_name=local_name, ai_name=ai_name):
                result, _, _ = self._run_legacy(
                    local_result, ai_result, local_name=local_name, ai_name=ai_name)
                self.assertEqual(expected_name, result.get_name())
                self.assertEqual(expected_tmdb, result.tmdb_info)


if __name__ == "__main__":
    import unittest
    unittest.main()
