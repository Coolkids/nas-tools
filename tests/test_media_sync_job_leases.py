"""持久化媒体同步租约的互斥与 fencing token 回归。"""

import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.db.manager import DATABASE
from app.db.media_db import MediaDb
from app.db.models import Base, MEDIASYNCSYNCJOB


class MediaSyncJobLeaseTest(TestCase):
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

    def test_server_has_one_active_lease_and_old_token_cannot_release_new_job(self):
        first = self.media_db.acquire_sync_lease("emby", lease_seconds=30)
        self.assertIsNotNone(first)
        self.assertIsNone(self.media_db.acquire_sync_lease("emby", lease_seconds=30))
        self.assertTrue(self.media_db.renew_sync_lease(first, lease_seconds=30))

        with self.media_db.transaction() as session:
            job = session.query(MEDIASYNCSYNCJOB).filter_by(SERVER="emby").one()
            job.LEASE_UNTIL = 0

        second = self.media_db.acquire_sync_lease("emby", lease_seconds=30)
        self.assertIsNotNone(second)
        self.assertEqual(first["lease_token"] + 1, second["lease_token"])
        self.assertNotEqual(first["generation_id"], second["generation_id"])
        self.assertFalse(self.media_db.renew_sync_lease(first, lease_seconds=30))
        self.assertFalse(self.media_db.release_sync_lease(first, status="SUCCEEDED"))
        self.assertTrue(self.media_db.sync_lease_is_current(second))
        self.assertTrue(self.media_db.release_sync_lease(second, status="FAILED"))
        self.assertFalse(self.media_db.sync_lease_is_current(second))

    def test_different_servers_can_sync_independently(self):
        emby = self.media_db.acquire_sync_lease("emby")
        plex = self.media_db.acquire_sync_lease("plex")

        self.assertIsNotNone(emby)
        self.assertIsNotNone(plex)
        self.assertTrue(self.media_db.sync_lease_is_current(emby))
        self.assertTrue(self.media_db.sync_lease_is_current(plex))

    def test_startup_recovery_cleans_expired_generation_and_keeps_active_items(self):
        self.assertTrue(self.media_db.statistics("emby", 1, 1, 0))
        self.assertTrue(self.media_db.insert("emby", {
            "id": "active", "type": "Movie", "title": "活动条目"}))
        active_generation = self.media_db.get_statistics("emby").ACTIVE_GENERATION
        expired = self.media_db.acquire_sync_lease("emby")
        self.media_db.write_sync_batch(expired, [{
            "id": "unpublished", "type": "Movie", "title": "未发布条目"}])
        with self.media_db.transaction() as session:
            job = session.query(MEDIASYNCSYNCJOB).filter_by(SERVER="emby").one()
            job.LEASE_UNTIL = 0
            job.STATUS = "WRITING"

        from app.db import init_db
        init_db()

        self.assertTrue(self.media_db.exists("emby", "活动条目", None, None))
        self.assertFalse(self.media_db.exists("emby", "未发布条目", None, None))
        self.assertEqual(active_generation,
                         self.media_db.get_statistics("emby").ACTIVE_GENERATION)
        with self.media_db.transaction() as session:
            job = session.query(MEDIASYNCSYNCJOB).filter_by(SERVER="emby").one()
            self.assertEqual("INTERRUPTED", job.STATUS)
            self.assertEqual(0, job.LEASE_UNTIL)

    def test_startup_recovery_leaves_live_sync_lease_alone(self):
        lease = self.media_db.acquire_sync_lease("emby", lease_seconds=300)

        self.assertEqual([], self.media_db.recover_stale_sync_jobs())
        self.assertTrue(self.media_db.sync_lease_is_current(lease))
