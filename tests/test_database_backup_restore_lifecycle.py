"""SQLite complete backup/upload/check/restore task lifecycle."""

import io
import os
import sqlite3
import time
import zipfile
from collections import namedtuple

import pytest
from sqlalchemy import text

import app.db as app_database
import app.db.maintenance as maintenance
from app.db.manager import DATABASE
from app.db.models import MEDIASYNCITEMS, RECOGNITIONREQUEST, TRANSFERHISTORY
from app.utils.storage_jobs import StorageJobStore
from tests.test_sqlite_file_migration import (_create_media_database,
                                              _create_user_database)


class _Config:
    def __init__(self, directory):
        self.directory = str(directory)

    def get_config_path(self):
        return self.directory

    def get_config(self, node=None):
        if node == "database":
            return {"type": "sqlite"}
        return {"database": {"type": "sqlite"}}

    @staticmethod
    def get_root_path():
        return os.path.dirname(os.path.dirname(__file__))


def _wait_for_job(jobs, job_id, expected, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job and job["status"] in expected:
            worker = maintenance._WORKERS.get(job_id)
            if worker:
                worker.join(timeout=5)
            return job
        time.sleep(0.02)
    pytest.fail(f"database job {job_id} did not finish: {jobs.get(job_id)}")


def _prepare_lifecycle(tmp_path, monkeypatch):
    # Import before replacing Config: media package initialization reads global
    # proxy settings during import.
    from app.media.recognition import records as recognition_records

    config = _Config(tmp_path)
    jobs = StorageJobStore(str(tmp_path / ".maintenance" / "storage-jobs.sqlite3"))
    monkeypatch.setattr("config.Config", lambda: config)
    monkeypatch.setattr("app.db.Config", lambda: config)
    monkeypatch.setattr(maintenance, "STORAGE_JOBS", jobs)
    monkeypatch.setattr("app.db.migration.STORAGE_JOBS", jobs)
    monkeypatch.setattr("app.db.manager.STORAGE_JOBS", jobs)
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

    DATABASE.dispose()
    app_database.update_db()
    return jobs, restart_calls


def _create_and_check_backup(jobs):
    backup = maintenance.start_database_backup({"idempotency_key": "lifecycle-backup"})
    assert backup["code"] == 0, backup
    backup_job = _wait_for_job(jobs, backup["job_id"], {"SUCCEEDED", "FAILED"})
    assert backup_job["status"] == "SUCCEEDED", backup_job.get("error")
    artifact, _filename = maintenance.database_backup_path(backup["job_id"])

    with open(artifact, "rb") as stream:
        uploaded = maintenance.upload_database_backup(stream)
    assert uploaded["code"] == 0, uploaded
    checked = maintenance.check_database_backup({"upload_id": uploaded["upload_id"]})
    assert checked["code"] == 0, checked
    check_job = _wait_for_job(jobs, checked["job_id"], {"READY", "FAILED"})
    assert check_job["status"] == "READY", check_job.get("error")
    assert check_job["result"]["full_database"] is True
    return checked


def _insert_restore_item(title):
    with DATABASE.engine.begin() as connection:
        connection.execute(MEDIASYNCITEMS.__table__.insert().values(
            SERVER="emby", ITEM_ID="restore-item", ITEM_TYPE="Movie",
            TITLE=title, PATH="/media/before.mkv", JSON='{"source":"original"}'))
        connection.execute(TRANSFERHISTORY.__table__.insert().values(
            ID=71, TITLE=title, SOURCE_PATH="/downloads/before.mkv"))
        connection.execute(RECOGNITIONREQUEST.__table__.insert().values(
            REQUEST_ID="restore-request", ORIGINAL_NAME="original-name", SUMMARY=title))


def _create_restore_startup_job(jobs, status, result=None):
    from app.db.migration import _current_settings, _fingerprint

    _settings = _current_settings()
    job, created = jobs.create(
        "database_restore", {"fingerprint": _fingerprint(_settings)},
        idempotency_key=f"startup-recovery:{status}")
    assert created
    jobs.update(job["job_id"], status=status, stage=status, result=result)
    with jobs.maintenance_lock(exclusive=True):
        jobs.set_maintenance_gate("database", job["job_id"], status)
    return job["job_id"]


def test_sqlite_backup_upload_check_and_restore_replaces_data_transactionally(
        tmp_path, monkeypatch):
    jobs, restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("before backup")
    checked = _create_and_check_backup(jobs)

    with DATABASE.engine.begin() as connection:
        connection.execute(text("UPDATE MEDIASYNC_ITEMS SET TITLE='after backup' "
                                "WHERE SERVER='emby' AND ITEM_ID='restore-item'"))

    declined = maintenance.start_database_restore({"check_id": checked["job_id"]})
    assert declined["code"] == 1
    restored = maintenance.start_database_restore({"check_id": checked["job_id"],
                                                   "confirm_replace": True})
    assert restored["code"] == 0, restored
    restore_job = _wait_for_job(jobs, restored["job_id"],
                                {"RESTART_PENDING", "FAILED", "RECOVERY_REQUIRED"})
    assert restore_job["status"] == "RESTART_PENDING", restore_job.get("error")
    assert restore_job["result"]["restore_validated"] is True
    assert restore_job["result"]["restart_required"] is True
    assert restart_calls == ["scheduled"]
    assert maintenance.retry_database_restore_restart(restored["job_id"]) is True
    assert restart_calls == ["scheduled", "scheduled"]

    with DATABASE.engine.connect() as connection:
        title = connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                        "WHERE SERVER='emby' AND ITEM_ID='restore-item'")).scalar_one()
        assert title == "before backup"
        assert connection.execute(text("SELECT TITLE FROM TRANSFER_HISTORY WHERE ID=71")).scalar_one() \
            == "before backup"
        assert connection.execute(text("SELECT SUMMARY FROM RECOGNITION_REQUEST "
                                       "WHERE REQUEST_ID='restore-request'")).scalar_one() == "before backup"
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    assert jobs.maintenance_gate("database")["job_id"] == restored["job_id"]
    assert maintenance.database_backup_path(restored["job_id"])

    with jobs.maintenance_lock(exclusive=True):
        jobs.clear_maintenance_gate("database", restored["job_id"])
    with DATABASE.engine.begin() as connection:
        connection.execute(MEDIASYNCITEMS.__table__.insert().values(
            SERVER="emby", ITEM_ID="post-restore-write", ITEM_TYPE="Movie",
            TITLE="new write", PATH="/media/new.mkv", JSON='{"source":"post-restore"}'))
    with DATABASE.engine.connect() as connection:
        assert connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                       "WHERE SERVER='emby' AND ITEM_ID='post-restore-write'")) \
            .scalar_one() == "new write"
    DATABASE.dispose()


