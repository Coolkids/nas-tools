"""真实远程数据库上的备份恢复和迁移任务生命周期。"""

import copy
import json
import os
import time

import pytest
import ruamel.yaml
from sqlalchemy import inspect, select, text

import app.db as app_database
import app.db.maintenance as maintenance
import app.db.migration as migration
import app.db.manager as database_manager
from app.db.backends import create_backend
from app.db.manager import DATABASE
from app.db.models import Base, MEDIASYNCITEMS
from app.utils.storage_jobs import StorageJobStore


REMOTE_TYPES = ("postgresql", "mariadb", "mysql")
MIGRATION_PAIRS = tuple((source, target) for source in REMOTE_TYPES
                        for target in REMOTE_TYPES if source != target)


class _Config:
    def __init__(self, directory, settings):
        self.directory = str(directory)
        self._config_path = os.path.join(self.directory, "config.yaml")
        self.values = {"database": copy.deepcopy(settings)}
        os.makedirs(self.directory, exist_ok=True)
        self._write()

    def _write(self):
        with open(self._config_path, "w", encoding="utf-8") as stream:
            ruamel.yaml.YAML().dump(self.values, stream)
        os.chmod(self._config_path, 0o600)

    def get_config_path(self):
        return self.directory

    def get_config(self, node=None):
        return self.values if node is None else self.values.get(node)

    @staticmethod
    def get_root_path():
        return os.path.dirname(os.path.dirname(__file__))

    @staticmethod
    def get_proxies():
        return {}

    def save_config(self, values):
        self.values = copy.deepcopy(values)
        self._write()
        return True


def _settings(backend_name):
    settings = json.loads(os.environ.get("NASTOOLS_TEST_DATABASE_SETTINGS", "{}"))
    selected = settings.get(backend_name)
    if not selected:
        pytest.skip(f"未配置隔离的 {backend_name} 测试库")
    if selected.get("type") != backend_name:
        pytest.fail(f"{backend_name} 测试库配置类型不匹配")
    return copy.deepcopy(selected)


def _empty_remote_database(settings):
    engine = create_backend(settings["type"]).create_engine(settings)
    try:
        objects = migration._database_objects(engine)
        assert not objects, \
            f"{settings['type']} 隔离测试库非空，拒绝运行远程生命周期测试：{objects[:20]}"
    finally:
        engine.dispose()


def _drop_owned_schema(settings):
    engine = create_backend(settings["type"]).create_engine(settings)
    try:
        Base.metadata.drop_all(engine, checkfirst=True)
        inspector = inspect(engine)
        if inspector.has_table("alembic_version"):
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE alembic_version"))
    finally:
        engine.dispose()


def _install_runtime(settings, tmp_path, monkeypatch):
    from app.media.recognition import records as recognition_records

    _empty_remote_database(settings)
    config = _Config(tmp_path / "config", settings)
    jobs = StorageJobStore(str(tmp_path / ".maintenance" / "jobs.sqlite3"))
    monkeypatch.setattr("config.Config", lambda: config)
    monkeypatch.setattr(app_database, "Config", lambda: config)
    monkeypatch.setattr(maintenance, "STORAGE_JOBS", jobs)
    monkeypatch.setattr(migration, "STORAGE_JOBS", jobs)
    monkeypatch.setattr(database_manager, "STORAGE_JOBS", jobs)
    monkeypatch.setattr(migration, "_WORKERS", {})
    monkeypatch.setattr(maintenance, "_WORKERS", {})
    monkeypatch.setattr(maintenance, "_clear_restore_derived_state",
                        lambda _job_id: {"cleared": True})
    monkeypatch.setattr(recognition_records, "quiesce_recognition",
                        lambda **_kwargs: None)
    monkeypatch.setattr(recognition_records, "replay_recognition_spool",
                        lambda: {"pending": False})
    monkeypatch.setattr(recognition_records, "resume_recognition", lambda: None)
    restart_calls = []
    monkeypatch.setattr(maintenance, "schedule_restart",
                        lambda: restart_calls.append("scheduled"))
    monkeypatch.setattr(migration, "schedule_restart",
                        lambda: restart_calls.append("scheduled"))

    DATABASE.dispose()
    app_database.update_db()
    return config, jobs, restart_calls


