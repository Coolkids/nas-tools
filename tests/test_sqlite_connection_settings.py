"""SQLite connection-level settings must apply to every pooled connection."""

from app.db.backends.sqlite import SQLiteBackend
from app.db.manager import DatabaseManager


def _read_settings(connection):
    return {
        name: connection.exec_driver_sql(f"PRAGMA {name}").scalar_one()
        for name in ("busy_timeout", "foreign_keys", "synchronous", "wal_autocheckpoint")
    }


def test_sqlite_settings_apply_to_pooled_and_recreated_connections(tmp_path, monkeypatch):
    database_path = tmp_path / "nastools.db"
    monkeypatch.setattr(SQLiteBackend, "path", staticmethod(lambda: str(database_path)))
    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_read_settings", lambda: {"type": "sqlite"})

    try:
        engine = manager.engine
        assert engine.pool.size() == 2
        assert engine.pool._max_overflow == 0
        with engine.connect() as first, engine.connect() as second:
            assert _read_settings(first) == {
                "busy_timeout": 60000,
                "foreign_keys": 1,
                "synchronous": 1,
                "wal_autocheckpoint": 1000,
            }
            assert _read_settings(second) == _read_settings(first)

        engine.dispose()
        with manager.engine.connect() as recreated:
            assert _read_settings(recreated) == {
                "busy_timeout": 60000,
                "foreign_keys": 1,
                "synchronous": 1,
                "wal_autocheckpoint": 1000,
            }
    finally:
        if manager._engine is not None:
            manager._engine.dispose()