def test_startup_recovery_keeps_original_database_after_prepublication_interruption(
        tmp_path, monkeypatch):
    jobs, _restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("still active")
    job_id = _create_restore_startup_job(jobs, "BUILDING_STAGE")
    stage_dir = tmp_path / "db" / ".staging" / f"database-restore-{job_id}"
    stage_dir.mkdir(parents=True)
    (stage_dir / "partial.sqlite3").write_bytes(b"partial stage")

    with jobs.maintenance_lock(exclusive=True):
        assert maintenance.verify_database_maintenance_startup(jobs.get(job_id)) is True

    recovered = jobs.get(job_id)
    assert recovered["status"] == "INTERRUPTED"
    assert jobs.maintenance_gate("database") is None
    assert not stage_dir.exists()
    with DATABASE.engine.connect() as connection:
        assert connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                       "WHERE ITEM_ID='restore-item'")).scalar_one() == "still active"
    DATABASE.dispose()


def test_startup_recovery_validates_restore_pending_restart_before_reopening_database(
        tmp_path, monkeypatch):
    jobs, _restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("restored data")
    digests = maintenance._digest_tables(DATABASE.engine)
    job_id = _create_restore_startup_job(
        jobs, "RESTART_PENDING", {"restored_digests": digests,
                                   "restore_validated": True})

    with jobs.maintenance_lock(exclusive=True):
        assert maintenance.verify_database_maintenance_startup(jobs.get(job_id)) is True

    recovered = jobs.get(job_id)
    assert recovered["status"] == "SUCCEEDED"
    assert recovered["result"]["startup_recovered"] is True
    assert recovered["result"]["restart_required"] is False
    assert jobs.maintenance_gate("database") is None
    with DATABASE.engine.connect() as connection:
        assert connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                       "WHERE ITEM_ID='restore-item'")).scalar_one() == "restored data"
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    DATABASE.dispose()


