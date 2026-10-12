"""媒体新代次在统计原子发布后可见，旧代次安全清理。"""

import tempfile
import time
from threading import Event, Thread
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import text

from app.db.manager import DATABASE
from app.db.media_db import MediaDb
from app.db.models import Base, MEDIASYNCITEMS
from app.utils.storage_jobs import STORAGE_JOBS


class MediaSyncGenerationPublishTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        config = SimpleNamespace(
            get_config_path=lambda: self.directory.name,
            get_config=lambda node=None: {"type": "sqlite"} if node == "database" else {},
        )
        self.config_patch = patch("config.Config", return_value=config)
        self.config_patch.start()
        DATABASE.dispose()
        Base.metadata.create_all(DATABASE.engine)
        self.media_db = MediaDb()

    def tearDown(self):
        DATABASE.dispose()
        self.config_patch.stop()
        self.directory.cleanup()

    def _count_items(self, server="emby"):
        with DATABASE.engine.connect() as connection:
            return connection.execute(text(
                "SELECT count(*) FROM MEDIASYNC_ITEMS WHERE SERVER=:server"),
                {"server": server}).scalar_one()

    def test_publish_switches_visibility_and_stats_atomically_then_cleans_old_rows(self):
        self.assertTrue(self.media_db.statistics("emby", 1, 1, 0))
        self.assertTrue(self.media_db.insert("emby", {
            "id": "old", "type": "Movie", "title": "旧片", "tmdbid": "11"}))
        lease = self.media_db.acquire_sync_lease("emby")
        self.assertEqual("legacy", lease["expected_active_generation"])
        self.media_db.write_sync_batch(lease, [{
            "id": "new", "type": "Movie", "title": "新片", "tmdbid": "22"}])

        self.assertTrue(self.media_db.exists("emby", "旧片", None, None))
        self.assertFalse(self.media_db.exists("emby", "新片", None, None))
        self.assertFalse(self.media_db.insert("emby", {
            "id": "racing", "type": "Movie", "title": "竞争写入"}))
        self.assertFalse(self.media_db.empty("emby"))
        self.assertEqual(2, self._count_items())

        result = self.media_db.publish_sync_generation(lease, movie_count=1, series_count=0)
        self.assertEqual(1, result["total_count"])
        self.assertFalse(self.media_db.exists("emby", "旧片", None, None))
        self.assertTrue(self.media_db.exists("emby", "新片", None, None))
        statistic = self.media_db.get_statistics("emby")
        self.assertEqual(lease["generation_id"], statistic.ACTIVE_GENERATION)
        self.assertEqual("1", statistic.TOTAL_COUNT)
        self.assertEqual(2, self._count_items())

        removed = self.media_db.cleanup_sync_generations(lease, batch_size=1)
        self.assertEqual(1, removed)
        self.assertEqual(1, self._count_items())
        self.assertTrue(self.media_db.release_sync_lease(lease, status="SUCCEEDED"))

    def test_expired_or_fenced_lease_cannot_publish_staged_rows(self):
        self.media_db.statistics("emby", 0, 0, 0)
        lease = self.media_db.acquire_sync_lease("emby")
        self.media_db.write_sync_batch(lease, [{
            "id": "staged", "type": "Movie", "title": "待发布"}])
        with self.media_db.transaction() as session:
            session.execute(text("UPDATE MEDIASYNC_SYNC_JOBS SET LEASE_TOKEN=LEASE_TOKEN+1 "
                                 "WHERE SERVER='emby'"))

        with self.assertRaisesRegex(RuntimeError, "租约已过期或已被接管"):
            self.media_db.publish_sync_generation(lease, movie_count=1, series_count=0)
        self.assertFalse(self.media_db.exists("emby", "待发布", None, None))
        self.assertEqual(1, self._count_items())

    def test_legacy_items_without_statistics_remain_active_until_first_publish(self):
        self.assertTrue(self.media_db.insert("emby", {
            "id": "legacy-item", "type": "Movie", "title": "旧片"}))
        self.assertTrue(self.media_db.exists("emby", "旧片", None, None))
        lease = self.media_db.acquire_sync_lease("emby")
        self.assertEqual("legacy", lease["expected_active_generation"])
        self.media_db.cleanup_sync_generations(lease)
        self.assertEqual(1, self._count_items())

        self.media_db.write_sync_batch(lease, [{
            "id": "new-item", "type": "Movie", "title": "新片"}])
        self.assertTrue(self.media_db.exists("emby", "旧片", None, None))
        self.assertFalse(self.media_db.exists("emby", "新片", None, None))
        self.media_db.publish_sync_generation(lease, movie_count=1, series_count=0)
        self.assertFalse(self.media_db.exists("emby", "旧片", None, None))
        self.assertTrue(self.media_db.exists("emby", "新片", None, None))

    def test_sync_batch_waits_for_database_maintenance_gate(self):
        lease = self.media_db.acquire_sync_lease("emby")
        maintenance_started = Event()
        release_maintenance = Event()
        batch_finished = Event()
        batch_errors = []

        def hold_maintenance():
            with STORAGE_JOBS.maintenance_lock(exclusive=True):
                STORAGE_JOBS.set_maintenance_gate("database", "maintenance-test", "TESTING")
                maintenance_started.set()
                release_maintenance.wait(timeout=10)
                STORAGE_JOBS.clear_maintenance_gate("database", "maintenance-test")

        def write_batch():
            try:
                self.media_db.write_sync_batch(lease, [{
                    "id": "after-maintenance", "type": "Movie", "title": "完成后写入"}])
            except Exception as error:
                batch_errors.append(error)
            finally:
                batch_finished.set()

        maintenance_thread = Thread(target=hold_maintenance)
        maintenance_thread.start()
        batch_thread = None
        try:
            self.assertTrue(maintenance_started.wait(timeout=5))
            batch_thread = Thread(target=write_batch)
            batch_thread.start()
            time.sleep(0.1)
            self.assertFalse(batch_finished.is_set())
        finally:
            release_maintenance.set()
            maintenance_thread.join(timeout=5)
            if batch_thread:
                batch_thread.join(timeout=5)

        self.assertTrue(batch_finished.is_set())
        self.assertEqual([], batch_errors)
        self.assertEqual(1, self._count_items())
