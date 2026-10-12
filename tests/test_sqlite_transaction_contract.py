"""Python 3.14 sqlite3 / SQLAlchemy transaction and maintenance boundaries."""

from types import SimpleNamespace
from unittest.mock import patch

import sqlalchemy as sa

from app.db.backends.sqlite import SQLiteBackend


def test_sqlite_transactions_rollback_release_connections_and_vacuum_outside_transaction(tmp_path):
    configuration = SimpleNamespace(get_config_path=lambda: str(tmp_path))
    backend = SQLiteBackend()
    with patch("config.Config", return_value=configuration):
        engine = backend.create_engine({})
    sa.event.listen(engine, "connect", lambda connection, record:
                    backend.configure_connection(connection))
    try:
        backend.initialize(engine)

        def assert_connection_settings():
            with engine.connect() as connection:
                raw = connection.connection.driver_connection
                assert raw.autocommit is False
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 60000
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
                assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
                assert connection.exec_driver_sql("PRAGMA wal_autocheckpoint").scalar_one() == 1000
            assert engine.pool.checkedout() == 0

        assert_connection_settings()
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE tx_probe (id INTEGER PRIMARY KEY, value TEXT)")

        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO tx_probe (id, value) VALUES (1, 'rolled back')")
                raise RuntimeError("force transaction rollback")
        except RuntimeError as error:
            assert str(error) == "force transaction rollback"

        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM tx_probe").scalar_one() == 0
        assert engine.pool.checkedout() == 0

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO tx_probe (id, value) VALUES (1, 'committed')")
            connection.exec_driver_sql("DELETE FROM tx_probe")
        backend.reclaim_space(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM tx_probe").scalar_one() == 0
        assert engine.pool.checkedout() == 0

        engine.dispose()
        assert_connection_settings()
    finally:
        engine.dispose()