def test_legacy_complete_dual_backup_restores_to_fixed_sqlite_path(
        tmp_path, monkeypatch):
    jobs, _restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    legacy_dir = tmp_path / "legacy-export"
    legacy_dir.mkdir()
    user_path = legacy_dir / "user.db"
    media_path = legacy_dir / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    archive_path = legacy_dir / "old-backup.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(user_path, "user.db")
        archive.write(media_path, "media.db")

    with archive_path.open("rb") as stream:
        uploaded = maintenance.upload_database_backup(stream)
    assert uploaded["code"] == 0, uploaded
    checked = maintenance.check_database_backup({"upload_id": uploaded["upload_id"]})
    assert checked["code"] == 0, checked
    check_job = _wait_for_job(jobs, checked["job_id"], {"READY", "FAILED"})
    assert check_job["status"] == "READY", check_job.get("error")
    assert check_job["result"]["legacy_converted"] is True

    restored = maintenance.start_database_restore({"check_id": checked["job_id"],
                                                   "confirm_replace": True})
    assert restored["code"] == 0, restored
    restore_job = _wait_for_job(jobs, restored["job_id"],
                                {"RESTART_PENDING", "FAILED", "RECOVERY_REQUIRED"})
    assert restore_job["status"] == "RESTART_PENDING", restore_job.get("error")
    assert maintenance._sqlite_path() == str(tmp_path / "db" / "nastools.db")
    with DATABASE.engine.connect() as connection:
        assert connection.execute(text("SELECT NAME FROM CONFIG_USERS WHERE ID=17")) \
            .scalar_one() == "管理员"
        assert connection.execute(text("SELECT ITEM_ID FROM MEDIASYNC_ITEMS "
                                       "WHERE ID=31")).scalar_one() == "movie-31"
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    DATABASE.dispose()


def test_sqlite_restore_failure_after_publish_restores_pre_restore_database(
        tmp_path, monkeypatch):
    jobs, restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("before backup")
    checked = _create_and_check_backup(jobs)
    with DATABASE.engine.begin() as connection:
        connection.execute(text("UPDATE MEDIASYNC_ITEMS SET TITLE='keep this value' "
                                "WHERE SERVER='emby' AND ITEM_ID='restore-item'"))
        connection.execute(text("UPDATE TRANSFER_HISTORY SET TITLE='keep this value' WHERE ID=71"))
        connection.execute(text("UPDATE RECOGNITION_REQUEST SET SUMMARY='keep this value' "
                                "WHERE REQUEST_ID='restore-request'"))

    verify_revision = maintenance._verify_revision
    verify_calls = []

    def fail_after_publish(engine):
        verify_calls.append(engine)
        if len(verify_calls) == 3:
            raise RuntimeError("injected post-publish verification failure")
        return verify_revision(engine)

    monkeypatch.setattr(maintenance, "_verify_revision", fail_after_publish)
    restored = maintenance.start_database_restore({"check_id": checked["job_id"],
                                                   "confirm_replace": True})
    assert restored["code"] == 0, restored
    restore_job = _wait_for_job(jobs, restored["job_id"], {"FAILED", "RECOVERY_REQUIRED"})
    assert restore_job["status"] == "FAILED", restore_job.get("error")
    assert "injected post-publish verification failure" in restore_job["error"]
    assert restart_calls == []

    with DATABASE.engine.connect() as connection:
        title = connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                        "WHERE SERVER='emby' AND ITEM_ID='restore-item'")).scalar_one()
        assert title == "keep this value"
        assert connection.execute(text("SELECT TITLE FROM TRANSFER_HISTORY WHERE ID=71")).scalar_one() \
            == "keep this value"
        assert connection.execute(text("SELECT SUMMARY FROM RECOGNITION_REQUEST "
                                       "WHERE REQUEST_ID='restore-request'")).scalar_one() \
            == "keep this value"
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    assert jobs.maintenance_gate("database") is None
    DATABASE.dispose()


