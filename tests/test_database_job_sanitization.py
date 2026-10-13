from unittest import TestCase

from web.action import _sanitize_database_job


class DatabaseJobSanitizationTest(TestCase):
    def test_hides_filesystem_paths_without_dropping_path_named_tables(self):
        job = {
            "result": {
                "snapshot_path": "/private/source.sqlite",
                "target": {"type": "sqlite", "path": "/private/target.sqlite"},
                "target_files": ["/private/user.db", "/private/media.db"],
                "tables": {
                    "CONFIG_SYNC_PATHS": {"rows": 3, "sha256": "abc"},
                    "DOWNLOAD_HISTORY": {"rows": 7, "sha256": "def"},
                },
            },
        }

        sanitized = _sanitize_database_job(job)

        self.assertNotIn("snapshot_path", sanitized["result"])
        self.assertNotIn("path", sanitized["result"]["target"])
        self.assertNotIn("target_files", sanitized["result"])
        self.assertEqual(
            sanitized["result"]["tables"], job["result"]["tables"])
