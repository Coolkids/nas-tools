"""数据库概览定时精确统计的配置、调度和结果复用验证。"""

import datetime
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.db.maintenance as maintenance
import app.db.manager as manager_module
import app.db.migration as migration
import app.scheduler as scheduler_module
from app.db.backends.sqlite import SQLiteBackend
from app.db.manager import DatabaseManager
from app.db.models import Base, TRANSFERHISTORY
from app.db.statistics import database_statistics_schedule
from app.utils.storage_jobs import StorageJobStore
from web.action import WebAction


def configured_scheduler(monkeypatch, options=None, latest=None):
    config = SimpleNamespace(
        get_config=lambda node: {"database_statistics": options} if node == "scheduler" else {},
        get_timezone=lambda: "UTC")
    monkeypatch.setattr(scheduler_module, "Config", lambda: config)
    monkeypatch.setattr(scheduler_module, "STORAGE_JOBS", SimpleNamespace(latest=Mock(return_value=latest)))
    service = object.__new__(type(scheduler_module.Scheduler()))
    service.SCHEDULER = Mock()
    return service


def test_unconfigured_statistics_default_to_enabled_every_twelve_hours():
    assert database_statistics_schedule() == {
        "enabled": True, "interval_value": 12, "interval_unit": "hour", "interval_hours": 12}


@pytest.mark.parametrize("value,unit", [
    (11, "hour"), (0, "day"), (-1, "hour"), (12.5, "hour"), (True, "hour"),
    (None, "hour"), ("", "hour"), (0.5, "day"), (12, "month"),
])
def test_invalid_statistics_intervals_are_rejected_without_saving(monkeypatch, value, unit):
    save = Mock()
    live = {"database": {"type": "sqlite"}}
    monkeypatch.setattr("web.action.Config", lambda: SimpleNamespace(get_config=lambda: live, save_config=save))
    restart = Mock()
    monkeypatch.setattr("web.action.restart_scheduler", restart)

    result = object.__new__(WebAction)._WebAction__update_config({
        "scheduler.database_statistics.interval_value": value,
        "scheduler.database_statistics.interval_unit": unit,
    })

    assert result["code"] == 1
    assert "数据库精确统计间隔" in result["msg"]
    save.assert_not_called()
    restart.assert_not_called()


@pytest.mark.parametrize("enabled,value,unit,hours", [(True, "12", "hour", 12), (False, 2, "day", 48)])
def test_statistics_settings_save_and_reload_without_changing_database_connection(
        monkeypatch, enabled, value, unit, hours):
    live = {"database": {"type": "postgresql", "host": "test.invalid", "port": 5432},
            "recognition": {"decision": {"weights": {"title_match": 1}}}}
    original = deepcopy(live)
    save = Mock()
    monkeypatch.setattr("web.action.Config", lambda: SimpleNamespace(get_config=lambda: live, save_config=save))
    restart = Mock()
    monkeypatch.setattr("web.action.restart_scheduler", restart)

    result = object.__new__(WebAction)._WebAction__update_config({
        "scheduler.database_statistics.enabled": enabled,
        "scheduler.database_statistics.interval_value": value,
        "scheduler.database_statistics.interval_unit": unit,
    })

    assert result["code"] == 0, result
    saved = save.call_args.args[0]
    settings = saved["scheduler"]["database_statistics"]
    assert settings == {"enabled": enabled, "interval_value": int(value), "interval_unit": unit}
    assert database_statistics_schedule(settings)["interval_hours"] == hours
    assert saved["database"] == original["database"]
    assert live == original
    restart.assert_called_once_with()


def test_statistics_job_is_registered_on_service_start_with_default_interval(monkeypatch):
    service = configured_scheduler(monkeypatch)
    engine = service.SCHEDULER
    monkeypatch.setattr(scheduler_module, "PersistentBackgroundScheduler", lambda **kwargs: engine)
    before = datetime.datetime.now(datetime.timezone.utc)
    service.run_service()

    job = next(call for call in engine.add_interval_job.call_args_list
               if call.kwargs.get("id") == "database_statistics")
    assert job.args == ("app.scheduler:Scheduler.refresh_database_statistics",)
    assert job.kwargs["hours"] == 12
    assert before <= job.kwargs["next_run_time"] <= datetime.datetime.now(datetime.timezone.utc)
    engine.start.assert_called_once_with(paused=True)
    engine.resume.assert_called_once_with()


@pytest.mark.parametrize("settings", [{"enabled": False}, {"interval_value": 11}])
def test_disabled_or_invalid_statistics_job_is_not_registered(monkeypatch, settings):
    service = configured_scheduler(monkeypatch, settings)

    service._schedule_database_statistics()

    service.SCHEDULER.add_interval_job.assert_not_called()