def test_sqlite_restore_checkpoint_failure_before_publication_keeps_active_database(
        tmp_path, monkeypatch):
    jobs, restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("backup source")
    checked = _create_and_check_backup(jobs)
    with DATABASE.engine.begin() as connection:
        connection.execute(text("UPDATE MEDIASYNC_ITEMS SET TITLE='active source' "
                                "WHERE SERVER='emby' AND ITEM_ID='restore-item'"))

    original_check = maintenance._check_checkpoint

    def fail_restored_stage_checkpoint(result, stage):
        if stage == "SQLite 恢复暂存库":
            raise RuntimeError("injected stage checkpoint failure")
        return original_check(result, stage)

    monkeypatch.setattr(maintenance, "_check_checkpoint", fail_restored_stage_checkpoint)
    restored = maintenance.start_database_restore({"check_id": checked["job_id"],
                                                   "confirm_replace": True})
    assert restored["code"] == 0, restored
    restore_job = _wait_for_job(jobs, restored["job_id"], {"FAILED", "RECOVERY_REQUIRED"})
    assert restore_job["status"] == "FAILED", restore_job.get("error")
    assert "injected stage checkpoint failure" in restore_job["error"]
    assert restart_calls == []
    assert jobs.maintenance_gate("database") is None
    with DATABASE.engine.connect() as connection:
        assert connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                       "WHERE ITEM_ID='restore-item'")).scalar_one() == "active source"
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    DATABASE.dispose()


def test_sqlite_restore_snapshot_failure_keeps_active_database_and_clears_gate(
        tmp_path, monkeypatch):
    jobs, restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("backup source")
    checked = _create_and_check_backup(jobs)
    with DATABASE.engine.begin() as connection:
        connection.execute(text("UPDATE MEDIASYNC_ITEMS SET TITLE='active source' "
                                "WHERE SERVER='emby' AND ITEM_ID='restore-item'"))

    def fail_before_snapshot(*_args, **_kwargs):
        raise RuntimeError("injected pre-restore snapshot failure")

    monkeypatch.setattr("app.db.migration._snapshot_source", fail_before_snapshot)
    restored = maintenance.start_database_restore({"check_id": checked["job_id"],
                                                   "confirm_replace": True})
    assert restored["code"] == 0, restored
    restore_job = _wait_for_job(jobs, restored["job_id"], {"FAILED", "RECOVERY_REQUIRED"})
    assert restore_job["status"] == "FAILED", restore_job.get("error")
    assert "injected pre-restore snapshot failure" in restore_job["error"]
    assert restart_calls == []
    assert jobs.maintenance_gate("database") is None
    with DATABASE.engine.connect() as connection:
        assert connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                       "WHERE ITEM_ID='restore-item'")).scalar_one() == "active source"
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    DATABASE.dispose()


def test_sqlite_restore_partial_sidecar_move_is_recovered_before_failure(
        tmp_path, monkeypatch):
    jobs, restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("backup source")
    checked = _create_and_check_backup(jobs)
    database_path = maintenance._sqlite_path()
    DATABASE.dispose()
    sidecar_writer = sqlite3.connect(database_path, timeout=5, isolation_level=None)
    try:
        sidecar_writer.execute("PRAGMA journal_mode=WAL")
        sidecar_writer.execute("UPDATE MEDIASYNC_ITEMS SET TITLE='active source' "
                               "WHERE SERVER='emby' AND ITEM_ID='restore-item'")
        assert os.path.isfile(database_path + "-wal")
        assert os.path.isfile(database_path + "-shm")

        original_replace = maintenance.os.replace

        def fail_while_moving_shm(source, destination):
            if source == database_path + "-shm":
                raise OSError("injected auxiliary-file move failure")
            return original_replace(source, destination)

        monkeypatch.setattr(maintenance.os, "replace", fail_while_moving_shm)
        restored = maintenance.start_database_restore({"check_id": checked["job_id"],
                                                       "confirm_replace": True})
        assert restored["code"] == 0, restored
        restore_job = _wait_for_job(
            jobs, restored["job_id"], {"FAILED", "RECOVERY_REQUIRED"})
        assert restore_job["status"] == "FAILED", restore_job.get("error")
        assert "injected auxiliary-file move failure" in restore_job["error"]
        assert restart_calls == []
        assert jobs.maintenance_gate("database") is None
    finally:
        sidecar_writer.close()

    with DATABASE.engine.connect() as connection:
        assert connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                       "WHERE ITEM_ID='restore-item'")).scalar_one() == "active source"
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    DATABASE.dispose()


