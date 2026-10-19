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
from app.db.models import (AIRECOGNITIONRECORD, Base, DOWNLOADHISTORY, RECOGNITIONATTEMPT,
                           RECOGNITIONREQUEST, RECOGNITIONREQUESTACTION, RSSHISTORY, TRANSFERHISTORY)
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


def configure_history(monkeypatch, value=2, unit="month", max_records=None):
    history = {"cleanup": {key: {
        "enabled": True, "interval_hours": 1,
        "retention_value": value, "retention_unit": unit,
    } for _, _, key, _, _ in CASES}}
    if max_records is not None:
        for cleanup in history["cleanup"].values():
            cleanup["max_records"] = max_records
    configured = SimpleNamespace(get_config=lambda node: history if node == "history" else {})
    monkeypatch.setattr(scheduler_module, "Config", lambda: configured)


@pytest.mark.parametrize("model,date_field,key,delete_method,callback", CASES)
@pytest.mark.parametrize("max_records", [None, 7])
def test_public_cleanup_entrypoint_uses_singleton_instance_and_current_settings(
        monkeypatch, model, date_field, key, delete_method, callback, max_records):
    configure_history(monkeypatch, max_records=max_records)
    delete = Mock(return_value=3)
    monkeypatch.setattr(scheduler_module, "DbHelper", lambda: SimpleNamespace(**{delete_method: delete}))

    assert getattr(scheduler_module.Scheduler(), callback)() == 3
    expected_limit = max_records if max_records is not None else (200 if key == "transfer_history" else 100)
    delete.assert_called_once_with(2, retention_unit="month", max_records=expected_limit)


@pytest.fixture
def history_database(tmp_path, monkeypatch):
    monkeypatch.setattr(SQLiteBackend, "path", staticmethod(lambda: str(tmp_path / "history.db")))
    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_read_settings", lambda: {"type": "sqlite"})
    monkeypatch.setattr(main_db_module, "DATABASE", manager)
    monkeypatch.setattr(database_scheduler_module, "DATABASE", manager)
    tables = [case[0].__table__ for case in CASES] + [model.__table__ for model in (
        AIRECOGNITIONRECORD, RECOGNITIONREQUEST, RECOGNITIONATTEMPT, RECOGNITIONREQUESTACTION)]
    Base.metadata.create_all(manager.engine, tables=tables)
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
    assert "共删除 3 条记录（过期 3 条，超出上限 0 条）" in messages[-1]

    assert getattr(DbHelper(), delete_method)(
        value, retention_unit=unit, now=datetime.datetime(2026, 3, 31, 12), batch_size=2) == 0
    assert "共删除 0 条记录（过期 0 条，超出上限 0 条）" in messages[-1]

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


@pytest.mark.parametrize("model,date_field,key,delete_method,callback", CASES)
def test_cleanup_limit_keeps_newest_dates_and_deletes_null_dates_when_over_limit(
        history_database, monkeypatch, model, date_field, key, delete_method, callback):
    manager = history_database
    dates = ["2026-03-30 00:00:00", "2026-03-28 00:00:00", "2026-03-29 00:00:00",
             "2026-03-30 00:00:00", None, "2000-01-01 00:00:00", None]
    manager.session.add_all([model(ID=index, **{date_field: date})
                             for index, date in enumerate(dates, start=1)])
    manager.session.commit()
    messages = []
    monkeypatch.setattr("app.helper.db_helper.log.info", messages.append)
    cleanup = getattr(DbHelper(), delete_method)

    assert cleanup(30, now=datetime.datetime(2026, 3, 31), batch_size=1, max_records=2) == 5
    manager.close_session()
    assert [row.ID for row in manager.session.query(model).order_by(model.ID)] == [1, 4]
    assert "共删除 5 条记录（过期 1 条，超出上限 4 条）" in messages[-1]
    assert cleanup(30, now=datetime.datetime(2026, 3, 31), batch_size=1, max_records=2) == 0


@pytest.mark.parametrize("model,date_field,key,delete_method,callback", CASES)
def test_cleanup_default_limits(history_database, model, date_field, key, delete_method, callback):
    manager = history_database
    expected_limit = 200 if key == "transfer_history" else 100
    manager.session.add_all([model(ID=index, **{date_field: "2099-01-01 00:00:00"})
                             for index in range(1, expected_limit + 4)])
    manager.session.commit()

    assert getattr(DbHelper(), delete_method)(30, batch_size=2) == 3
    manager.close_session()
    assert [row.ID for row in manager.session.query(model).order_by(model.ID)] == list(
        range(4, expected_limit + 4))


