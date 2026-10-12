"""媒体库全量快照必须完整后才能交给数据库替换。"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.mediaserver.media_server import (MediaServer, _collect_media_sync_items,
                                         _media_sync_batches)


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

    def test_batches_are_bounded_by_row_count_and_persisted_field_bytes(self):
        items = [{"id": str(number), "title": "x"} for number in range(5)]
        by_rows = list(_media_sync_batches(items, max_rows=2, max_bytes=1024))
        self.assertEqual([2, 2, 1], [len(batch) for batch in by_rows])

        by_bytes = list(_media_sync_batches(items, max_rows=20, max_bytes=3))
        self.assertEqual([1, 1, 1, 1, 1], [len(batch) for batch in by_bytes])

    def test_sync_does_not_write_or_publish_when_snapshot_is_incomplete(self):
        instance = MediaServer()
        lease = {"server": "emby", "job_id": "job", "generation_id": "generation",
                 "lease_token": 1, "expected_active_generation": "legacy"}
        media_db = Mock()
        media_db.acquire_sync_lease.return_value = lease
        progress = Mock()
        with patch.object(instance, "mediadb", media_db), \
                patch.object(instance, "progress", progress), \
                patch.object(instance, "_server_type", SimpleNamespace(value="emby")), \
                patch.object(instance, "get_medias_count", return_value=None), \
                patch.object(instance, "get_libraries", return_value=[]), \
                patch.object(instance, "get_items") as get_items:
            instance.sync_mediaserver()

        get_items.assert_not_called()
        media_db.write_sync_batch.assert_not_called()
        media_db.publish_sync_generation.assert_not_called()
        media_db.replace.assert_not_called()
        self.assertEqual(2, media_db.cleanup_sync_generations.call_count)
        self.assertEqual((lease,), media_db.cleanup_sync_generations.call_args_list[0].args)
        self.assertEqual({}, media_db.cleanup_sync_generations.call_args_list[0].kwargs)
        self.assertEqual((lease,), media_db.cleanup_sync_generations.call_args_list[1].args)
        self.assertEqual({"generation_id": "generation"},
                         media_db.cleanup_sync_generations.call_args_list[1].kwargs)
        media_db.release_sync_lease.assert_called_once_with(lease, status="FAILED")
        progress.end.assert_called_once_with("mediasync")