def test_sqlite_restore_task_marker_failure_holds_gate_until_restart_retry(
        tmp_path, monkeypatch):
    jobs, restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("backup source")
    checked = _create_and_check_backup(jobs)
    with DATABASE.engine.begin() as connection:
        connection.execute(text("UPDATE MEDIASYNC_ITEMS SET TITLE='active source' "
                                "WHERE SERVER='emby' AND ITEM_ID='restore-item'"))

    original_update = jobs.update
    injected = []

    def fail_restart_pending_marker(job_id, *args, **kwargs):
        if kwargs.get("status") == "RESTART_PENDING" and not injected:
            injected.append(True)
            raise RuntimeError("injected restore task marker failure")
        return original_update(job_id, *args, **kwargs)

    monkeypatch.setattr(jobs, "update", fail_restart_pending_marker)
    restored = maintenance.start_database_restore({"check_id": checked["job_id"],
                                                   "confirm_replace": True})
    assert restored["code"] == 0, restored
    restore_job = _wait_for_job(
        jobs, restored["job_id"], {"RECOVERY_REQUIRED", "FAILED", "RESTART_PENDING"})
    assert restore_job["status"] == "RECOVERY_REQUIRED", restore_job.get("error")
    assert "injected restore task marker failure" in restore_job["error"]
    assert jobs.maintenance_gate("database")["job_id"] == restore_job["job_id"]
    assert restart_calls == []

    assert maintenance.retry_database_restore_restart(restore_job["job_id"])
    pending = jobs.get(restore_job["job_id"])
    assert pending["status"] == "RESTART_PENDING"
    assert restart_calls == ["scheduled"]
    with jobs.maintenance_lock(exclusive=True):
        assert maintenance.verify_database_maintenance_startup(pending) is True
    assert jobs.get(restore_job["job_id"])["status"] == "SUCCEEDED"
    assert jobs.maintenance_gate("database") is None
    with DATABASE.engine.connect() as connection:
        assert connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                       "WHERE ITEM_ID='restore-item'")).scalar_one() == "backup source"
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    DATABASE.dispose()


def test_sqlite_restore_failure_before_atomic_replace_preserves_active_database(
        tmp_path, monkeypatch):
    jobs, restart_calls = _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("backup source")
    checked = _create_and_check_backup(jobs)
    with DATABASE.engine.begin() as connection:
        connection.execute(text("UPDATE MEDIASYNC_ITEMS SET TITLE='active source' "
                                "WHERE SERVER='emby' AND ITEM_ID='restore-item'"))

    original_replace = maintenance.os.replace

    def fail_staged_main_replace(source, destination):
        if os.path.basename(source) == "nastools.restored.sqlite3":
            raise OSError("injected before atomic SQLite replacement")
        return original_replace(source, destination)

    monkeypatch.setattr(maintenance.os, "replace", fail_staged_main_replace)
    restored = maintenance.start_database_restore({"check_id": checked["job_id"],
                                                   "confirm_replace": True})
    assert restored["code"] == 0, restored
    restore_job = _wait_for_job(jobs, restored["job_id"], {"FAILED", "RECOVERY_REQUIRED"})
    assert restore_job["status"] == "FAILED", restore_job.get("error")
    assert "injected before atomic SQLite replacement" in restore_job["error"]
    assert restart_calls == []
    assert jobs.maintenance_gate("database") is None
    with DATABASE.engine.connect() as connection:
        assert connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                       "WHERE ITEM_ID='restore-item'")).scalar_one() == "active source"
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    DATABASE.dispose()


def test_interrupted_or_space_limited_upload_removes_partial_file_and_preserves_database(
        tmp_path, monkeypatch):
    _prepare_lifecycle(tmp_path, monkeypatch)
    _insert_restore_item("keep active database")
    upload_root = maintenance._upload_root()
    os.makedirs(upload_root, exist_ok=True)

    class InterruptedStream:
        def __init__(self):
            self.reads = 0

        def read(self, _size):
            self.reads += 1
            if self.reads == 1:
                return b"partial archive"
            raise OSError("injected upload interruption")

    interrupted = maintenance.upload_database_backup(InterruptedStream())
    assert interrupted["code"] == 1
    assert os.listdir(upload_root) == []

    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(maintenance.shutil, "disk_usage",
                        lambda _path: usage(100, 99, 1))
    no_space = maintenance.upload_database_backup(io.BytesIO(b"complete archive"))
    assert no_space["code"] == 1
    assert "剩余空间不足" in no_space["msg"]
    assert os.listdir(upload_root) == []

    with DATABASE.engine.connect() as connection:
        title = connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS "
                                        "WHERE SERVER='emby' AND ITEM_ID='restore-item'")).scalar_one()
        assert title == "keep active database"
    DATABASE.dispose()