def test_statistics_reload_preserves_recent_refresh_time_and_converts_days(monkeypatch):
    created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
    service = configured_scheduler(monkeypatch, {"interval_value": 1, "interval_unit": "day"},
                                   {"created_at": created.timestamp(), "status": "SUCCEEDED"})

    service._schedule_database_statistics()

    job = service.SCHEDULER.add_interval_job.call_args
    assert job.kwargs["hours"] == 24
    assert job.kwargs["next_run_time"] == created + datetime.timedelta(days=1)


@pytest.mark.parametrize("result", [
    {"code": 0, "job_id": "already-counting"},
    {"code": 1, "msg": "数据库正在维护，暂不能刷新精确统计"},
])
def test_statistics_callback_uses_existing_background_task_and_maintenance_guards(monkeypatch, result):
    service = configured_scheduler(monkeypatch)
    start = Mock(return_value=result)
    monkeypatch.setattr(maintenance, "start_database_statistics_refresh", start)

    assert service.refresh_database_statistics() == result
    start.assert_called_once_with({"idempotency_key": "scheduled-database-statistics"})


@pytest.mark.parametrize("entrypoint", ["service", "database-overview"])
@pytest.mark.parametrize("options", [{"enabled": True}, {"enabled": False, "interval_value": 1}])
def test_manual_statistics_can_refresh_again_within_twelve_hours(tmp_path, monkeypatch, entrypoint, options):
    jobs = StorageJobStore(str(tmp_path / ".maintenance" / "jobs.sqlite3"))
    path = str(tmp_path / "nastools.db")
    monkeypatch.setattr(SQLiteBackend, "path", staticmethod(lambda: path))
    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_read_settings", lambda: {"type": "sqlite"})
    monkeypatch.setattr(manager_module, "STORAGE_JOBS", jobs)
    monkeypatch.setattr(maintenance, "DATABASE", manager)
    monkeypatch.setattr(migration, "DATABASE", manager)
    monkeypatch.setattr(maintenance, "STORAGE_JOBS", jobs)
    monkeypatch.setattr(maintenance, "_sqlite_path", lambda: path)
    service = configured_scheduler(monkeypatch, options)
    if entrypoint == "service":
        # 验证服务列表的“运行”实际进入立即执行回调，而非定时配置校验。
        monkeypatch.setattr("web.action.Scheduler", lambda: service)
        for name in ("TorrentRemover", "Downloader", "Sync", "DoubanSync", "Subscribe"):
            monkeypatch.setattr(f"web.action.{name}", Mock())
        dispatched = []
        monkeypatch.setattr("web.action.ThreadHelper", lambda: SimpleNamespace(
            start_thread=lambda function, args: dispatched.append(function(*args))))

        def refresh():
            WebAction._WebAction__sch({"item": "database_statistics"})
            return dispatched[-1]
    else:
        refresh = WebAction._WebAction__start_database_statistics_refresh
    try:
        Base.metadata.create_all(manager.engine)
        with manager.engine.begin() as connection:
            connection.execute(TRANSFERHISTORY.__table__.insert(), [{"ID": 1}, {"ID": 2}])

        result = refresh()
        assert result["code"] == 0
        maintenance._WORKERS[result["job_id"]].join(timeout=5)
        job = jobs.get(result["job_id"])
        assert job["status"] == "SUCCEEDED", job
        assert job["result"]["tables"]["TRANSFER_HISTORY"] == 2

        response = maintenance.get_database_info()
        assert response["code"] == 0
        overview = response["database"]
        assert overview["table_count_mode"] == "exact"
        assert overview["tables"]["TRANSFER_HISTORY"] == 2
        assert overview["exact_statistics_at"] == job["result"]["statistics_at"]
        assert manager.engine.pool.checkedout() == 0

        # 刚完成的统计不能成为手动刷新的冷却限制；新记录必须被立即计入。
        with manager.engine.begin() as connection:
            connection.execute(TRANSFERHISTORY.__table__.insert(), {"ID": 3})
        repeated = refresh()
        assert repeated["code"] == 0
        assert repeated["job_id"] != result["job_id"]
        maintenance._WORKERS[repeated["job_id"]].join(timeout=5)
        repeated_job = jobs.get(repeated["job_id"])
        assert repeated_job["status"] == "SUCCEEDED", repeated_job
        assert repeated_job["created_at"] - job["created_at"] < 12 * 60 * 60
        assert repeated_job["result"]["tables"]["TRANSFER_HISTORY"] == 3
        assert maintenance.get_database_info()["database"]["tables"]["TRANSFER_HISTORY"] == 3
    finally:
        manager.dispose()
