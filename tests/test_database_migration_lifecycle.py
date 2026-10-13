"""Exercise database migration task orchestration with isolated SQLite fixtures."""

import os
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, text

import app.db.migration as migration
from app.db.models import Base, MEDIASYNCITEMS, SYSTEMDICT
from app.utils.storage_jobs import StorageJobStore


SOURCE_SETTINGS = {
    "type": "postgresql", "host": "source.example", "port": 5432,
    "name": "nastools", "username": "app", "password": "source-secret",
    "connect_timeout": 5, "ssl": {},
}


class _ConfigStub:
    def __init__(self, directory):
        self.directory = str(directory)
        self._config_path = os.path.join(self.directory, "config.yaml")
        self.values = {"database": dict(SOURCE_SETTINGS)}

    def get_config_path(self):
        return self.directory

    def get_config(self, node=None):
        return self.values if node is None else self.values.get(node)

    @staticmethod
    def get_proxies():
        return {}

    def save_config(self, values):
        self.values = values
        return True


class _StartedThread:
    def __init__(self, *, target, args, name, daemon):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


def _install_isolated_state(tmp_path, monkeypatch):
    config = _ConfigStub(tmp_path / "config")
    os.makedirs(config.directory)
    jobs = StorageJobStore(os.path.join(config.directory, ".maintenance", "jobs.sqlite3"))
    monkeypatch.setattr("config.Config", lambda: config)
    monkeypatch.setattr(migration, "STORAGE_JOBS", jobs)
    monkeypatch.setattr("app.db.maintenance.STORAGE_JOBS", jobs)
    monkeypatch.setattr("app.db.manager.STORAGE_JOBS", jobs)
    monkeypatch.setattr(migration, "_WORKERS", {})
    monkeypatch.setattr(migration, "_current_settings", lambda: dict(SOURCE_SETTINGS))
    monkeypatch.setattr(migration.threading, "Thread", _StartedThread)
    return config, jobs


def _ready_check(jobs, candidate, source_settings=None, target_identity=None):
    source_settings = source_settings or SOURCE_SETTINGS
    candidate_fingerprint = migration._fingerprint(candidate)
    source_fingerprint = migration._fingerprint(source_settings)
    target_identity = target_identity or {"instance": "test-target"}
    job, created = jobs.create(
        "database_migration",
        {"candidate": migration._safe_settings(candidate),
         "fingerprint": candidate_fingerprint,
         "source_fingerprint": source_fingerprint,
         "target_identity": target_identity},
        idempotency_key="migration-check")
    assert created
    job = jobs.update(job["job_id"], status="READY", stage="READY",
                      result={"ok": True}, message="test check ready")
    return job["job_id"], target_identity


def _start_values(candidate, check_id, confirm=True):
    values = {**candidate, "check_id": check_id}
    if confirm:
        values["confirm_target_exclusive"] = True
    return values


def test_migration_start_rejects_target_confirmation_and_stale_credentials(
        tmp_path, monkeypatch):
    _config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    candidate = {"type": "postgresql", "host": "target.example", "port": 5432,
                 "name": "target", "username": "app", "password": "target-secret",
                 "connect_timeout": 5, "ssl": {}}
    check_id, target_identity = _ready_check(jobs, candidate)
    monkeypatch.setattr(migration, "_normalize_candidate", lambda _values: candidate)
    monkeypatch.setattr(migration, "_target_identity", lambda _settings: target_identity)

    missing_confirmation = migration.start_database_migration(
        _start_values(candidate, check_id, confirm=False))
    assert missing_confirmation["code"] == 1
    assert "确认目标专用" in missing_confirmation["msg"]
    assert jobs.get(check_id)["status"] == "READY"

    changed_candidate = {**candidate, "name": "different-target"}
    monkeypatch.setattr(migration, "_normalize_candidate",
                        lambda _values: changed_candidate)
    changed = migration.start_database_migration(_start_values(changed_candidate, check_id))
    assert changed["code"] == 1
    assert "连接参数已变化" in changed["msg"]
    assert jobs.get(check_id)["status"] == "READY"

    monkeypatch.setattr(migration, "_normalize_candidate", lambda _values: candidate)
    changed_source = {**SOURCE_SETTINGS, "name": "changed-source"}
    monkeypatch.setattr(migration, "_current_settings", lambda: changed_source)
    source_changed = migration.start_database_migration(_start_values(candidate, check_id))
    assert source_changed["code"] == 1
    assert "活动数据库配置已变化" in source_changed["msg"]
    assert jobs.get(check_id)["status"] == "READY"

    monkeypatch.setattr(migration, "_current_settings", lambda: dict(SOURCE_SETTINGS))
    monkeypatch.setattr(migration, "_target_identity",
                        lambda _settings: {"instance": "replacement-target"})
    target_changed = migration.start_database_migration(_start_values(candidate, check_id))
    assert target_changed["code"] == 1
    assert "目标数据库身份在检查后变化" in target_changed["msg"]
    assert jobs.get(check_id)["status"] == "READY"


