"""SQLite database files stay below the application config directory."""

import os

import pytest

from app.db.backends.sqlite import SQLiteBackend
from app.db.migration import _normalize_candidate
from app.db.sqlite_files import _database_type, database_path


class _Config:
    def __init__(self, config_path, database=None):
        self.config_path = str(config_path)
        self.database = database

    def get_config_path(self):
        return self.config_path

    def get_config(self, node=None):
        return self.database if node == "database" else {}


def test_missing_database_configuration_defaults_to_fixed_sqlite_path(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config", lambda: _Config(tmp_path))

    assert _database_type() == "sqlite"
    assert database_path() == os.path.join(str(tmp_path), "db", "nastools.db")
    assert SQLiteBackend.path() == database_path()


@pytest.mark.parametrize("field", ("path", "directory", "url", "dsn", "sqlite_path"))
def test_database_migration_api_rejects_sqlite_path_overrides(field):
    with pytest.raises(ValueError, match="路径和 DSN 由后端固定生成"):
        _normalize_candidate({"type": "sqlite", field: "/tmp/user-selected.db"})
