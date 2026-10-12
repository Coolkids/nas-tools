"""媒体库全量快照必须完整后才能交给数据库替换。"""

from unittest import TestCase

from app.mediaserver.media_server import _collect_media_sync_items


class MediaSyncSnapshotTest(TestCase):
    def test_accepts_a_confirmed_empty_library(self):
        items, movie_count, series_count = _collect_media_sync_items(
            {"MovieCount": 0, "SeriesCount": 0}, [],
            lambda library_id: self.fail("空媒体库不应请求条目"))

        self.assertEqual([], items)
        self.assertEqual((0, 0), (movie_count, series_count))

    def test_collects_items_and_checks_both_server_counts(self):
        libraries = [{"id": "movies", "name": "电影"}, {"id": "shows", "name": "剧集"}]
        fetched = {
            "movies": [{"id": 17, "type": "Movie", "title": "电影 A"}, {}],
            "shows": [{"id": "series-2", "type": "show", "title": "剧集 B"}],
        }
        progress = []

        items, movie_count, series_count = _collect_media_sync_items(
            {"MovieCount": 1, "SeriesCount": "1"}, libraries,
            lambda library_id: fetched[library_id],
            on_library=lambda name, count: progress.append((name, count)))

        self.assertEqual((1, 1), (movie_count, series_count))
        self.assertEqual(["17", "series-2"], [item["id"] for item in items])
        self.assertEqual(["电影", "剧集"], [item["library"] for item in items])
        self.assertEqual([("电影", 1), ("剧集", 2)], progress)

    def test_rejects_missing_counts_libraries_and_partial_pages(self):
        cases = [
            (None, [], lambda _: []),
            ({"MovieCount": 1}, [{"id": "movies"}], lambda _: []),
            ({"MovieCount": 1, "SeriesCount": 0}, [], lambda _: []),
            ({"MovieCount": 0, "SeriesCount": 0}, None, lambda _: []),
            ({"MovieCount": 0, "SeriesCount": 0}, [{"id": "movies"}], lambda _: None),
        ]
        for counts, libraries, get_items in cases:
            with self.subTest(counts=counts, libraries=libraries):
                with self.assertRaises(ValueError):
                    _collect_media_sync_items(counts, libraries, get_items)

    def test_rejects_duplicate_or_malformed_items(self):
        libraries = [{"id": "movies", "name": "电影"}]
        duplicate = [
            {"id": "same", "type": "Movie", "title": "电影 A"},
            {"id": "same", "type": "Movie", "title": "电影 A"},
        ]
        with self.assertRaisesRegex(ValueError, "重复返回媒体条目"):
            _collect_media_sync_items({"MovieCount": 2, "SeriesCount": 0},
                                      libraries, lambda _: duplicate)
        with self.assertRaisesRegex(ValueError, "缺少标识或标题"):
            _collect_media_sync_items({"MovieCount": 1, "SeriesCount": 0},
                                      libraries, lambda _: [{"id": "x", "type": "Movie"}])

    def test_propagates_generator_failures_instead_of_returning_partial_rows(self):
        def failed_page(_):
            yield {"id": "movie-1", "type": "Movie", "title": "电影 A"}
            raise RuntimeError("page request failed")

        with self.assertRaisesRegex(RuntimeError, "page request failed"):
            _collect_media_sync_items({"MovieCount": 2, "SeriesCount": 0},
                                      [{"id": "movies"}], failed_page)
