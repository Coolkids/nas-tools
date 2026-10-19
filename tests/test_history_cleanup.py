"""从定时任务入口到历史表删除的回归验证。"""

import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.executors.pool import ThreadPoolExecutor
from sqlalchemy import inspect

import app.db.main_db as main_db_module
import app.db.scheduler as database_scheduler_module
import app.scheduler as scheduler_module
from app.db.backends.sqlite import SQLiteBackend
from app.db.manager import DatabaseManager
from app.db.models import Base, DOWNLOADHISTORY, RSSHISTORY, TRANSFERHISTORY
from app.db.scheduler import DatabaseSessionBackgroundScheduler
from app.helper.db_helper import DbHelper


CASES = [
    (TRANSFERHISTORY, "DATE", "transfer_history", "delete_expired_transfer_history",
     "cleanup_expired_transfer_history"),
    (RSSHISTORY, "FINISH_TIME", "subscription_history", "delete_expired_subscription_history",
     "cleanup_expired_subscription_history"),
    (DOWNLOADHISTORY, "DATE", "download_history", "delete_expired_download_history",
     "cleanup_expired_download_history"),
]


def configure_history(monkeypatch, value=2, unit="month"):
    history = {"cleanup": {key: {
        "enabled": True, "interval_hours": 1,
        "retention_value": value, "retention_unit": unit,
    } for _, _, key, _, _ in CASES}}
    configured = SimpleNamespace(get_config=lambda node: history if node == "history" else {})
    monkeypatch.setattr(scheduler_module, "Config", lambda: configured)


@pytest.mark.parametrize("model,date_field,key,delete_method,callback", CASES)
def test_public_cleanup_entrypoint_uses_singleton_instance_and_current_settings(
        monkeypatch, model, date_field, key, delete_method, callback):
    configure_history(monkeypatch)
    delete = Mock(return_value=3)
    monkeypatch.setattr(scheduler_module, "DbHelper", lambda: SimpleNamespace(**{delete_method: delete}))

    assert getattr(scheduler_module.Scheduler(), callback)() == 3
    delete.assert_called_once_with(2, retention_unit="month")


@pytest.fixture
def history_database(tmp_path, monkeypatch):
    monkeypatch.setattr(SQLiteBackend, "path", staticmethod(lambda: str(tmp_path / "history.db")))
    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_read_settings", lambda: {"type": "sqlite"})
    monkeypatch.setattr(main_db_module, "DATABASE", manager)
    monkeypatch.setattr(database_scheduler_module, "DATABASE", manager)
    Base.metadata.create_all(manager.engine, tables=[case[0].__table__ for case in CASES])
    try:
        yield manager
    finally:
        manager.dispose()


@pytest.mark.parametrize("model,date_field,key,delete_method,callback", CASES)
@pytest.mark.parametrize("value,unit,cutoff", [
    (30, "day", datetime.datetime(2026, 3, 1, 12)),
    (1, "month", datetime.datetime(2026, 2, 28, 12)),
])
def test_history_tables_have_dates_and_expired_rows_are_committed_in_batches(
        history_database, monkeypatch, model, date_field, key, delete_method, callback, value, unit, cutoff):
    manager = history_database
    messages = []
    monkeypatch.setattr("app.helper.db_helper.log.info", messages.append)
    columns = {column["name"]: column for column in inspect(manager.engine).get_columns(model.__tablename__)}
    assert date_field in columns
    assert str(columns[date_field]["type"]) == "TEXT"
    dates = [cutoff - datetime.timedelta(days=2), cutoff - datetime.timedelta(seconds=1),
             cutoff - datetime.timedelta(days=1), cutoff + datetime.timedelta(seconds=1),
             None, datetime.datetime(2026, 3, 31, 12)]
    for record_id, date in enumerate(dates, start=1):
        manager.session.add(model(ID=record_id, **{
            date_field: date.strftime("%Y-%m-%d %H:%M:%S") if date else None}))
    for other_model, other_date, *_ in CASES:
        if other_model is not model:
            manager.session.add(other_model(ID=90, **{other_date: "2000-01-01 00:00:00"}))
    manager.session.commit()

    assert getattr(DbHelper(), delete_method)(
        value, retention_unit=unit, now=datetime.datetime(2026, 3, 31, 12), batch_size=2) == 3
    assert any("本批次删除 2 条，累计删除 2 条" in message for message in messages)
    assert any("本批次删除 1 条，累计删除 3 条" in message for message in messages)
    assert messages[-1].endswith("共删除 3 条过期记录")

    assert getattr(DbHelper(), delete_method)(
        value, retention_unit=unit, now=datetime.datetime(2026, 3, 31, 12), batch_size=2) == 0
    assert messages[-1].endswith("共删除 0 条过期记录")

    manager.close_session()
    assert [row.ID for row in manager.session.query(model).order_by(model.ID)] == [4, 5, 6]
    for other_model, *_ in CASES:
        if other_model is not model:
            assert manager.session.query(other_model).count() == 1


def test_all_three_scheduled_callbacks_delete_old_rows_and_release_sessions(history_database, monkeypatch):
    manager = history_database
    configure_history(monkeypatch, value=1, unit="day")
    for model, date_field, *_ in CASES:
        manager.session.add_all([
            model(ID=1, **{date_field: "2000-01-01 00:00:00"}),
            model(ID=2, **{date_field: "2099-01-01 00:00:00"}),
            model(ID=3, **{date_field: None}),
        ])
    manager.session.commit()
    manager.close_session()
    finished = Event()
    events = []
    scheduler = DatabaseSessionBackgroundScheduler(executors={"default": ThreadPoolExecutor(1)})

    def record_completion(event):
        events.append(event)
        if len(events) == 3:
            finished.set()

    scheduler.add_listener(record_completion, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    service = scheduler_module.Scheduler()
    for _, _, key, _, callback in CASES:
        scheduler.add_job(getattr(service, callback), "date", id=key)
    try:
        scheduler.start()
        assert finished.wait(timeout=10), "清理任务未完成"
        assert all(event.exception is None for event in events)
        assert [event.retval for event in events] == [1, 1, 1]
        assert manager.engine.pool.checkedout() == 0
        for model, *_ in CASES:
            assert [row.ID for row in manager.session.query(model).order_by(model.ID)] == [2, 3]
    finally:
        scheduler.shutdown(wait=True)


def test_database_failure_is_not_reported_as_completed_cleanup(monkeypatch):
    configure_history(monkeypatch)
    monkeypatch.setattr(scheduler_module, "DbHelper", lambda: SimpleNamespace(
        delete_expired_transfer_history=Mock(return_value=False)))
    with pytest.raises(RuntimeError, match="转移历史.*清理失败"):
        scheduler_module.Scheduler().cleanup_expired_transfer_history()