@pytest.mark.parametrize("max_records,expected_requests,expected_legacy,total_deleted", [
    (4, {"running", "business-running", "new"}, {2}, 4),
    (1, {"running", "business-running"}, set(), 6),
])
def test_recognition_limit_is_shared_with_legacy_and_protects_running_requests(
        history_database, monkeypatch, max_records, expected_requests, expected_legacy, total_deleted):
    manager = history_database
    request_data = [
        ("expired", "2000-01-01 00:00:00", "success", None),
        ("running", "2000-01-01 00:00:00", "running", None),
        ("business-running", "2000-01-01 00:00:00", "success", "running"),
        ("null-date", None, None, None),
        ("mid", "2026-03-28 00:00:00", "failed", None),
        ("new", "2026-03-30 00:00:00", "success", None),
    ]
    for request_id, created, status, business_status in request_data:
        manager.session.add(RECOGNITIONREQUEST(REQUEST_ID=request_id, CREATED_AT=created,
                                               STATUS_SUMMARY=status, BUSINESS_STATUS_SUMMARY=business_status))
        manager.session.add(RECOGNITIONATTEMPT(REQUEST_ID=request_id, ATTEMPT_ID=request_id))
        manager.session.add(RECOGNITIONREQUESTACTION(
            ID=request_id, REQUEST_ID=request_id, ACTION_TYPE="recognize", ACTION_HASH="test-hash"))
    manager.session.add_all([
        AIRECOGNITIONRECORD(ID=1, ADD_TIME="2000-01-01 00:00:00"),
        AIRECOGNITIONRECORD(ID=2, ADD_TIME="2026-03-29 00:00:00"),
    ])
    manager.session.commit()
    messages = []
    monkeypatch.setattr("app.helper.db_helper.log.info", messages.append)

    assert DbHelper().delete_expired_recognition_records(
        30, now=datetime.datetime(2026, 3, 31), batch_size=1, max_records=max_records) == total_deleted
    manager.close_session()
    assert {row.REQUEST_ID for row in manager.session.query(RECOGNITIONREQUEST)} == expected_requests
    assert {row.REQUEST_ID for row in manager.session.query(RECOGNITIONATTEMPT)} == expected_requests
    assert {row.REQUEST_ID for row in manager.session.query(RECOGNITIONREQUESTACTION)} == expected_requests
    assert {row.ID for row in manager.session.query(AIRECOGNITIONRECORD)} == expected_legacy
    assert f"过期 2 条，超出上限 {total_deleted - 2} 条" in messages[-1]
    if max_records == 1:
        assert any("运行中的请求暂不清理" in message for message in messages)


def test_recognition_default_limit_counts_new_and_legacy_records_together(history_database):
    manager = history_database
    manager.session.add_all([RECOGNITIONREQUEST(
        REQUEST_ID=f"request-{index:03d}", CREATED_AT="2099-01-01 00:00:00")
        for index in range(201)])
    manager.session.add(AIRECOGNITIONRECORD(ID=1, ADD_TIME="2099-02-01 00:00:00"))
    manager.session.commit()

    assert DbHelper().delete_expired_recognition_records(30, batch_size=1) == 2
    manager.close_session()
    assert manager.session.query(RECOGNITIONREQUEST).count() == 199
    assert manager.session.query(AIRECOGNITIONRECORD).count() == 1
    assert manager.session.query(RECOGNITIONREQUEST).filter(
        RECOGNITIONREQUEST.REQUEST_ID.in_(["request-000", "request-001"])).count() == 0


@pytest.mark.parametrize("max_records", [None, 7])
def test_recognition_callback_passes_current_limit(monkeypatch, max_records):
    cleanup = {"retention_days": 45}
    if max_records is not None:
        cleanup["max_records"] = max_records
    config = SimpleNamespace(get_config=lambda node: {"records": {"cleanup": cleanup}}
                             if node == "recognition" else {})
    monkeypatch.setattr(scheduler_module, "Config", lambda: config)
    delete = Mock(return_value=3)
    monkeypatch.setattr(scheduler_module, "DbHelper", lambda: SimpleNamespace(
        delete_expired_recognition_records=delete))

    assert scheduler_module.Scheduler().cleanup_expired_recognition_records() == 3
    delete.assert_called_once_with(retention_unit="day", retention_value=45,
                                   max_records=max_records if max_records is not None else 200)
