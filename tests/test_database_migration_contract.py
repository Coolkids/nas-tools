"""Exercise the portable table-copy contract across isolated database engines."""

import json
import os

import pytest
import sqlalchemy as sa

from app.db.backends import create_backend
from app.db.manager import DatabaseManager
from app.db.migration import (_calibrate_sequences, _coerce_transfer_value, _copy_table,
                              _safe_error, _table_digest)
from app.db.models import Base, MEDIASYNCITEMS, RSSTVS


@pytest.mark.parametrize("source_type", ("sqlite", "postgresql", "mariadb", "mysql"))
@pytest.mark.parametrize("target_type", ("sqlite", "postgresql", "mariadb", "mysql"))
def test_portable_table_copy_matrix_preserves_data_and_new_ids(
        source_type, target_type, tmp_path, monkeypatch):
    remote_settings = json.loads(os.environ.get("NASTOOLS_TEST_DATABASE_SETTINGS", "{}"))
    required_remote = {kind for kind in (source_type, target_type) if kind != "sqlite"}
    missing = sorted(kind for kind in required_remote if not remote_settings.get(kind))
    if missing:
        pytest.skip(f"未配置隔离数据库：{', '.join(missing)}")
    for kind in required_remote:
        if remote_settings[kind].get("type") != kind:
            pytest.fail(f"{kind} 测试库配置类型不匹配")

    def make_engine(kind, role):
        if kind == "sqlite":
            manager = DatabaseManager()
            monkeypatch.setattr(manager, "_read_settings", lambda: {"type": "sqlite"})
            from app.db.backends.sqlite import SQLiteBackend
            monkeypatch.setattr(SQLiteBackend, "path", staticmethod(
                lambda: str(tmp_path / f"{role}.sqlite3")))
            engine = manager.engine
            managers.append(manager)
            return engine
        return create_backend(kind).create_engine(remote_settings[kind])

    managers = []
    same_remote_database = source_type == target_type and source_type != "sqlite"
    source_engine = make_engine(source_type, "source")
    target_engine = source_engine if same_remote_database else make_engine(target_type, "target")
    snapshot_engine = None
    source_created = target_created = snapshot_created = False
    try:
        assert not sa.inspect(source_engine).get_table_names(), \
            f"{source_type} 源测试库非空，拒绝运行迁移矩阵"
        if target_engine is not source_engine:
            assert not sa.inspect(target_engine).get_table_names(), \
                f"{target_type} 目标测试库非空，拒绝运行迁移矩阵"

        Base.metadata.create_all(source_engine)
        source_created = True
        with source_engine.begin() as connection:
            source_id = connection.execute(MEDIASYNCITEMS.__table__.insert().values(
                SERVER="migration-matrix",
                ITEM_ID="matrix-中文-🧪",
                TITLE="矩阵迁移标题",
                PATH="/matrix/" + "路径/" * 100,
                JSON=json.dumps({"source": source_type, "value": "雪"}, ensure_ascii=False),
            )).inserted_primary_key[0]

        copy_source = source_engine
        if same_remote_database:
            # A same-instance remote migration is correctly rejected by the
            # application. Use a SQLite snapshot to isolate the copy contract,
            # then reuse the now-empty remote database as the target.
            snapshot_engine = sa.create_engine(f"sqlite:///{tmp_path / 'snapshot.sqlite3'}")
            Base.metadata.create_all(snapshot_engine)
            snapshot_created = True
            with source_engine.connect() as source_connection:
                for table in Base.metadata.sorted_tables:
                    _copy_table(source_connection, snapshot_engine, table)
            Base.metadata.drop_all(source_engine)
            source_created = False
            Base.metadata.create_all(target_engine)
            target_created = True
            copy_source = snapshot_engine
        elif target_engine is not source_engine:
            Base.metadata.create_all(target_engine)
            target_created = True

        expected = {}
        with copy_source.connect() as source_connection:
            for table in Base.metadata.sorted_tables:
                expected[table.name] = _table_digest(source_connection, table)
                copied = _copy_table(source_connection, target_engine, table)
                assert copied == expected[table.name][0]

        with target_engine.connect() as target_connection:
            for table in Base.metadata.sorted_tables:
                assert _table_digest(target_connection, table) == expected[table.name]
        _calibrate_sequences(target_engine)
        with target_engine.begin() as connection:
            row = connection.execute(sa.select(
                MEDIASYNCITEMS.__table__.c.TITLE,
                MEDIASYNCITEMS.__table__.c.ITEM_ID,
                MEDIASYNCITEMS.__table__.c.ID,
            )).one()
            assert row == ("矩阵迁移标题", "matrix-中文-🧪", source_id)
            new_id = connection.execute(MEDIASYNCITEMS.__table__.insert().values(
                SERVER="migration-matrix",
                ITEM_ID="matrix-after-copy",
                TITLE="迁移后新增",
            )).inserted_primary_key[0]
            assert new_id > source_id
    finally:
        if target_created:
            Base.metadata.drop_all(target_engine)
        if source_created and source_engine is not target_engine:
            Base.metadata.drop_all(source_engine)
        if snapshot_created:
            Base.metadata.drop_all(snapshot_engine)
        if target_engine is not source_engine:
            target_engine.dispose()
        source_engine.dispose()
        for manager in managers:
            manager.dispose()


def test_portable_copy_normalizes_blank_nullable_integer_values(tmp_path):
    source_engine = sa.create_engine(f"sqlite:///{tmp_path / 'source.sqlite3'}")
    target_engine = sa.create_engine(f"sqlite:///{tmp_path / 'target.sqlite3'}")
    try:
        Base.metadata.create_all(source_engine)
        Base.metadata.create_all(target_engine)
        with source_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO RSS_TVS (ID, DOWNLOAD_SETTING) VALUES (?, ?)",
                (991, ""))

        with source_engine.connect() as source:
            expected = _table_digest(source, RSSTVS.__table__, for_transfer=True)
            assert _copy_table(source, target_engine, RSSTVS.__table__) == 1
        with target_engine.connect() as target:
            actual = _table_digest(target, RSSTVS.__table__, for_transfer=True)
            value = target.exec_driver_sql(
                "SELECT DOWNLOAD_SETTING FROM RSS_TVS WHERE ID=991").scalar_one()

        assert value is None
        assert actual == expected
        assert _coerce_transfer_value("", RSSTVS.__table__.c.DOWNLOAD_SETTING) is None
    finally:
        Base.metadata.drop_all(target_engine)
        Base.metadata.drop_all(source_engine)
        target_engine.dispose()
        source_engine.dispose()


def test_database_statement_errors_do_not_expose_bound_row_values():
    from sqlalchemy.exc import StatementError

    error = StatementError(
        "database statement failed",
        "INSERT INTO RSS_TVS (NAME) VALUES (?)",
        [{"NAME": "private-subscription-marker", "PASSWORD": "private-password-marker"}],
        RuntimeError("driver details private-subscription-marker"))

    safe = _safe_error(error, {"password": "private-password-marker"})

    assert "private-subscription-marker" not in safe
    assert "private-password-marker" not in safe
    assert "INSERT INTO" not in safe
