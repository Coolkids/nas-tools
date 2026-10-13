"""Scheduled jobs must release scoped DB sessions on reused worker threads."""

from datetime import datetime, timedelta
from concurrent.futures import Future
from threading import Barrier, Event, Lock

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.executors.pool import ThreadPoolExecutor
from sqlalchemy import text

import app.db.manager as database_manager_module
import app.db.scheduler as database_scheduler_module
from app.db.backends.sqlite import SQLiteBackend
from app.db.manager import DatabaseManager
from app.db.scheduler import DatabaseSessionBackgroundScheduler
import app.subscribe as subscribe_module
from app.subscribe import Subscribe


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


def test_subscribe_search_releases_coordinator_and_worker_sessions_before_fanout(monkeypatch):
    closed_sessions = []

    class InlineExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def submit(self, function, *args):
            assert closed_sessions, "coordinator DB session must be released before worker fan-out"
            future = Future()
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)
            return future

    monkeypatch.setattr(subscribe_module, "close_db", lambda: closed_sessions.append(True))
    monkeypatch.setattr(subscribe_module, "ThreadPoolExecutor", InlineExecutor)

    for getter_name, search_name in (
            ("get_subscribe_movies", "subscribe_search_movie"),
            ("get_subscribe_tvs", "subscribe_search_tv")):
        closed_sessions.clear()
        subscribe = Subscribe.__new__(Subscribe)
        monkeypatch.setattr(subscribe, getter_name, lambda **kwargs: {
            "fixture": {"fuzzy_match": True}
        })

        getattr(subscribe, search_name)(state="R")

        assert len(closed_sessions) == 2
