"""SQLAlchemy connection pool with aggregate acquisition wait metrics."""

import time

from sqlalchemy.pool import QueuePool


class MeasuredQueuePool(QueuePool):
    """QueuePool that reports total time spent acquiring a connection.

    SQLAlchemy exposes checkout events after acquisition, so measuring the
    queue wait requires timing QueuePool's acquisition hook. The callback is
    optional and only receives an elapsed duration; it never sees SQL or data.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wait_observer = None

    def set_wait_observer(self, observer):
        self._wait_observer = observer

    def _do_get(self):
        started_at = time.perf_counter()
        try:
            return super()._do_get()
        finally:
            observer = self._wait_observer
            if observer is not None:
                observer(max(0.0, time.perf_counter() - started_at))
