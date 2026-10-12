"""旧设置包与完整双库备份必须区别处理。"""

import json
import os
import tempfile
import zipfile
from unittest import TestCase

from app.db.maintenance import _legacy_dual_backup_entries


class LegacyDatabaseBackupConversionTest(TestCase):
    def _entries(self, members):
        temporary = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        temporary.close()
        self.addCleanup(lambda: os.unlink(temporary.name))
        with zipfile.ZipFile(temporary.name, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return zipfile.ZipFile(temporary.name, "r")

    def test_accepts_only_complete_database_pairs_from_one_layout(self):
        for layout in (("user.db", "media.db"), ("db/user.db", "db/media.db")):
            with self.subTest(layout=layout):
                archive = self._entries({name: b"sqlite" for name in layout})
                with archive:
                    entries = _legacy_dual_backup_entries(archive)
                self.assertEqual({"user.db", "media.db"}, set(entries))

    def test_rejects_incomplete_or_settings_only_archives(self):
        incomplete = self._entries({"config.yaml": b"app: {}", "user.db": b"sqlite"})
        with incomplete:
            with self.assertRaisesRegex(ValueError, "缺少 media.db"):
                _legacy_dual_backup_entries(incomplete)

        settings = self._entries({
            "config.yaml": b"app: {}",
            "backup-format.json": json.dumps({"database_included": False}).encode(),
        })
        with settings:
            with self.assertRaisesRegex(ValueError, "设置备份"):
                _legacy_dual_backup_entries(settings)
