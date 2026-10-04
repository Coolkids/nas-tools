from unittest import TestCase

from app.media.recognition.adapters import meta_from_anitopy_result
from app.utils.types import MediaType


class RecognitionAdaptersTest(TestCase):
    def test_adapts_anitopy_ml_anime_schema(self):
        result = {
            "record_title": "Grand Blue 2025 S02E02 [1080p]",
            "anime_title": "Grand Blue",
            "all_titles": ["Grand Blue"],
            "anime_year": "2025",
            "episode_number": "02",
            "media_type": "anime",
            "video_resolution": "1080p",
            "video_term": "x265",
            "audio_term": "OPUS",
            "release_group": "ExampleGroup",
        }

        meta_info = meta_from_anitopy_result(result["record_title"], result)

        self.assertEqual("Grand Blue", meta_info.get_name())
        self.assertEqual(MediaType.ANIME, meta_info.type)
        self.assertEqual("2025", str(meta_info.year))
        self.assertEqual(2, meta_info.begin_episode)
        self.assertEqual("1080p", meta_info.resource_pix)
        self.assertEqual("x265", meta_info.video_encode)
        self.assertEqual("OPUS", meta_info.audio_encode)
        self.assertEqual("ExampleGroup", meta_info.resource_team)

    def test_adapts_nested_extracted_title_spans_without_media_type(self):
        result = {
            "extracted": {
                "title_spans": [{"start": 0, "end": 10, "text": "Grand Blue"}],
                "all_titles": ["Grand Blue"],
                "anime_year": "2025",
            },
        }

        meta_info = meta_from_anitopy_result("Grand Blue S02E02", result)

        self.assertEqual("Grand Blue", meta_info.get_name())
        self.assertEqual(MediaType.ANIME, meta_info.type)
