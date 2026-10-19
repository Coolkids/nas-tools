"""持久化 JobStore 的重启、间隔更新、调度器隔离和 RSS 配置重载。"""

import datetime
import inspect
import pickle
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
from apscheduler.schedulers.base import STATE_PAUSED
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import inspect as inspect_database, select, text

import app.db.scheduler as scheduler_module
import app.rsschecker as rss_module
from app.db.backends.sqlite import SQLiteBackend
from app.db.manager import DatabaseManager
from app.db.models import SCHEDULER_JOB_TABLES
from app.db.scheduler import PersistentBackgroundScheduler, run_persistent_job
from app.utils.storage_jobs import StorageJobStore
from db_scripts.versions.fa0d1e2f3a4b_persistent_scheduler_jobs import upgrade as upgrade_scheduler_jobs


UTC = datetime.timezone.utc


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(SQLiteBackend, "path", staticmethod(lambda: str(tmp_path / "scheduler.db")))
    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_read_settings", lambda: {"type": "sqlite"})
    monkeypatch.setattr(scheduler_module, "DATABASE", manager)
    for table in SCHEDULER_JOB_TABLES.values():
        table.create(manager.engine)
    try:
        yield manager
    finally:
        manager.dispose()


def scheduler(namespace="rss"):
    instance = PersistentBackgroundScheduler(namespace=namespace, timezone="UTC")
    instance.start(paused=True)
    return instance


@pytest.mark.parametrize("old_minutes,new_minutes", [(10, 30), (30, 5)])
def test_restart_and_interval_change_keep_original_next_run(database, old_minutes, new_minutes):
    next_run = datetime.datetime.now(UTC) + datetime.timedelta(hours=2, microseconds=123456)
    first = scheduler()
    first.add_interval_job("builtins:len", id="rss_task_7", args=([1, 2],),
                           minutes=old_minutes, next_run_time=next_run)
    first.shutdown()

    restored = scheduler()
    try:
        original = restored.get_job("rss_task_7")
        assert original.next_run_time == next_run
        assert isinstance(original.trigger, IntervalTrigger)
        assert original.trigger.interval == datetime.timedelta(minutes=old_minutes)
        changed = restored.add_interval_job("builtins:len", id="rss_task_7", args=([1, 2],),
                                            minutes=new_minutes, next_run_time=datetime.datetime.now(UTC))
        assert changed.next_run_time == next_run
        assert changed.trigger.interval == datetime.timedelta(minutes=new_minutes)
        assert changed.trigger.get_next_fire_time(next_run, next_run) == (
            next_run + datetime.timedelta(minutes=new_minutes))
        table = SCHEDULER_JOB_TABLES["rss"]
        with database.engine.connect() as connection:
            saved = connection.execute(select(table.c.job_state)).scalar_one()
        state = pickle.loads(saved)
        assert state["func"] == "app.db.scheduler:run_persistent_job"
        assert state["args"] == ("builtins:len", ([1, 2],), {})
        assert state["next_run_time"] == next_run
    finally:
        restored.shutdown()

    again = scheduler()
    try:
        assert again.get_job("rss_task_7").next_run_time == next_run
        assert again.get_job("rss_task_7").trigger.interval == datetime.timedelta(minutes=new_minutes)
    finally:
        again.shutdown()


@pytest.mark.parametrize("paused", [False, True])
def test_interval_change_preserves_overdue_or_paused_next_run(database, paused):
    next_run = None if paused else datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
    instance = scheduler()
    try:
        instance.add_interval_job("builtins:len", id="existing", args=([],),
                                  hours=1, next_run_time=next_run)
        changed = instance.add_interval_job("builtins:len", id="existing", args=([],), hours=12)
        assert changed.next_run_time == next_run
    finally:
        instance.shutdown()


def test_old_next_run_executes_once_then_uses_new_interval(database):
    instance = scheduler()
    next_run = datetime.datetime.now(UTC) + datetime.timedelta(milliseconds=250)
    instance.add_interval_job("builtins:len", id="execute", args=([1, 2, 3],),
                              hours=1, next_run_time=next_run)
    instance.add_interval_job("builtins:len", id="execute", args=([1, 2, 3],), hours=2)
    finished = Event()
    events = []

    def completed(event):
        events.append(event)
        if {event.code for event in events} == {EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED}:
            finished.set()

    instance.add_listener(completed, EVENT_JOB_EXECUTED | EVENT_JOB_SUBMITTED)
    try:
        instance.resume()
        assert finished.wait(10), "任务未按原下一次时间执行"
        executed = next(event for event in events if event.code == EVENT_JOB_EXECUTED)
        assert executed.retval == 3
        assert executed.scheduled_run_time == next_run
        assert instance.get_job("execute").next_run_time == next_run + datetime.timedelta(hours=2)
    finally:
        instance.shutdown()


def test_scheduler_namespaces_and_shared_engine_survive_independent_shutdown(database):
    main, rss = scheduler("main"), scheduler("rss")
    main.add_interval_job("builtins:len", id="same-id", args=([1],), hours=1)
    rss.add_interval_job("builtins:len", id="same-id", args=([1, 2],), hours=2)
    pool = database.engine.pool
    main.shutdown()
    try:
        assert database.engine.pool is pool
        assert database.session.execute(text("SELECT 1")).scalar_one() == 1
        database.close_session()
        assert len(rss.get_jobs()) == 1
        assert rss.get_job("same-id").trigger.interval == datetime.timedelta(hours=2)
        database.dispose()
        current_engine = database.engine
        assert rss._lookup_jobstore("default").engine is current_engine
        assert rss.get_job("same-id").trigger.interval == datetime.timedelta(hours=2)
    finally:
        rss.shutdown()


