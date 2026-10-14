"""Run the full backup archive contract against each isolated database engine."""

import json
import os

import pytest
import sqlalchemy as sa

import app.db as app_database
import app.db.maintenance as maintenance
from app.db.backends import create_backend
from app.db.manager import DatabaseManager
from app.db.models import Base, MEDIASYNCITEMS, RSSTVS


def test_backup_decoder_normalizes_legacy_blank_nullable_integer():
    row = {column.name: None for column in RSSTVS.__table__.columns}
    row["ID"] = 991
    row["DOWNLOAD_SETTING"] = ""

    decoded = maintenance._decode_backup_row(row, RSSTVS.__table__)

    assert decoded["DOWNLOAD_SETTING"] is None

    row["DOWNLOAD_SETTING"] = "not-an-integer"
    with pytest.raises(ValueError, match="DOWNLOAD_SETTING 的类型不匹配"):
        maintenance._decode_backup_row(row, RSSTVS.__table__)


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


@pytest.mark.parametrize("source_type", ("sqlite", "postgresql", "mariadb", "mysql"))
@pytest.mark.parametrize("target_type", ("sqlite", "postgresql", "mariadb", "mysql"))
def test_backup_import_matrix_preserves_rows_and_allows_new_writes(
        source_type, target_type, tmp_path, monkeypatch):
    remote_settings = json.loads(os.environ.get("NASTOOLS_TEST_DATABASE_SETTINGS", "{}"))
    required_remote = {kind for kind in (source_type, target_type) if kind != "sqlite"}
    missing = sorted(kind for kind in required_remote if not remote_settings.get(kind))
    if missing:
        pytest.skip(f"未配置隔离数据库：{', '.join(missing)}")
    for kind in required_remote:
        if remote_settings[kind].get("type") != kind:
            pytest.fail(f"{kind} 测试库配置类型不匹配")

    class NoopJobs:
        @staticmethod
        def cancel_requested(_job_id):
            return False

        @staticmethod
        def update(*_args, **_kwargs):
            return None

    monkeypatch.setattr(maintenance, "STORAGE_JOBS", NoopJobs())
    same_remote_database = source_type == target_type and source_type != "sqlite"

    def make_engine(kind, role):
        if kind == "sqlite":
            path = tmp_path / f"{role}.sqlite3"
            return sa.create_engine(f"sqlite:///{path}")
        return create_backend(kind).create_engine(remote_settings[kind])

    source_engine = make_engine(source_type, "source")
    target_engine = (source_engine if same_remote_database
                     else make_engine(target_type, "target"))
    source_created = target_created = False
    try:
        assert not sa.inspect(source_engine).get_table_names(), \
            f"{source_type} 源测试库非空，拒绝运行备份矩阵"
        if target_engine is not source_engine:
            assert not sa.inspect(target_engine).get_table_names(), \
                f"{target_type} 目标测试库非空，拒绝运行备份矩阵"

        Base.metadata.create_all(source_engine)
        source_created = True
        with source_engine.begin() as connection:
            source_id = connection.execute(MEDIASYNCITEMS.__table__.insert().values(
                SERVER="backup-matrix",
                ITEM_ID="matrix-中文-🧪",
                TITLE="矩阵备份标题",
                PATH="/matrix/" + "路径/" * 100,
                JSON=json.dumps({"source": source_type, "value": "雪"}, ensure_ascii=False),
            )).inserted_primary_key[0]

        archive_path = tmp_path / f"{source_type}-to-{target_type}.zip"
        with source_engine.connect() as connection:
            maintenance._export_backup(
                connection, str(archive_path), f"{source_type}-{target_type}", source_type)
        checked = maintenance._validate_backup_archive(str(archive_path))
        assert checked["manifest"]["source_database_type"] == source_type

        if target_engine is source_engine:
            Base.metadata.drop_all(target_engine)
            source_created = False
        Base.metadata.create_all(target_engine)
        target_created = True
        with target_engine.begin() as connection:
            restored = maintenance._import_backup_data(
                str(archive_path), connection, f"{source_type}-{target_type}", "BUILDING_STAGE")
        assert restored["MEDIASYNC_ITEMS"]["rows"] == 1

        target_backend = create_backend(target_type)
        calibrate = getattr(target_backend, "calibrate_sequences", None)
        if calibrate:
            calibrate(target_engine, Base.metadata)
        with target_engine.begin() as connection:
            row = connection.execute(sa.select(
                MEDIASYNCITEMS.__table__.c.TITLE,
                MEDIASYNCITEMS.__table__.c.ITEM_ID,
                MEDIASYNCITEMS.__table__.c.ID,
            )).one()
            assert row == ("矩阵备份标题", "matrix-中文-🧪", source_id)
            inserted_id = connection.execute(MEDIASYNCITEMS.__table__.insert().values(
                SERVER="backup-matrix",
                ITEM_ID="matrix-after-restore",
                TITLE="恢复后新增",
            )).inserted_primary_key[0]
            assert inserted_id > source_id
    finally:
        if target_created:
            Base.metadata.drop_all(target_engine)
        if source_created and source_engine is not target_engine:
            Base.metadata.drop_all(source_engine)
        if target_engine is not source_engine:
            target_engine.dispose()
        source_engine.dispose()
