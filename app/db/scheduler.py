"""APScheduler integration that releases thread-scoped database sessions."""

from functools import wraps

from apscheduler.schedulers.background import BackgroundScheduler

from app.db.manager import DATABASE


def _release_database_session(func):
    @wraps(func)
    def run(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        finally:
            # APScheduler reuses worker threads. A scoped_session therefore
            # survives between jobs unless it is explicitly removed here.
            DATABASE.close_session()

    return run


class DatabaseSessionBackgroundScheduler(BackgroundScheduler):
    """Background scheduler that returns each job thread's DB connection."""

    def add_job(self, func, *args, **kwargs):
        if callable(func):
            func = _release_database_session(func)
        return super().add_job(func, *args, **kwargs)
