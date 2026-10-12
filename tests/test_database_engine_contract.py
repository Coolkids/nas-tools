"""同一套数据库契约；远程后端仅在显式提供隔离测试库时启用。"""

import json
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.schema import CreateIndex

import app.db as app_database
from app.db.manager import DatabaseManager
from app.db.models import Base, LargeText
from config import Config


def test_mariadb_text_index_uses_bounded_prefixes():
    from app.db.models import DOUBANMEDIAS

    index = next(index for index in DOUBANMEDIAS.__table__.indexes
                 if index.name == "INDX_DOUBAN_MEDIAS_NAME")
    statement = str(CreateIndex(index).compile(dialect=MariaDBDialect()))

    assert "`NAME`(191)" in statement
    assert "`YEAR`(191)" in statement


@pytest.mark.parametrize("backend_name", ("sqlite", "postgresql", "mariadb", "mysql"))
def test_crud_transactions_bulk_upsert_pagination_and_session_release(
        backend_name, tmp_path, monkeypatch):
    if backend_name == "sqlite":
        from app.db.backends.sqlite import SQLiteBackend
        monkeypatch.setattr(SQLiteBackend, "path", staticmethod(
            lambda: str(tmp_path / "db" / "nastools.db")))
        settings = {"type": "sqlite"}
    else:
        all_settings = json.loads(os.environ.get("NASTOOLS_TEST_DATABASE_SETTINGS", "{}"))
        settings = all_settings.get(backend_name)
        if not settings:
            pytest.skip(f"未配置隔离的 {backend_name} 测试库")
        if settings.get("type") != backend_name:
            pytest.fail(f"{backend_name} 测试库配置类型不匹配")

    table_name = f"codex_contract_{backend_name}_{uuid.uuid4().hex[:12]}"
    model_base = declarative_base(metadata=sa.MetaData())

    class ContractRow(model_base):
        __tablename__ = table_name
        __table_args__ = (sa.UniqueConstraint("CODE", name=f"uq_{table_name}_code"),)

        ID = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
        CODE = sa.Column(sa.String(128), nullable=False)
        VALUE = sa.Column(LargeText)
        PATH = sa.Column(sa.Text)
        OPTIONAL = sa.Column(sa.Text)
        CREATED_AT = sa.Column(sa.String(32), nullable=False)

    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_read_settings", lambda: settings)
    engine = None
    try:
        engine = manager.engine
        assert table_name not in sa.inspect(engine).get_table_names()
        model_base.metadata.create_all(engine)

        with manager.transaction() as session:
            # Keep a sparse explicit key alongside generated IDs so paging and
            # inserts are exercised with a non-contiguous primary-key space.
            session.add(ContractRow(
                ID=100, CODE="sparse-id", VALUE="explicit", CREATED_AT="2026-10-13T10:00:00"))
            session.add(ContractRow(
                CODE="中文-emoji-🧪", VALUE=json.dumps({"payload": "长" * 50000}),
                PATH="/long/" + "路径/" * 1000, OPTIONAL=None,
                CREATED_AT="2026-10-13T10:00:00"))
            session.add(ContractRow(
                CODE="empty-string", VALUE="", PATH="", OPTIONAL="",
                CREATED_AT="2026-10-13T10:00:00"))
        assert manager.session.query(ContractRow).filter(
            ContractRow.CODE == "中文-emoji-🧪").one().OPTIONAL is None
        assert manager.session.query(ContractRow).filter(
            ContractRow.CODE == "empty-string").one().OPTIONAL == ""
        assert manager.session.query(ContractRow).filter(
            ContractRow.ID == 100).one().CODE == "sparse-id"

        inserted = manager.insert_many(ContractRow, [
            {"CODE": "bulk-a", "VALUE": "a", "CREATED_AT": "2026-10-13T10:00:00"},
            {"CODE": "bulk-b", "VALUE": "b", "CREATED_AT": "2026-10-13T10:00:00"},
        ])
        assert inserted == 2
        with manager.transaction():
            manager.upsert_many(ContractRow, [
                {"CODE": "bulk-a", "VALUE": "updated", "CREATED_AT": "2026-10-13T10:00:00"},
                {"CODE": "upsert-c", "VALUE": "c", "CREATED_AT": "2026-10-13T10:00:00"},
            ], ("CODE",), ("VALUE",))
        assert manager.session.query(ContractRow).filter(
            ContractRow.CODE == "bulk-a").one().VALUE == "updated"

        with pytest.raises(sa.exc.IntegrityError):
            with manager.transaction() as session:
                session.add(ContractRow(
                    CODE="bulk-a", VALUE="duplicate", CREATED_AT="2026-10-13T10:00:00"))

        with pytest.raises(RuntimeError, match="rollback probe"):
            with manager.transaction() as session:
                session.add(ContractRow(
                    CODE="rolled-back", VALUE="discard", CREATED_AT="2026-10-13T10:00:00"))
                raise RuntimeError("rollback probe")
        assert manager.session.query(ContractRow).filter(
            ContractRow.CODE == "rolled-back").count() == 0

        page = manager.session.query(ContractRow).order_by(
            ContractRow.CREATED_AT.desc(), ContractRow.ID.desc()).limit(3).all()
        all_rows = manager.session.query(ContractRow).all()
        expected_ids = sorted((row.ID for row in all_rows), reverse=True)[:3]
        assert [row.ID for row in page] == expected_ids
        assert all(row.ID > 0 for row in all_rows)

        manager.close_session()
        assert manager.metrics()["pool_checked_out"] == 0
    finally:
        if engine is not None:
            manager.close_session()
            if table_name in sa.inspect(engine).get_table_names():
                model_base.metadata.drop_all(engine)
            engine.dispose()


@pytest.mark.parametrize("backend_name", ("sqlite", "postgresql", "mariadb", "mysql"))
def test_unified_application_metadata_bootstraps_cleanly(backend_name, tmp_path, monkeypatch):
    if backend_name == "sqlite":
        from app.db.backends.sqlite import SQLiteBackend
        monkeypatch.setattr(SQLiteBackend, "path", staticmethod(
            lambda: str(tmp_path / "db" / "nastools.db")))
        settings = {"type": "sqlite"}
    else:
        all_settings = json.loads(os.environ.get("NASTOOLS_TEST_DATABASE_SETTINGS", "{}"))
        settings = all_settings.get(backend_name)
        if not settings:
            pytest.skip(f"未配置隔离的 {backend_name} 测试库")
        if settings.get("type") != backend_name:
            pytest.fail(f"{backend_name} 测试库配置类型不匹配")

    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_read_settings", lambda: settings)
    engine = None
    created = False
    try:
        engine = manager.engine
        existing = {name.upper() for name in sa.inspect(engine).get_table_names()}
        application_names = {name.upper() for name in Base.metadata.tables}
        assert not existing, "隔离测试数据库非空，拒绝运行完整结构初始化"
        created = True
        monkeypatch.setattr(app_database, "DATABASE", manager)
        app_database.update_db()
        available = {name.upper() for name in sa.inspect(engine).get_table_names()}
        assert application_names <= available
        alembic_config = AlembicConfig()
        alembic_config.set_main_option(
            "script_location", os.path.join(Config().get_root_path(), "db_scripts"))
        head = ScriptDirectory.from_config(alembic_config).get_current_head()
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version").scalar_one() == head
    finally:
        if engine is not None:
            manager.close_session()
            if created:
                Base.metadata.drop_all(engine)
                if "alembic_version" in sa.inspect(engine).get_table_names():
                    sa.Table("alembic_version", sa.MetaData(), autoload_with=engine).drop(engine)
            engine.dispose()
