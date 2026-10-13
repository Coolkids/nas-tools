"""Scheduled jobs must release scoped DB sessions on reused worker threads."""

from datetime import datetime, timedelta
from concurrent.futures import Future
from threading import Barrier, Event, Lock
from types import SimpleNamespace

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


def test_subscribe_search_failures_return_movie_and_tv_rows_to_ready_state(monkeypatch):
    class InlineExecutor:
        def __init__(self, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)
            return future

    updates = []
    errors = []
    subscribe = Subscribe.__new__(Subscribe)
    subscribe.dbhelper = SimpleNamespace(
        update_rss_movie_state=lambda rssid, state: updates.append(("movie", rssid, state)),
        update_rss_tv_state=lambda rssid, state: updates.append(("tv", rssid, state)))
    subscribe.get_subscribe_movies = lambda **kwargs: {
        "11": {"id": 11, "name": "failed movie", "fuzzy_match": False}}
    subscribe.get_subscribe_tvs = lambda **kwargs: {
        "21": {"id": 21, "name": "failed TV", "fuzzy_match": False}}

    def fail_media_lookup(*args, **kwargs):
        raise RuntimeError("fixture metadata failure")

    monkeypatch.setattr(subscribe_module, "ThreadPoolExecutor", InlineExecutor)
    monkeypatch.setattr(subscribe_module, "close_db", lambda: None)
    monkeypatch.setattr(subscribe_module.log, "error",
                        lambda message, module=None: errors.append(message))
    monkeypatch.setattr(Subscribe, "_Subscribe__get_media_info", fail_media_lookup)

    subscribe.subscribe_search_movie(state="R")
    subscribe.subscribe_search_tv(state="R")

    assert updates == [("movie", 11, "S"), ("movie", 11, "R"),
                       ("tv", 21, "S"), ("tv", 21, "R")]
    assert len(errors) == 2