def test_migration_start_rejects_expired_or_reused_check(tmp_path, monkeypatch):
    _config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    candidate = {"type": "postgresql", "host": "target.example", "port": 5432,
                 "name": "target", "username": "app", "password": "target-secret",
                 "connect_timeout": 5, "ssl": {}}
    check_id, target_identity = _ready_check(jobs, candidate)
    monkeypatch.setattr(migration, "_normalize_candidate", lambda _values: candidate)
    monkeypatch.setattr(migration, "_target_identity", lambda _settings: target_identity)
    monkeypatch.setattr(migration.time, "time", lambda: jobs.get(check_id)["created_at"] + 601)
    expired = migration.start_database_migration(_start_values(candidate, check_id))
    assert expired["code"] == 1
    assert "已过期" in expired["msg"]
    assert jobs.get(check_id)["status"] == "READY"

    monkeypatch.undo()
    _config, jobs = _install_isolated_state(tmp_path / "reused", monkeypatch)
    check_id, target_identity = _ready_check(jobs, candidate)
    monkeypatch.setattr(migration, "_normalize_candidate", lambda _values: candidate)
    monkeypatch.setattr(migration, "_target_identity", lambda _settings: target_identity)
    started = migration.start_database_migration(_start_values(candidate, check_id))
    assert started["code"] == 0, started
    thread = migration._WORKERS[check_id]
    assert thread.started
    assert jobs.get(check_id)["status"] == "QUIESCING"
    reused = migration.start_database_migration(_start_values(candidate, check_id))
    assert reused["code"] == 1
    assert "已失效" in reused["msg"]


