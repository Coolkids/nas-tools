import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from app.db.main_db import MainDb


class FakeStorageJobs:
    @staticmethod
    @contextmanager
    def maintenance_lock(exclusive=False):
        yield

    @staticmethod
    def maintenance_gate(domain):
        return False


class FakeConnection:
    def __init__(self, result):
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement):
        assert str(statement) == "PRAGMA wal_checkpoint(PASSIVE)"
        return SimpleNamespace(one=lambda: self.result)


class FakeEngine:
    def __init__(self, result):
        self.result = result

    def connect(self):
        return FakeConnection(self.result)


def test_passive_checkpoint_reports_incomplete_progress(caplog):
    engine = FakeEngine((0, 12, 5))
    database = SimpleNamespace(backend=SimpleNamespace(name="sqlite"), engine=engine)
    with patch("app.db.main_db.DATABASE", database), \
            patch("app.utils.storage_jobs.STORAGE_JOBS", FakeStorageJobs()):
        with caplog.at_level(logging.INFO, logger="app.db.main_db"):
            result = MainDb.wal_checkpoint()

        assert result == {"busy": 0, "log_frames": 12, "checkpointed_frames": 5}
        assert "checkpoint incomplete" in caplog.text
        assert "log_frames=12" in caplog.text
        assert "checkpointed_frames=5" in caplog.text

        caplog.clear()
        engine.result = (0, 12, 12)
        assert MainDb.wal_checkpoint() == {
            "busy": 0, "log_frames": 12, "checkpointed_frames": 12,
        }
        assert not caplog.records
