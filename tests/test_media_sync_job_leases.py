"""持久化媒体同步租约的互斥与 fencing token 回归。"""

import multiprocessing
import tempfile
import time
from unittest import TestCase
from unittest.mock import patch

from app.db.manager import DATABASE
from app.db.media_db import MediaDb
from app.db.models import Base, MEDIASYNCSYNCJOB


class _Config:
    def __init__(self, directory):
        self.directory = directory

    def get_config_path(self):
        return self.directory

    @staticmethod
    def get_config(node=None):
        return {"type": "sqlite"} if node == "database" else {}


def _hold_unpublished_sync_generation(directory, ready):
    config = _Config(directory)
    with patch("config.Config", return_value=config):
        DATABASE.dispose()
        media_db = MediaDb()
        lease = media_db.acquire_sync_lease("emby", lease_seconds=1)
        media_db.write_sync_batch(lease, [{
            "id": "child-unpublished", "type": "Movie", "title": "子进程未发布条目"}])
        ready.set()
        while True:
            time.sleep(1)


class MediaSyncJobLeaseTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        config = _Config(self.directory.name)
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

    def test_process_death_recovers_expired_unpublished_generation(self):
        self.assertTrue(self.media_db.statistics("emby", 1, 1, 0))
        self.assertTrue(self.media_db.insert("emby", {
            "id": "parent-active", "type": "Movie", "title": "父进程活动条目"}))
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        child = context.Process(target=_hold_unpublished_sync_generation,
                                args=(self.directory.name, ready))
        child.start()
        try:
            self.assertTrue(ready.wait(30), "子进程未写入未发布媒体代次")
        finally:
            if child.is_alive():
                child.terminate()
                child.join(10)
            if child.is_alive():
                child.kill()
                child.join(5)
        self.assertNotEqual(0, child.exitcode)
        time.sleep(1.1)

        recovered = self.media_db.recover_stale_sync_jobs()

        self.assertEqual([{"server": "emby", "status": "INTERRUPTED",
                           "removed_rows": 1, "error": None}], recovered)
        self.assertTrue(self.media_db.exists("emby", "父进程活动条目", None, None))
        self.assertFalse(self.media_db.exists("emby", "子进程未发布条目", None, None))