def test_database_check_requires_an_empty_target_and_cleans_its_probe(
        tmp_path, monkeypatch):
    _config, _jobs = _install_isolated_state(tmp_path, monkeypatch)
    candidate = {"type": "postgresql", "host": "target.example", "port": 5432,
                 "name": "target", "username": "app", "password": "secret",
                 "connect_timeout": 5, "ssl": {}}
    monkeypatch.setattr(migration, "_current_database_status",
                        lambda: {"type": "postgresql", "fingerprint": "source"})
    monkeypatch.setattr(migration, "_resolved_addresses",
                        lambda host: ["192.0.2.10" if host == "source.example"
                                      else "192.0.2.20"])
    monkeypatch.setattr(migration, "_server_capabilities",
                        lambda _engine: {"product": "test-sqlite-emulator"})

    occupied_path = os.path.join(str(tmp_path), "occupied-target.sqlite3")
    occupied_engine = create_engine(f"sqlite:///{occupied_path}")
    with occupied_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE user_data (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("CREATE VIEW user_view AS SELECT id FROM user_data")
    monkeypatch.setattr(migration, "_new_engine", lambda *_args, **_kwargs: occupied_engine)
    occupied = migration._database_check(candidate)
    assert occupied["ok"] is False
    assert occupied["reason"] == "远程目标不是空库；请使用新的空数据库"
    assert occupied["conflicts"] == [
        "table:alembic_version", "table:user_data", "view:user_view"]
    occupied_engine.dispose()

    empty_path = os.path.join(str(tmp_path), "empty-target.sqlite3")
    empty_engine = create_engine(f"sqlite:///{empty_path}")
    monkeypatch.setattr(migration, "_new_engine", lambda *_args, **_kwargs: empty_engine)
    ready = migration._database_check(candidate)
    assert ready["ok"] is True, ready
    assert ready["capabilities"] == {"product": "test-sqlite-emulator"}
    assert migration._database_objects(empty_engine) == []
    empty_engine.dispose()


def test_database_check_rejects_external_object_created_after_initial_check(
        tmp_path, monkeypatch):
    _config, _jobs = _install_isolated_state(tmp_path, monkeypatch)
    candidate = {"type": "postgresql", "host": "target.example", "port": 5432,
                 "name": "target", "username": "app", "password": "secret",
                 "connect_timeout": 5, "ssl": {}}
    monkeypatch.setattr(migration, "_current_database_status",
                        lambda: {"type": "postgresql", "fingerprint": "source"})
    monkeypatch.setattr(migration, "_resolved_addresses",
                        lambda host: ["192.0.2.10" if host == "source.example"
                                      else "192.0.2.20"])
    engine = create_engine(f"sqlite:///{tmp_path / 'external-write.sqlite3'}")
    monkeypatch.setattr(migration, "_new_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(migration, "_server_capabilities", lambda _engine: {"product": "test"})

    def add_external_object(_engine):
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE external_after_check (id INTEGER)")

    monkeypatch.setattr(migration, "_probe_engine", add_external_object)
    with pytest.raises(RuntimeError, match="探测对象清理后远程目标仍非空"):
        migration._database_check(candidate)
    engine.dispose()


def test_postgresql_target_scan_includes_user_objects_and_ignores_extension_members(
        monkeypatch):
    class InspectorStub:
        default_schema_name = "public"

        @staticmethod
        def get_schema_names():
            return ["public", "custom_data", "_timescaledb_catalog",
                    "information_schema", "pg_catalog", "pg_toast_1"]

        @staticmethod
        def get_table_names(schema=None):
            if schema == "custom_data":
                return ["shadow_data"]
            if schema == "_timescaledb_catalog":
                return ["bgw_job", "user_extra"]
            return []

        @staticmethod
        def get_view_names(schema=None):
            return []

        @staticmethod
        def get_materialized_view_names(schema=None):
            return []

        @staticmethod
        def get_sequence_names(schema=None):
            return []

    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(migration, "inspect", lambda _engine: InspectorStub())
    monkeypatch.setattr(
        migration, "_postgres_extension_objects",
        lambda _engine: {("_timescaledb_catalog", "bgw_job")})
    assert migration._database_objects(engine) == [
        "table:_timescaledb_catalog.user_extra", "table:custom_data.shadow_data"]


def test_same_remote_database_aliases_are_rejected(tmp_path, monkeypatch):
    _config, _jobs = _install_isolated_state(tmp_path, monkeypatch)
    source = {"type": "postgresql", "host": "db.example", "port": 5432,
              "name": "nastools"}
    candidate = {"type": "postgresql", "host": "192.0.2.30", "port": 5432,
                 "name": "nastools"}
    monkeypatch.setattr(migration, "_resolved_addresses",
                        lambda host: ["192.0.2.30"] if host in {
                            "db.example", "192.0.2.30"} else [])
    assert migration._same_database(source, candidate)


def _build_source(path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(SYSTEMDICT.__table__).values(
            ID=41, TYPE="migration", KEY="source", VALUE="源数据"))
        connection.execute(insert(MEDIASYNCITEMS.__table__).values(
            ID=71, SERVER="emby", ITEM_ID="migration-item", TITLE="迁移媒体"))
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES (?)",
                                   (migration.current_schema_revision(),))
    return engine


def _snapshot_fixture(source_path, snapshot_path):
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    source = sqlite3.connect(source_path)
    snapshot = sqlite3.connect(snapshot_path)
    try:
        source.backup(snapshot)
    finally:
        snapshot.close()
        source.close()
    engine = create_engine(f"sqlite:///{snapshot_path}")
    connection = engine.connect()
    transaction = connection.begin()
    return engine, connection, transaction, snapshot_path


def _prepare_migration_job(jobs):
    candidate = {"type": "sqlite"}
    target_fingerprint = migration._fingerprint(candidate)
    source_fingerprint = migration._fingerprint(SOURCE_SETTINGS)
    job, created = jobs.create(
        "database_migration",
        {"candidate": candidate, "fingerprint": target_fingerprint,
         "source_fingerprint": source_fingerprint},
        idempotency_key="sqlite-migration")
    assert created
    jobs.update(job["job_id"], status="QUIESCING", stage="QUIESCING")
    return job["job_id"], candidate, source_fingerprint, target_fingerprint


@pytest.mark.parametrize("failure", [None, "copy", "digest", "config-save", "restart"],
                         ids=["publish", "copy-failure", "digest-mismatch",
                              "config-save-failure", "restart-failure"])
def test_remote_to_sqlite_migration_publishes_only_after_validation_and_rolls_back(
        tmp_path, monkeypatch, failure):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    source_path = os.path.join(config.directory, "source.sqlite3")
    source_engine = _build_source(source_path)
    source_engine.dispose()
    target_path = os.path.join(config.directory, "db", "nastools.db")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    sqlite3.connect(target_path).close()  # The checked, empty target placeholder.
    original_target = migration._target_identity({"type": "sqlite"})
    job_id, candidate, source_fingerprint, target_fingerprint = _prepare_migration_job(jobs)
    snapshot_path = os.path.join(
        config.directory, ".maintenance", "snapshots", job_id + ".sqlite3")
    current = {"settings": dict(SOURCE_SETTINGS)}

    def read_current_settings():
        return dict(current["settings"])

    def save_config(_candidate, expected_source):
        assert expected_source == source_fingerprint
        if failure == "config-save":
            raise OSError("injected config write failure")
        current["settings"] = dict(candidate)
        config.values["database"] = dict(candidate)

    monkeypatch.setattr(migration, "_current_settings", read_current_settings)
    monkeypatch.setattr(migration, "_snapshot_source", lambda _settings, _job: _snapshot_fixture(
        source_path, snapshot_path))
    monkeypatch.setattr(migration, "_target_identity", lambda _settings: original_target)
    monkeypatch.setattr(migration, "_save_database_config", save_config)
    recognition_calls = []
    monkeypatch.setattr("app.media.recognition.records.quiesce_recognition",
                        lambda timeout: recognition_calls.append(("quiesce", timeout)))
    monkeypatch.setattr("app.media.recognition.records.replay_recognition_spool",
                        lambda: {"pending": 0})
    monkeypatch.setattr("app.media.recognition.records.resume_recognition",
                        lambda: recognition_calls.append(("resume", None)))
    scheduled = []

    def schedule_restart():
        scheduled.append(True)
        if failure == "restart" and len(scheduled) == 1:
            raise RuntimeError("injected restart scheduling failure")

    monkeypatch.setattr(migration, "schedule_restart", schedule_restart)

    if failure == "copy":
        original_copy = migration._copy_table
        failed_copy = []

        def copy_then_fail(source_connection, target_engine, table):
            original_copy(source_connection, target_engine, table)
            if not failed_copy:
                failed_copy.append(table.name)
                raise OSError("injected disk full during staged copy")

        monkeypatch.setattr(migration, "_copy_table", copy_then_fail)
    elif failure == "digest":
        original_digest = migration._table_digest
        corrupted = []

        def mismatch_target_digest(connection, table, *args, **kwargs):
            count, digest = original_digest(connection, table, *args, **kwargs)
            if (connection.engine.url.database != snapshot_path and not corrupted):
                corrupted.append(table.name)
                return count, "0" * 64
            return count, digest

        monkeypatch.setattr(migration, "_table_digest", mismatch_target_digest)

    migration._run_migration(job_id, candidate, source_fingerprint,
                             target_fingerprint, original_target, job_id)

    result = jobs.get(job_id)
    if failure in {"copy", "digest", "config-save"}:
        assert result["status"] == "FAILED", result
        expected_error = {
            "copy": "injected disk full during staged copy",
            "digest": "内容摘要不一致",
            "config-save": "injected config write failure",
        }[failure]
        assert expected_error in result["error"]
        assert current["settings"] == SOURCE_SETTINGS
        assert scheduled == []
        assert os.path.isfile(target_path)  # Restore the checked empty target.
        with sqlite3.connect(target_path) as connection:
            assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
        assert jobs.maintenance_gate("database") is None
        assert recognition_calls == [("quiesce", 120), ("resume", None)]
        assert not os.path.exists(os.path.join(
            config.directory, "db", ".staging", f"database-migration-{job_id}"))
    else:
        assert result["status"] == "RESTART_PENDING", (
            result["status"], result.get("stage"), result.get("error"), result.get("message"))
        assert result["result"]["rows"] >= 2
        assert result["result"]["tables"]["SYSTEM_DICT"]["rows"] == 1
        assert current["settings"] == candidate
        assert scheduled == [True]
        assert recognition_calls == [("quiesce", 120)]
        assert jobs.maintenance_gate("database")["job_id"] == job_id
        if failure == "restart":
            assert result["error"] == "injected restart scheduling failure"
            assert migration.retry_database_restart(job_id)
            assert scheduled == [True, True]
        from app.db.manager import DATABASE

        DATABASE.dispose()
        try:
            migration.verify_database_startup()
            assert jobs.get(job_id)["status"] == "SUCCEEDED"
            assert jobs.maintenance_gate("database") is None
        finally:
            DATABASE.dispose()
        published_engine = create_engine(f"sqlite:///{target_path}")
        try:
            with published_engine.connect() as connection:
                assert connection.execute(text("SELECT VALUE FROM SYSTEM_DICT WHERE ID=41")).scalar_one() == "源数据"
                assert connection.execute(text("SELECT TITLE FROM MEDIASYNC_ITEMS WHERE ID=71")).scalar_one() == "迁移媒体"
                assert connection.execute(text("SELECT STATUS FROM DATABASE_MIGRATION_MARKERS WHERE ID=:id"),
                                          {"id": job_id}).scalar_one() == "complete"
                assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == migration.current_schema_revision()
        finally:
            published_engine.dispose()


def test_migration_cancellation_at_table_boundary_keeps_source_config_and_empty_target(
        tmp_path, monkeypatch):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    source_path = os.path.join(config.directory, "source.sqlite3")
    source_engine = _build_source(source_path)
    source_engine.dispose()
    target_path = os.path.join(config.directory, "db", "nastools.db")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    sqlite3.connect(target_path).close()
    target_identity = migration._target_identity({"type": "sqlite"})
    job_id, candidate, source_fingerprint, target_fingerprint = _prepare_migration_job(jobs)
    monkeypatch.setattr(migration, "_snapshot_source", lambda _settings, job: _snapshot_fixture(
        source_path, os.path.join(config.directory, ".maintenance", "snapshots", job + ".sqlite3")))
    monkeypatch.setattr(migration, "_target_identity", lambda _settings: target_identity)
    original_copy = migration._copy_table
    copied = []

    def copy_then_cancel(source_connection, target_engine, table):
        result = original_copy(source_connection, target_engine, table)
        if not copied:
            copied.append(table.name)
            assert jobs.request_cancel(job_id)
        return result

    monkeypatch.setattr(migration, "_copy_table", copy_then_cancel)
    scheduled = []
    monkeypatch.setattr(migration, "schedule_restart", lambda: scheduled.append(True))

    migration._run_migration_impl(job_id, candidate, source_fingerprint,
                                  target_fingerprint, target_identity, job_id)

    job = jobs.get(job_id)
    assert copied
    assert job["status"] == "CANCELLED", job
    assert job["stage"] == "CANCELLED"
    assert scheduled == []
    assert jobs.maintenance_gate("database") is None
    assert os.path.isfile(target_path)
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0


def test_remote_migration_rejects_target_objects_added_before_copy_without_deleting_them(
        tmp_path, monkeypatch):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    source_path = os.path.join(config.directory, "source.sqlite3")
    source_engine = _build_source(source_path)
    source_engine.dispose()
    target_path = os.path.join(config.directory, "remote-target.sqlite3")
    target_engine = create_engine(f"sqlite:///{target_path}")
    with target_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE external_data (value TEXT)")
        connection.exec_driver_sql("INSERT INTO external_data VALUES ('keep me')")
    candidate = {"type": "postgresql", "host": "target.example", "port": 5432,
                 "name": "target", "username": "app", "password": "target-secret",
                 "connect_timeout": 5, "ssl": {}}
    target_fingerprint = migration._fingerprint(candidate)
    source_fingerprint = migration._fingerprint(SOURCE_SETTINGS)
    job, created = jobs.create(
        "database_migration",
        {"candidate": migration._safe_settings(candidate),
         "fingerprint": target_fingerprint,
         "source_fingerprint": source_fingerprint},
        idempotency_key="reject-populated-remote-target")
    assert created
    jobs.update(job["job_id"], status="QUIESCING", stage="QUIESCING")
    target_identity = {"instance": "target-before-copy"}
    current = {"settings": dict(SOURCE_SETTINGS)}
    monkeypatch.setattr(migration, "_current_settings", lambda: dict(current["settings"]))
    monkeypatch.setattr(migration, "_snapshot_source", lambda _settings, job_id: _snapshot_fixture(
        source_path, os.path.join(config.directory, ".maintenance", "snapshots", job_id + ".sqlite3")))
    monkeypatch.setattr(migration, "_new_engine", lambda *_args, **_kwargs: target_engine)
    monkeypatch.setattr(migration, "_engine_target_identity",
                        lambda *_args: target_identity)
    monkeypatch.setattr(migration, "_save_database_config",
                        lambda candidate, _fingerprint: current.update(settings=candidate))
    scheduled = []
    monkeypatch.setattr(migration, "schedule_restart", lambda: scheduled.append(True))

    migration._run_migration_impl(job["job_id"], candidate, source_fingerprint,
                                  target_fingerprint, target_identity, job["job_id"])

    result = jobs.get(job["job_id"])
    assert result["status"] == "FAILED", result
    assert "目标数据库检查后出现新对象" in result["error"]
    assert current["settings"] == SOURCE_SETTINGS
    assert scheduled == []
    assert jobs.maintenance_gate("database") is None
    with target_engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM external_data")).scalar_one() == "keep me"
        assert "DATABASE_MIGRATION_MARKERS" not in migration.inspect(connection).get_table_names()
    target_engine.dispose()


def test_migration_spool_pending_aborts_before_snapshot_and_resumes_recognition(
        tmp_path, monkeypatch):
    _config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    job_id, candidate, source_fingerprint, target_fingerprint = _prepare_migration_job(jobs)
    calls = []
    monkeypatch.setattr("app.media.recognition.records.quiesce_recognition",
                        lambda timeout: calls.append(("quiesce", timeout)))
    monkeypatch.setattr("app.media.recognition.records.replay_recognition_spool",
                        lambda: {"pending": 1})
    monkeypatch.setattr("app.media.recognition.records.resume_recognition",
                        lambda: calls.append(("resume", None)))
    monkeypatch.setattr(migration, "_snapshot_source",
                        lambda *_args: pytest.fail("pending spool must stop before snapshot"))

    migration._run_migration(job_id, candidate, source_fingerprint,
                             target_fingerprint, {"instance": "target"}, job_id)

    job = jobs.get(job_id)
    assert job["status"] == "FAILED"
    assert "暂存记录未能全部写入" in job["error"]
    assert calls == [("quiesce", 120), ("resume", None)]


def _legacy_sqlite(path, table, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(f'CREATE TABLE "{table}" (value TEXT NOT NULL)')
        connection.execute(f'INSERT INTO "{table}" VALUES (?)', (value,))
        connection.commit()
    finally:
        connection.close()


def _prepare_target_job(jobs, config):
    candidate = {"type": "sqlite"}
    files = migration._sqlite_target_files(config.directory)
    job, created = jobs.create(
        "database_target_prepare",
        {"check_id": "f" * 32, "candidate": candidate,
         "fingerprint": migration._fingerprint(candidate),
         "source_fingerprint": migration._fingerprint(SOURCE_SETTINGS),
         "target_files": files,
         "target_file_identities": {
             path: migration._sqlite_file_set_identity(path) for path in files}},
        idempotency_key="prepare-sqlite-target")
    assert created
    return job["job_id"], candidate, files


def test_sqlite_source_cannot_enter_remote_to_sqlite_file_retirement_flow(
        tmp_path, monkeypatch):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    legacy_path = os.path.join(config.directory, "user.db")
    _legacy_sqlite(legacy_path, "users", "must-remain")
    candidate = {"type": "sqlite"}
    files = migration._sqlite_target_files(config.directory)
    check, created = jobs.create(
        "database_migration",
        {"fingerprint": migration._fingerprint(candidate),
         "source_fingerprint": migration._fingerprint(SOURCE_SETTINGS)},
        idempotency_key="sqlite-source-refuses-retirement")
    assert created
    jobs.update(check["job_id"], status="READY", stage="READY",
                result={"target_preparation_required": True,
                        "target_files": files,
                        "target_file_identities": {
                            path: migration._sqlite_file_set_identity(path) for path in files}})
    monkeypatch.setattr(migration, "_normalize_candidate", lambda _values: candidate)
    monkeypatch.setattr(migration, "_current_settings", lambda: {"type": "sqlite"})

    result = migration.prepare_sqlite_migration_target({"check_id": check["job_id"]})

    assert result["code"] == 1
    assert "拒绝删除活动数据库文件" in result["msg"]
    assert os.path.isfile(legacy_path)
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute("SELECT value FROM users").fetchone() == ("must-remain",)


def test_changed_legacy_file_identity_after_check_is_never_retired(tmp_path, monkeypatch):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    legacy_path = os.path.join(config.directory, "user.db")
    _legacy_sqlite(legacy_path, "users", "checked-source")
    candidate = {"type": "sqlite"}
    files = migration._sqlite_target_files(config.directory)
    check, created = jobs.create(
        "database_migration",
        {"fingerprint": migration._fingerprint(candidate),
         "source_fingerprint": migration._fingerprint(SOURCE_SETTINGS)},
        idempotency_key="changed-source-file-check")
    assert created
    jobs.update(check["job_id"], status="READY", stage="READY",
                result={"target_preparation_required": True,
                        "target_files": files,
                        "target_file_identities": {
                            path: migration._sqlite_file_set_identity(path) for path in files}})
    monkeypatch.setattr(migration, "_normalize_candidate", lambda _values: candidate)

    prepared = migration.prepare_sqlite_migration_target({"check_id": check["job_id"]})
    assert prepared["code"] == 0, prepared
    job_id = prepared["job_id"]
    replacement = legacy_path + ".replacement"
    _legacy_sqlite(replacement, "users", "external-replacement")
    os.replace(replacement, legacy_path)

    migration._run_target_preparation(job_id, candidate)

    job = jobs.get(job_id)
    assert job["status"] == "FAILED", job
    assert "身份发生变化" in job["error"]
    assert os.path.isfile(legacy_path)
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute("SELECT value FROM users").fetchone() == (
            "external-replacement",)
    assert not os.path.exists(os.path.join(
        config.directory, ".maintenance", "database-retired", job_id))


def test_remote_to_sqlite_target_preparation_archives_legacy_files_before_empty_check(
        tmp_path, monkeypatch):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    user_path = os.path.join(config.directory, "user.db")
    media_path = os.path.join(config.directory, "media.db")
    _legacy_sqlite(user_path, "users", "preserve-user")
    _legacy_sqlite(media_path, "media", "preserve-media")
    job_id, candidate, original_files = _prepare_target_job(jobs, config)
    monkeypatch.setattr(migration, "_database_check",
                        lambda *_args, **_kwargs: {
                            "ok": True,
                            "target_identity": {"path": "prepared-empty-target"},
                            "target": {"type": "sqlite"}})

    migration._run_target_preparation(job_id, candidate)

    job = jobs.get(job_id)
    assert job["status"] == "SUCCEEDED", job
    assert all(not os.path.exists(path) for path in original_files)
    archive = job["result"]["archive"]
    assert os.path.isdir(archive)
    manifest = {os.path.basename(item["source"]): item for item in job["result"]["files"]}
    assert set(manifest) == {"user.db", "media.db"}
    for filename, value in (("user.db", "preserve-user"),
                            ("media.db", "preserve-media")):
        with sqlite3.connect(manifest[filename]["backup"]) as connection:
            table = "users" if filename == "user.db" else "media"
            assert connection.execute(f'SELECT value FROM "{table}"').fetchone()[0] == value
        assert migration._sha256_file(manifest[filename]["backup"]) == manifest[filename]["backup_sha256"]

    target_path = os.path.join(config.directory, "db", "nastools.db")
    assert os.path.isfile(target_path)
    assert migration._sqlite_objects(target_path) == []
    prepared_check = jobs.get(job["result"]["new_check_id"])
    assert prepared_check["status"] == "READY"
    assert prepared_check["payload"]["preparation_job_id"] == job_id


def test_target_preparation_archives_fixed_and_legacy_business_files_only(
        tmp_path, monkeypatch):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    fixed_path = os.path.join(config.directory, "db", "nastools.db")
    user_path = os.path.join(config.directory, "user.db")
    media_path = os.path.join(config.directory, "media.db")
    _legacy_sqlite(fixed_path, "fixed_data", "preserve-fixed")
    _legacy_sqlite(user_path, "users", "preserve-user")
    _legacy_sqlite(media_path, "media", "preserve-media")
    cache_path = os.path.join(config.directory, "cache", "system.sqlite3")
    _legacy_sqlite(cache_path, "cache_data", "preserve-cache")
    unrelated_path = os.path.join(config.directory, "keep.txt")
    with open(unrelated_path, "w", encoding="utf-8") as stream:
        stream.write("preserve-unrelated")

    job_id, candidate, business_files = _prepare_target_job(jobs, config)
    monkeypatch.setattr(migration, "_database_check",
                        lambda *_args, **_kwargs: {
                            "ok": True,
                            "target_identity": {"path": "prepared-empty-target"},
                            "target": {"type": "sqlite"}})

    migration._run_target_preparation(job_id, candidate)

    job = jobs.get(job_id)
    assert job["status"] == "SUCCEEDED", job
    assert set(business_files) == {fixed_path, user_path, media_path}
    assert not os.path.exists(user_path)
    assert not os.path.exists(media_path)
    assert os.path.isfile(fixed_path)
    assert len(job["result"]["files"]) == 3
    manifest = {os.path.basename(item["source"]): item
                for item in job["result"]["files"]}
    for filename, table, value in (
            ("nastools.db", "fixed_data", "preserve-fixed"),
            ("user.db", "users", "preserve-user"),
            ("media.db", "media", "preserve-media")):
        with sqlite3.connect(manifest[filename]["backup"]) as connection:
            assert connection.execute(
                f'SELECT value FROM "{table}"').fetchone() == (value,)
    with sqlite3.connect(cache_path) as connection:
        assert connection.execute("SELECT value FROM cache_data").fetchone() == (
            "preserve-cache",)
    with open(unrelated_path, encoding="utf-8") as stream:
        assert stream.read() == "preserve-unrelated"
    target_path = os.path.join(config.directory, "db", "nastools.db")
    assert os.path.isfile(target_path)
    assert migration._sqlite_objects(target_path) == []


def test_target_preparation_recovers_after_partial_old_file_removal(
        tmp_path, monkeypatch):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    user_path = os.path.join(config.directory, "user.db")
    media_path = os.path.join(config.directory, "media.db")
    _legacy_sqlite(user_path, "users", "recover-user")
    _legacy_sqlite(media_path, "media", "recover-media")
    job_id, candidate, original_files = _prepare_target_job(jobs, config)
    monkeypatch.setattr(migration, "_database_check",
                        lambda *_args, **_kwargs: {
                            "ok": True,
                            "target_identity": {"path": "prepared-empty-target"},
                            "target": {"type": "sqlite"}})
    original_unlink = migration.os.unlink

    def fail_removing_second_legacy_database(path, *args, **kwargs):
        if path == media_path:
            raise OSError("injected partial SQLite retirement")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(migration.os, "unlink", fail_removing_second_legacy_database)
    migration._run_target_preparation(job_id, candidate)
    interrupted = jobs.get(job_id)
    assert interrupted["status"] == "RECOVERY_REQUIRED", interrupted
    assert interrupted["result"]["resume_stage"] == "RETIRING_SQLITE"
    assert jobs.maintenance_gate("database") is None
    assert not os.path.exists(user_path)
    assert os.path.isfile(media_path)
    monkeypatch.setattr(migration.os, "unlink", original_unlink)

    migration._recover_target_preparation(jobs.get(job_id))

    recovered = jobs.get(job_id)
    assert recovered["status"] == "SUCCEEDED", recovered
    assert all(not os.path.exists(path) for path in original_files)
    target_path = os.path.join(config.directory, "db", "nastools.db")
    assert os.path.isfile(target_path)
    assert migration._sqlite_objects(target_path) == []
    assert os.path.isdir(recovered["result"]["archive"])


def test_target_preparation_failure_before_retirement_preserves_old_files(
        tmp_path, monkeypatch):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    user_path = os.path.join(config.directory, "user.db")
    media_path = os.path.join(config.directory, "media.db")
    _legacy_sqlite(user_path, "users", "preserve-user")
    _legacy_sqlite(media_path, "media", "preserve-media")
    job_id, candidate, original_files = _prepare_target_job(jobs, config)
    monkeypatch.setattr(migration, "_snapshot_sqlite_file",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            OSError("injected legacy snapshot failure")))

    migration._run_target_preparation(job_id, candidate)

    job = jobs.get(job_id)
    assert job["status"] == "FAILED", job
    assert "injected legacy snapshot failure" in job["error"]
    assert all(os.path.isfile(path) for path in original_files)
    assert not os.path.exists(os.path.join(config.directory, "db", "nastools.db"))


def test_target_preparation_recovery_preserves_unexpected_new_target_objects(
        tmp_path, monkeypatch):
    config, jobs = _install_isolated_state(tmp_path, monkeypatch)
    user_path = os.path.join(config.directory, "user.db")
    media_path = os.path.join(config.directory, "media.db")
    _legacy_sqlite(user_path, "users", "archive-user")
    _legacy_sqlite(media_path, "media", "archive-media")
    job_id, candidate, original_files = _prepare_target_job(jobs, config)
    target_path = os.path.join(config.directory, "db", "nastools.db")

    def introduce_external_object(settings, target_prepared=False):
        assert target_prepared
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with sqlite3.connect(target_path) as connection:
            connection.execute("CREATE TABLE external_writer_data (value TEXT)")
        return {"ok": False, "reason": "target changed after preparation"}

    monkeypatch.setattr(migration, "_database_check", introduce_external_object)
    migration._run_target_preparation(job_id, candidate)
    interrupted = jobs.get(job_id)
    assert interrupted["status"] == "RECOVERY_REQUIRED", interrupted
    assert interrupted["result"]["resume_stage"] == "EMPTY_TARGET_READY"
    assert all(not os.path.exists(path) for path in original_files)
    assert os.path.isdir(interrupted["result"]["archive"])

    with pytest.raises(RuntimeError, match="固定 SQLite 目标已出现业务对象|固定 SQLite 目标不是空库"):
        migration._recover_target_preparation(jobs.get(job_id))

    with sqlite3.connect(target_path) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "external_writer_data" in tables
    assert migration._current_settings()["type"] == "postgresql"