def test_reconciliation_removes_disabled_jobs_only_in_own_store(database):
    first, other = scheduler(), scheduler("main")
    for job_id in ("keep", "disabled", "deleted"):
        first.add_interval_job("builtins:len", id=job_id, args=([],), hours=1)
    other.add_interval_job("builtins:len", id="disabled", args=([],), hours=1)
    first.shutdown()
    current = scheduler()
    try:
        current.add_interval_job("builtins:len", id="keep", args=([],), hours=2)
        current.reconcile_jobs()
        assert [job.id for job in current.get_jobs()] == ["keep"]
        assert other.get_job("disabled") is not None
    finally:
        current.shutdown()
        other.shutdown()


def query_in_job(fail=False):
    assert scheduler_module.DATABASE.session.execute(text("SELECT 1")).scalar_one() == 1
    if fail:
        raise RuntimeError("expected job error")
    return 1


@pytest.mark.parametrize("fail", [False, True])
def test_persisted_dispatch_releases_database_sessions_after_success_or_failure(database, fail):
    if fail:
        with pytest.raises(RuntimeError, match="expected job error"):
            run_persistent_job(f"{__name__}:query_in_job", kwargs={"fail": True})
    else:
        assert run_persistent_job(f"{__name__}:query_in_job") == 1
    assert database.engine.pool.checkedout() == 0


def test_rss_reload_retains_next_run_and_removes_deleted_tasks(database, monkeypatch):
    values = dict(ID=7, NAME="rss", ADDRESS="", PARSER=None, FILTER=None, NOTE=None, SITES=None,
                  INTERVAL=10, USES="R", INCLUDE=None, EXCLUDE=None, UPDATE_TIME=None, PROCESS_COUNT=0,
                  STATE="Y", SAVE_PATH=None, DOWNLOAD_SETTING=None, RECOGNIZATION=None,
                  OVER_EDITION=None, FILTER_ARGS=None)
    task = SimpleNamespace(**values)
    tasks = [task]
    helper = SimpleNamespace(get_userrss_parser=lambda: [], get_userrss_tasks=lambda: tasks)
    monkeypatch.setattr(rss_module, "DbHelper", lambda: helper)
    for name in ("Message", "Searcher", "Filter", "Media", "Downloader", "Subscribe"):
        monkeypatch.setattr(rss_module, name, Mock())
    monkeypatch.setattr(rss_module, "Config", lambda: SimpleNamespace(
        get_config=lambda node: {}, get_timezone=lambda: "UTC"))
    rss_type = inspect.getclosurevars(rss_module.RssChecker).nonlocals["cls"]
    checker = rss_type()
    next_run = datetime.datetime.now(UTC) + datetime.timedelta(hours=1)
    try:
        checker._scheduler.modify_job("rss_task_7", next_run_time=next_run)
        task.INTERVAL = 20
        checker.init_config()
        restored = checker._scheduler.get_job("rss_task_7")
        assert restored.next_run_time == next_run
        assert restored.trigger.interval == datetime.timedelta(minutes=20)
        tasks.clear()
        checker.init_config()
        assert checker._scheduler is None
        table = SCHEDULER_JOB_TABLES["rss"]
        with database.engine.connect() as connection:
            assert connection.execute(select(table.c.id)).all() == []
    finally:
        if checker._scheduler:
            checker._scheduler.shutdown()


def test_scheduler_schema_upgrade_creates_all_jobstores_without_modifying_business_data(database):
    for table in SCHEDULER_JOB_TABLES.values():
        table.drop(database.engine)
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE BUSINESS_TEST (ID INTEGER PRIMARY KEY, VALUE TEXT)"))
        connection.execute(text("INSERT INTO BUSINESS_TEST VALUES (1, 'keep')"))
        with Operations.context(MigrationContext.configure(connection)):
            upgrade_scheduler_jobs()
            upgrade_scheduler_jobs()
        assert connection.execute(text("SELECT VALUE FROM BUSINESS_TEST")).scalar_one() == "keep"
    actual = set(inspect_database(database.engine).get_table_names())
    assert {table.name for table in SCHEDULER_JOB_TABLES.values()} <= actual


def test_scheduler_migration_does_not_create_main_jobstores_in_legacy_media_snapshot(database):
    for table in SCHEDULER_JOB_TABLES.values():
        table.drop(database.engine)
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE MEDIASYNC_ITEMS (ID INTEGER PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(connection)):
            upgrade_scheduler_jobs()
    assert inspect_database(database.engine).get_table_names() == ["MEDIASYNC_ITEMS"]


def test_scheduler_retries_during_database_maintenance_without_changing_schedule(database, tmp_path, monkeypatch):
    jobs = StorageJobStore(str(tmp_path / ".maintenance" / "jobs.sqlite3"))
    monkeypatch.setattr(scheduler_module, "STORAGE_JOBS", jobs)
    instance = scheduler()
    next_run = datetime.datetime.now(UTC) + datetime.timedelta(hours=1)
    instance.add_interval_job("builtins:len", id="protected", args=([],), hours=1, next_run_time=next_run)
    try:
        with jobs.maintenance_lock(exclusive=True):
            jobs.set_maintenance_gate("database", "test-maintenance", "MIGRATING")
            try:
                instance.state = scheduler_module.STATE_RUNNING
                assert instance._process_jobs() == instance.jobstore_retry_interval
                with pytest.raises(RuntimeError, match="数据库正在维护"):
                    instance.add_interval_job("builtins:len", id="protected", args=([],), hours=2)
            finally:
                instance.state = STATE_PAUSED
                jobs.clear_maintenance_gate("database", "test-maintenance")
        assert instance.get_job("protected").next_run_time == next_run
        assert instance.get_job("protected").trigger.interval == datetime.timedelta(hours=1)
    finally:
        instance.shutdown()
