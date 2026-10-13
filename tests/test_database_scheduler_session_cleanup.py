"""Scheduled jobs must release scoped DB sessions on reused worker threads."""

from datetime import datetime, timedelta
from threading import Barrier, Event, Lock

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.executors.pool import ThreadPoolExecutor
from sqlalchemy import text

import app.db.manager as database_manager_module
import app.db.scheduler as database_scheduler_module
from app.db.backends.sqlite import SQLiteBackend
from app.db.manager import DatabaseManager
from app.db.scheduler import DatabaseSessionBackgroundScheduler


def test_scheduled_jobs_release_connections_after_success_and_failure(
        tmp_path, monkeypatch):
    database_path = tmp_path / "db" / "nastools.db"
    monkeypatch.setattr(SQLiteBackend, "path", staticmethod(lambda: str(database_path)))
    manager = DatabaseManager()
    monkeypatch.setattr(manager, "_read_settings", lambda: {"type": "sqlite"})
    monkeypatch.setattr(database_manager_module, "DATABASE", manager)
    monkeypatch.setattr(database_scheduler_module, "DATABASE", manager)

    scheduler = DatabaseSessionBackgroundScheduler(
        executors={"default": ThreadPoolExecutor(2)})
    both_jobs_started = Barrier(2)
    finished = Event()
    observed = []
    observed_lock = Lock()

    def job_succeeded():
        assert manager.session.execute(text("SELECT 1")).scalar_one() == 1
        both_jobs_started.wait(timeout=5)

    def job_failed():
        assert manager.session.execute(text("SELECT 1")).scalar_one() == 1
        both_jobs_started.wait(timeout=5)
        raise RuntimeError("expected scheduled-job failure")

    def record_completion(event):
        with observed_lock:
            observed.append(event)
            if len(observed) == 2:
                finished.set()

    try:
        manager.engine
        scheduler.add_listener(record_completion, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        run_at = datetime.now() + timedelta(seconds=0.1)
        scheduler.add_job(job_succeeded, "date", run_date=run_at, id="success")
        scheduler.add_job(job_failed, "date", run_date=run_at, id="failure")
        scheduler.start()

        assert finished.wait(timeout=10), "scheduled jobs did not finish"
        assert {event.job_id for event in observed} == {"success", "failure"}
        assert manager.engine.pool.checkedout() == 0
        assert manager.session.execute(text("SELECT 1")).scalar_one() == 1
        manager.close_session()
        assert manager.engine.pool.checkedout() == 0
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=True)
        manager.dispose()
