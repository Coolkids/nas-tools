"""恢复备份时使来源租约失效，并在启动时安全清理未发布代次。"""

import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.db.manager import DATABASE
from app.db.media_db import MediaDb
from app.db.models import Base
from app.db.maintenance import (
    _content_digests_match,
    _content_digests_match_except_sync_jobs,
    _digest_tables,
    _invalidate_restored_media_sync_leases,
    _restored_media_sync_leases_invalidated,
)


class MediaSyncRestoreLeaseTest(TestCase):
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

    def test_restore_fences_old_worker_and_startup_only_removes_unpublished_rows(self):
        self.assertTrue(self.media_db.statistics("emby", 1, 1, 0))
        self.assertTrue(self.media_db.insert("emby", {
            "id": "active", "type": "Movie", "title": "活动影片"}))
        active_generation = self.media_db.get_statistics("emby").ACTIVE_GENERATION
        old_lease = self.media_db.acquire_sync_lease("emby", lease_seconds=300)
        self.media_db.write_sync_batch(old_lease, [{
            "id": "staged", "type": "Movie", "title": "未发布影片"}])
        backup_digests = _digest_tables(DATABASE.engine)

        with DATABASE.engine.begin() as connection:
            self.assertEqual(1, _invalidate_restored_media_sync_leases(connection))

        self.assertFalse(_content_digests_match(DATABASE.engine, backup_digests))
        self.assertTrue(_content_digests_match_except_sync_jobs(
            DATABASE.engine, backup_digests))
        self.assertTrue(_restored_media_sync_leases_invalidated(DATABASE.engine))
        self.assertFalse(self.media_db.renew_sync_lease(old_lease))

        recovered = self.media_db.recover_stale_sync_jobs()

        self.assertEqual("INTERRUPTED", recovered[0]["status"])
        self.assertEqual(1, recovered[0]["removed_rows"])
        self.assertTrue(self.media_db.exists("emby", "活动影片", None, None))
        self.assertFalse(self.media_db.exists("emby", "未发布影片", None, None))
        self.assertEqual(active_generation,
                         self.media_db.get_statistics("emby").ACTIVE_GENERATION)
