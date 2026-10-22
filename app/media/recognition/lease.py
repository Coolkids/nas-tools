"""Renewable fenced cache leases with cooperative cancellation of actual I/O."""

from threading import Event, Thread, RLock
import sys
import time
import uuid
import weakref

from app.media.recognition.budget import CancellationReason


_paused_stores, _state_lock = weakref.WeakSet(), RLock()


def pause_refreshes(store):
    with _state_lock:
        _paused_stores.add(store)
    module = sys.modules.get("app.media.recognition.refresh_worker")
    if module is not None:
        module.cancel_refreshes(store)


def resume_refreshes(store):
    with _state_lock:
        _paused_stores.discard(store)


def refresh_paused(store):
    with _state_lock:
        return store in _paused_stores


class CacheLease:
    def __init__(self, store, namespace, key, budget, *, allowed=lambda: True, seconds=30):
        self.store, self.namespace, self.key, self.budget = store, namespace, key, budget
        self.allowed, self.seconds = allowed, seconds
        self.owner, self.fence = uuid.uuid4().hex, None
        self.stopped, self.lost = Event(), False
        self.thread = None

    def __enter__(self):
        self.budget.check()
        if not self.allowed():
            return self
        self.fence = self.store.lease_acquire(self.namespace, self.key, self.owner, self.seconds)
        if self.fence:
            self.thread = Thread(target=self._watch, daemon=True, name="recognition-refresh-lease")
            self.thread.start()
        return self

    def _watch(self):
        renew_at = time.monotonic()+self.seconds/3
        while not self.stopped.wait(.05):
            if self.budget.cancellation.reason is not None:
                return
            try:
                if not self.allowed():
                    raise RuntimeError("refresh_disabled")
                if time.monotonic() >= renew_at:
                    if not self.store.lease_renew(self.namespace, self.key, self.owner, self.fence, self.seconds):
                        raise RuntimeError("lease_lost")
                    renew_at = time.monotonic()+self.seconds/3
            except Exception:
                self.lost = True
                self.budget.cancellation.cancel(CancellationReason.USER_CANCELLED)
                return

    def check(self):
        self.budget.check()
        if self.lost or not self.fence or not self.allowed():
            raise RuntimeError("refresh_lease_unavailable")

    def __exit__(self, *_args):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(1)
        if self.fence:
            try:
                self.store.lease_release(self.namespace, self.key, self.owner, self.fence)
            except Exception:
                pass  # Expires; a stale owner can never publish through fencing.
