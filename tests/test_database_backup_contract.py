"""Run the full backup archive contract against each isolated database engine."""

import json
import os

import pytest
import sqlalchemy as sa

import app.db as app_database
import app.db.maintenance as maintenance
from app.db.manager import DatabaseManager
from app.db.models import Base, MEDIASYNCITEMS
@pytest.mark.parametrize("backend_name", ("sqlite", "postgresql", "mariadb", "mysql"))
def test_full_backup_archive_exports_and_validates_on_each_engine(
        backend_name, tmp_path, monkeypatch):
    if backend_name == "sqlite":
        from app.db.backends.sqlite import SQLiteBackend

        monkeypatch.setattr(SQLiteBackend, "path", staticmethod(
            lambda: str(tmp_path / "db" / "nastools.db")))
        settings = {"type": "sqlite"}
    else:
        settings_by_backend = json.loads(
            os.environ.get("NASTOOLS_TEST_DATABASE_SETTINGS", "{}"))
        settings = settings_by_backend.get(backend_name)
        if not settings:
            pytest.skip(f"未配置隔离的 {backend_name} 测试库")
        if settings.get("type") != backend_name:
            pytest.fail(f"{backend_name} 测试库配置类型不匹配")

    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_read_settings", lambda: settings)
    engine = None
    restore_engine = None
    created = False

    class NoopJobs:
        @staticmethod
        def cancel_requested(_job_id):
            return False

        @staticmethod
        def update(*_args, **_kwargs):
            return None

    try:
        engine = manager.engine
        existing = {name.upper() for name in sa.inspect(engine).get_table_names()}
        assert not existing, "隔离测试数据库非空，拒绝运行备份归档测试"
        created = True
        monkeypatch.setattr(app_database, "DATABASE", manager)
        app_database.update_db()
        with engine.begin() as connection:
            connection.execute(MEDIASYNCITEMS.__table__.insert().values(
                SERVER="backup-contract",
                ITEM_ID="中文-emoji-🧪",
                TITLE="测试备份标题",
                PATH="/long/" + "路径/" * 500,
                JSON=json.dumps({"payload": "恢复验证", "value": 17}, ensure_ascii=False),
            ))

        monkeypatch.setattr(maintenance, "STORAGE_JOBS", NoopJobs())
        archive_path = tmp_path / f"{backend_name}-full-backup.zip"
        with engine.connect() as connection:
            manifest = maintenance._export_backup(
                connection, str(archive_path), "isolated-backup-contract", backend_name)

        checked = maintenance._validate_backup_archive(str(archive_path))
        assert checked["manifest"]["source_database_type"] == backend_name
        assert checked["manifest_sha256"]
        assert checked["content_digests"]["MEDIASYNC_ITEMS"]["rows"] == 1
        assert manifest["tables"]["MEDIASYNC_ITEMS"]["rows"] == 1

        restore_engine = sa.create_engine(f"sqlite:///{tmp_path / 'restored.sqlite3'}")
        Base.metadata.create_all(restore_engine)
        with restore_engine.begin() as connection:
            restored = maintenance._import_backup_data(
                str(archive_path), connection, "isolated-restore-contract", "BUILDING_STAGE")
        assert restored["MEDIASYNC_ITEMS"]["rows"] == 1
        with restore_engine.connect() as connection:
            row = connection.execute(sa.select(
                MEDIASYNCITEMS.__table__.c.TITLE,
                MEDIASYNCITEMS.__table__.c.ITEM_ID,
            )).one()
        assert row == ("测试备份标题", "中文-emoji-🧪")
    finally:
        if restore_engine is not None:
            restore_engine.dispose()
        if engine is not None:
            manager.close_session()
            if created:
                Base.metadata.drop_all(engine)
                if "alembic_version" in sa.inspect(engine).get_table_names():
                    sa.Table("alembic_version", sa.MetaData(), autoload_with=engine).drop(engine)
            engine.dispose()