def _wait_for_job(jobs, workers, job_id, expected, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job and job["status"] in expected:
            worker = workers.get(job_id)
            if worker:
                worker.join(timeout=10)
            return jobs.get(job_id)
        time.sleep(0.05)
    pytest.fail(f"远程数据库任务未完成：{jobs.get(job_id)}")


def _insert_media_item(item_id, title):
    with DATABASE.engine.begin() as connection:
        connection.execute(MEDIASYNCITEMS.__table__.insert().values(
            SERVER="remote-lifecycle", ITEM_ID=item_id, ITEM_TYPE="Movie",
            TITLE=title, PATH=f"/media/{item_id}.mkv",
            JSON=json.dumps({"title": title}, ensure_ascii=False)))


@pytest.mark.parametrize("backend_name", REMOTE_TYPES)
def test_remote_backup_upload_check_restore_and_startup_lifecycle(
        backend_name, tmp_path, monkeypatch):
    settings = _settings(backend_name)
    owned_settings = []
    try:
        _empty_remote_database(settings)
        owned_settings.append(settings)
        _config, jobs, restart_calls = _install_runtime(settings, tmp_path, monkeypatch)
        _insert_media_item("restore-item", "before backup")

        backup = maintenance.start_database_backup({"idempotency_key": "remote-lifecycle"})
        assert backup["code"] == 0, backup
        backup_job = _wait_for_job(
            jobs, maintenance._WORKERS, backup["job_id"], {"SUCCEEDED", "FAILED"})
        assert backup_job["status"] == "SUCCEEDED", backup_job.get("error")
        artifact, _filename = maintenance.database_backup_path(backup["job_id"])
        assert artifact and os.path.isfile(artifact)

        with open(artifact, "rb") as stream:
            uploaded = maintenance.upload_database_backup(stream)
        assert uploaded["code"] == 0, uploaded
        checked = maintenance.check_database_backup({"upload_id": uploaded["upload_id"]})
        assert checked["code"] == 0, checked
        check_job = _wait_for_job(
            jobs, maintenance._WORKERS, checked["job_id"], {"READY", "FAILED"})
        assert check_job["status"] == "READY", check_job.get("error")
        assert check_job["result"]["full_database"] is True

        with DATABASE.engine.begin() as connection:
            table = MEDIASYNCITEMS.__table__
            connection.execute(table.update().where(
                table.c.SERVER == "remote-lifecycle",
                table.c.ITEM_ID == "restore-item",
            ).values(TITLE="after backup"))
        restored = maintenance.start_database_restore({
            "check_id": checked["job_id"], "confirm_replace": True})
        assert restored["code"] == 0, restored
        restore_job = _wait_for_job(
            jobs, maintenance._WORKERS, restored["job_id"],
            {"RESTART_PENDING", "FAILED", "RECOVERY_REQUIRED"})
        assert restore_job["status"] == "RESTART_PENDING", restore_job.get("error")
        assert restart_calls == ["scheduled"]

        DATABASE.dispose()
        with jobs.maintenance_lock(exclusive=True):
            assert maintenance.verify_database_maintenance_startup(
                jobs.get(restored["job_id"]))
        recovered = jobs.get(restored["job_id"])
        assert recovered["status"] == "SUCCEEDED", recovered.get("error")
        assert recovered["result"]["startup_recovered"] is True
        with DATABASE.engine.connect() as connection:
            table = MEDIASYNCITEMS.__table__
            title = connection.execute(select(table.c.TITLE).where(
                table.c.SERVER == "remote-lifecycle",
                table.c.ITEM_ID == "restore-item",
            )).scalar_one()
        assert title == "before backup"
        _insert_media_item("post-restore-write", "new write")
    finally:
        DATABASE.dispose()
        for owned in owned_settings:
            _drop_owned_schema(owned)


@pytest.mark.parametrize(("source_name", "target_name"), MIGRATION_PAIRS)
def test_remote_database_migration_task_copies_and_verifies_before_restart(
        source_name, target_name, tmp_path, monkeypatch):
    source_settings = _settings(source_name)
    target_settings = _settings(target_name)
    owned_settings = []
    try:
        _empty_remote_database(source_settings)
        _empty_remote_database(target_settings)
        owned_settings.extend((source_settings, target_settings))
        config, jobs, restart_calls = _install_runtime(
            source_settings, tmp_path, monkeypatch)
        _empty_remote_database(target_settings)
        _insert_media_item("migration-item", f"{source_name}-source")

        checked = migration.check_database(target_settings)
        assert checked["code"] == 0, checked
        assert checked["check"]["ok"] is True, checked
        started = migration.start_database_migration({
            **target_settings, "check_id": checked["check"]["check_id"],
            "confirm_target_exclusive": True})
        assert started["code"] == 0, started
        job = _wait_for_job(
            jobs, migration._WORKERS, started["job_id"],
            {"RESTART_PENDING", "FAILED", "RECOVERY_REQUIRED"})
        assert job["status"] == "RESTART_PENDING", job.get("error")
        assert job["result"]["rows"] >= 1
        assert config.values["database"]["type"] == target_name
        assert restart_calls == ["scheduled"]

        DATABASE.dispose()
        assert migration.verify_database_startup()
        verified = jobs.get(started["job_id"])
        assert verified["status"] == "SUCCEEDED", verified.get("error")
        with DATABASE.engine.connect() as connection:
            table = MEDIASYNCITEMS.__table__
            title = connection.execute(select(table.c.TITLE).where(
                table.c.SERVER == "remote-lifecycle",
                table.c.ITEM_ID == "migration-item",
            )).scalar_one()
        assert title == f"{source_name}-source"
        _insert_media_item("post-migration-write", "new write")
    finally:
        DATABASE.dispose()
        for owned in owned_settings:
            _drop_owned_schema(owned)
