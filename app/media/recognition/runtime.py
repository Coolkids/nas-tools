"""使用有界且可复用的 spawn 工作进程。只有确认退出后才释放繁忙槽位。"""

import atexit
from contextlib import contextmanager
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
import json
import multiprocessing
import os
import selectors
import sys
from threading import Condition, RLock, Thread
import time
import uuid

from app.media.recognition.budget import CancellationReason, CancellationToken


_IN_WORKER = False


def _connection_ready(connection):
    if os.name != "posix":
        return connection.poll(0)
    # 单次内核轮询不会依赖 Python 时钟重试。DefaultSelector 还
    # 支持繁忙 Linux 服务器上超过 select() FD_SETSIZE 的文件描述符。
    with selectors.DefaultSelector() as selector:
        selector.register(connection, selectors.EVENT_READ)
        return bool(selector.select(0))


@dataclass(frozen=True, slots=True)
class WorkItem:
    kind: str
    payload: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in {"ping", "preprocess", "parse", "http"}:
            raise ValueError("unsupported recognition work kind")
        # 函数、会话、ORM 实体和锁都不得跨越此边界。
        if len(json.dumps(self.payload, ensure_ascii=False, allow_nan=False).encode()) > 1024 * 1024:
            raise ValueError("work input exceeds 1 MiB")


@dataclass(frozen=True, slots=True)
class WorkResult:
    task_id: str
    status: str
    value: object = None
    reason: str | None = None
    pid: int | None = None
    dispatched_at: float | None = None
    cancel_requested_at: float | None = None
    resource_released_at: float | None = None


class ResourceBusy(RuntimeError):
    pass


def in_worker():
    return _IN_WORKER


def _http_work(payload, session, cancelled):
    from urllib.parse import urlsplit
    parts = urlsplit(payload["url"])
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("invalid HTTP URL")
    if os.environ.get("NASTOOL_TEST_RUN_DIR") and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("test workers only allow local HTTP")
    method = payload.get("method", "GET").upper()
    if method not in {"GET", "POST", "HEAD"}:
        raise ValueError("unsupported HTTP method")
    kwargs = {key: payload[key] for key in ("headers", "params", "data", "json", "proxies", "allow_redirects") if key in payload}
    session.cookies.clear()
    try:
        with session.request(method, payload["url"], timeout=payload.get("timeout"),
                             verify=payload.get("verify", True), stream=True, **kwargs) as response:
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                if cancelled.is_set():
                    raise InterruptedError("cancelled")
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise ValueError("HTTP response exceeds 8 MiB")
                chunks.append(chunk)
            return {"status": response.status_code, "headers": dict(response.headers),
                    "content": b"".join(chunks), "encoding": response.encoding, "url": response.url}
    finally:
        session.cookies.clear()


def _execute_work(item, http, cancelled):
    if item.kind == "ping":
        return {"pid": os.getpid()}
    if item.kind == "http":
        return _http_work(item.payload, http, cancelled)
    if item.kind == "preprocess":
        from app.media.recognition.preprocessor import preprocess_with_details, RuleSnapshot, WordRule
        payload = item.payload
        rules = RuleSnapshot(tuple(tuple(WordRule(**rule) for rule in group) for group in payload.get("rules", ((),)*6)))
        return preprocess_with_details(payload["title"], payload.get("secondary_title"), rules=rules,
                                       normalize=payload.get("normalize", True))
    from app.media.recognition.registry import registry
    from app.media.recognition.contracts import ParseResult, RecognitionRequest
    payload = item.payload
    provider = registry.create(payload["provider_id"], **payload.get("options", {}))
    if provider is None:
        return ParseResult(payload["provider_id"], "error", error="provider_unavailable")
    try:
        if os.environ.get("NASTOOL_TEST_RUN_DIR") and provider.descriptor.network_required:
            from urllib.parse import urlsplit
            endpoint = getattr(provider, "endpoint", None) or getattr(provider, "base_url", None)
            if endpoint and urlsplit(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("test workers only allow local provider endpoints")
        context = {**payload.get("context", {}), "apply_custom_words": False}
        if context.get("mtype"):
            from app.utils.types import MediaType
            context["mtype"] = MediaType(context["mtype"])
        result = provider.parse(RecognitionRequest(payload["title"], payload.get("subtitle"), context))
        if not isinstance(result, ParseResult):
            raise ValueError("invalid provider result")
        meta = result.parsed
        if hasattr(meta, "get_name"):
            result.parsed = {"name": meta.get_name(), "media_type": getattr(meta.type, "name", "").lower(),
                "year": meta.year, "season": meta.begin_season, "episode": meta.begin_episode,
                "end_episode": meta.end_episode, "alternative_names": getattr(meta, "alternative_names", []),
                **{key: getattr(meta, key, None) for key in ("part", "resource_type", "resource_pix",
                    "resource_effect", "resource_team", "video_encode", "audio_encode")}}
        return result
    finally:
        registry.release(provider)


def _worker(connection, cancelled, parent_pid):
    global _IN_WORKER
    _IN_WORKER = True
    # 父进程退出时，也必须回收持有 GIL 的无限等待 HTTP 和正则调用。
    # Linux/ARM 使用独立于 Python 线程的内核死亡信号。
    if sys.platform.startswith("linux"):
        import ctypes
        import signal
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot supervise worker parent")
        if os.getppid() != parent_pid:
            os._exit(1)
    else:  # pragma: no cover - 非 Linux 打包环境
        def watch_parent():
            while os.getppid() == parent_pid:
                time.sleep(.1)
            os._exit(1)
        Thread(target=watch_parent, daemon=True).start()
    # 工作进程不得记录请求正文、标题或凭据。诊断信息
    # 只向协调器返回稳定代码，由协调器决定保留策略。
    import logging
    logging.disable(logging.CRITICAL)
    import requests
    http = requests.Session()
    try:
        from app.media.recognition.preprocessor import preprocess
        preprocess("")
        connection.send(("ready", os.getpid()))
        while True:
            message = connection.recv()
            if message is None:
                return
            task_id, item = message
            try:
                if cancelled.is_set():
                    raise InterruptedError("cancelled")
                value = _execute_work(item, http, cancelled)
                if cancelled.is_set():
                    raise InterruptedError("cancelled")
                result = ("result", task_id, value, None)
            except Exception as error:
                result = ("result", task_id, None, type(error).__name__)
            finally:
                manager = sys.modules.get("app.db.manager")
                if manager is not None:
                    manager.DATABASE.close_session()
            connection.send(result)
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        http.close()
        connection.close()


class WorkFuture(Future):
    """cancel() 会请求终止；任务完成需等待资源实际退出。"""

    def __init__(self, token):
        super().__init__()
        self.token = token
        self.cancel_requested_at = None

    def cancel(self, reason=CancellationReason.USER_CANCELLED):
        if self.done():
            return False
        self.cancel_requested_at = self.cancel_requested_at or time.monotonic()
        self.token.cancel(reason)
        return True


@dataclass
class _Pending:
    item: WorkItem
    future: WorkFuture
    task_id: str
    deadline: float | None
    deadline_reason: str = CancellationReason.DEADLINE_EXCEEDED.value
    queue_deadline: float | None = None
    dispatched: float | None = None


@dataclass
class _Slot:
    process: object
    connection: object
    cancelled: object
    ready: bool = False
    task: _Pending | None = None
    abort_reason: str | None = None
    abort_at: float | None = None
    created_at: float = field(default_factory=time.monotonic)


class WorkerPool:
    def __init__(self, workers=1, max_queue=100, *, cancellation_grace=0.1, name="parser"):
        if type(workers) is not int or not 1 <= workers <= 32 or type(max_queue) is not int or not 0 <= max_queue <= 10000:
            raise ValueError("invalid worker quota")
        self.workers, self.max_queue, self.name = workers, max_queue, name
        self.cancellation_grace = max(0, cancellation_grace)
        self._context = multiprocessing.get_context("spawn")
        self._condition = Condition(RLock())
        self._pending, self._slots = deque(), []
        self._closing, self._thread = False, None
        self._spawn_count, self._released_count = 0, 0
        self._warm_failures = 0

    def _spawn(self):
        parent, child = self._context.Pipe()
        cancelled = self._context.Event()
        process = self._context.Process(target=_worker, args=(child, cancelled, os.getpid()), daemon=True,
                                        name="recognition-"+self.name)
        process.start()
        child.close()
        self._spawn_count += 1
        return _Slot(process, parent, cancelled)

    def _start(self):
        if self._thread is None:
            try:
                for _ in range(self.workers):
                    self._slots.append(self._spawn())
            except BaseException:
                self._closing = True
                for slot in self._slots:
                    self._retire(slot)
                self._slots.clear()
                raise
            self._thread = Thread(target=self._supervise, name="recognition-supervisor-"+self.name, daemon=True)
            self._thread.start()

    def prewarm(self, timeout=30):
        with self._condition:
            if self._closing:
                raise RuntimeError("worker pool is closed")
            self._start()
            deadline = time.monotonic() + timeout
            while not all(slot.ready for slot in self._slots):
                if self._closing:
                    raise RuntimeError("worker initialization failed")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("worker warmup timeout")
                self._condition.wait(min(left, 0.1))
        return self.stats()

    def submit(self, item, *, deadline=None, cancellation=None, deadline_reason=CancellationReason.DEADLINE_EXCEEDED.value,
               queue_deadline=None):
        if in_worker():
            raise RuntimeError("workers cannot recursively submit recognition work")
        if not isinstance(item, WorkItem):
            raise TypeError("WorkItem required")
        with self._condition:
            if self._closing:
                raise RuntimeError("recognition runtime is shutting down")
            token = cancellation.child() if cancellation is not None else CancellationToken()
            future = WorkFuture(token)
            pending = _Pending(item, future, uuid.uuid4().hex, deadline, deadline_reason, queue_deadline)
            reason = self._abort(pending)
            if reason:
                self._complete(pending, reason=reason)
                return future
            self._start()
            running = sum(slot.task is not None for slot in self._slots)
            if len(self._pending) + running >= self.workers + self.max_queue:
                raise ResourceBusy("recognition resource queue is full")
            # 子进程各自拥有任务，因此取消 singleflight 等待者不会
            # 取消另一请求的所有者令牌。
            self._pending.append(pending)
            self._condition.notify_all()
            return future

    @staticmethod
    def _abort(task):
        if task.future.token.reason is not None:
            return task.future.token.reason.value
        if task.deadline is not None and time.monotonic() >= task.deadline:
            return task.deadline_reason
        if task.dispatched is None and task.queue_deadline is not None and time.monotonic() >= task.queue_deadline:
            return "queue_deadline_exceeded"
        return None

    def _complete(self, task, *, value=None, reason=None, pid=None):
        if task.future.done():
            return
        status = "success" if reason is None else "timeout" if str(reason).endswith("deadline_exceeded") else "cancelled" if reason in {
            CancellationReason.USER_CANCELLED, CancellationReason.SERVICE_SHUTDOWN} else "error"
        task.future.set_result(WorkResult(task.task_id, status, value if reason is None else None, reason, pid,
            task.dispatched, task.future.cancel_requested_at, time.monotonic()))

    def _retire(self, slot):
        if slot.process.is_alive():
            slot.process.terminate()
            slot.process.join(0.1)
        if slot.process.is_alive():
            slot.process.kill()
            slot.process.join(1)
        if slot.process.is_alive():
            return False
        slot.process.join()
        pid = slot.process.pid
        slot.connection.close()
        slot.process.close()
        self._released_count += 1
        if slot.task is not None:
            self._complete(slot.task, reason=slot.abort_reason or "worker_crashed", pid=pid)
        return True

    def _supervise(self):
        while True:
            with self._condition:
                for task in list(self._pending):
                    reason = self._abort(task)
                    if self._closing:
                        reason = CancellationReason.SERVICE_SHUTDOWN.value
                    if reason:
                        self._pending.remove(task)
                        self._complete(task, reason=reason)
                for slot in list(self._slots):
                    task = slot.task
                    reason = self._abort(task) if task is not None else None
                    if self._closing:
                        reason = CancellationReason.SERVICE_SHUTDOWN.value
                    elif task is None and not slot.ready and time.monotonic()-slot.created_at > 30:
                        reason = "worker_warmup_timeout"
                    if reason and slot.abort_reason is None:
                        slot.abort_reason, slot.abort_at = reason, time.monotonic()
                        if task is not None:
                            task.future.cancel_requested_at = task.future.cancel_requested_at or slot.abort_at
                        slot.cancelled.set()
                    dead = not slot.process.is_alive()
                    if dead or slot.abort_reason and time.monotonic()-slot.abort_at >= self.cancellation_grace:
                        failed_warmup = slot.task is None and not slot.ready and not self._closing
                        if self._retire(slot):
                            self._slots.remove(slot)
                            if failed_warmup:
                                self._warm_failures += 1
                            if self._warm_failures >= 3:
                                self._closing = True
                                for pending in list(self._pending):
                                    self._complete(pending, reason="worker_initialization_failed")
                                self._pending.clear()
                            if not self._closing:
                                try:
                                    self._slots.append(self._spawn())
                                except (OSError, RuntimeError):
                                    self._closing = True
                                    for pending in list(self._pending):
                                        self._complete(pending, reason="worker_start_failed")
                                    self._pending.clear()
                        continue
                    try:
                        # POSIX 的 Connection.poll(0) 内部会循环读取
                        # 进程全局单调时钟。模拟时钟测试可能在
                        # 两次迭代之间将时钟调回，使这个非阻塞轮询
                        # 变成无界等待，
                        # 并在持有池锁时阻止取消操作。
                        if _connection_ready(slot.connection):
                            message = slot.connection.recv()
                            if message[0] == "ready":
                                slot.ready = True
                                self._warm_failures = 0
                            elif task is not None and message[0] == "result" and message[1] == task.task_id:
                                # 忽略取消后才返回的任何结果。
                                reason = slot.abort_reason or self._abort(task) or message[3]
                                self._complete(task, value=message[2], reason=reason, pid=slot.process.pid)
                                slot.task = None
                                slot.ready = not self._closing
                                slot.abort_reason, slot.abort_at = None, None
                    except (EOFError, OSError):
                        slot.abort_reason, slot.abort_at = "worker_crashed", time.monotonic()-self.cancellation_grace
                    if slot.ready and slot.task is None and self._pending and not self._closing:
                        task = self._pending.popleft()
                        reason = self._abort(task)
                        if reason:
                            self._complete(task, reason=reason)
                            continue
                        slot.cancelled.clear()
                        slot.task, slot.ready = task, False
                        task.dispatched = time.monotonic()
                        try:
                            slot.connection.send((task.task_id, task.item))
                        except (BrokenPipeError, EOFError, OSError):
                            slot.abort_reason, slot.abort_at = "worker_crashed", time.monotonic()-self.cancellation_grace
                self._condition.notify_all()
                if self._closing and not self._slots:
                    return
                self._condition.wait(0.01 if self._pending or any(slot.task for slot in self._slots) else 0.05)

    def stats(self):
        with self._condition:
            return {"name": self.name, "workers": len(self._slots), "running": sum(slot.task is not None for slot in self._slots),
                    "queued": len(self._pending), "spawn_count": self._spawn_count, "released_count": self._released_count,
                    "pids": [slot.process.pid for slot in self._slots]}

    def shutdown(self, wait=True, timeout=10):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        if wait and self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise TimeoutError("worker resources have not been released")

    def __enter__(self):
        try:
            self.prewarm()
        except BaseException:
            self.shutdown()
            raise
        return self

    def __exit__(self, *_args):
        self.shutdown()


class RecognitionRuntime:
    def __init__(self, *, parser_workers=2, tmdb_workers=2, refresh_workers=1, max_queue=100, foreground_limit=4):
        self.owner_pid = os.getpid()
        self.pools = {name: WorkerPool(workers, max_queue, name=name) for name, workers in (
            ("parser", parser_workers), ("tmdb", tmdb_workers), ("refresh", refresh_workers))}
        self.foreground_limit = foreground_limit
        self._foreground_condition = Condition()
        self._foreground_active, self._foreground_waiting = 0, 0
        self._foreground_tokens = set()
        self._background_tokens = set()
        self.max_queue, self._closing = max_queue, False

    @contextmanager
    def foreground(self, *, deadline=None, cancellation=None):
        token = cancellation if cancellation is not None else CancellationToken()
        acquired = False
        with self._foreground_condition:
            if self._closing or (self._foreground_active >= self.foreground_limit and self._foreground_waiting >= self.max_queue):
                raise ResourceBusy("foreground recognition queue is full")
            self._foreground_tokens.add(token)
            self._foreground_waiting += 1
        try:
            with self._foreground_condition:
                while True:
                    token.check()
                    if deadline is not None and time.monotonic() >= deadline:
                        from app.media.recognition.budget import BudgetExceeded
                        raise BudgetExceeded("total")
                    if self._closing:
                        token.cancel(CancellationReason.SERVICE_SHUTDOWN)
                        token.check()
                    if self._foreground_active < self.foreground_limit:
                        self._foreground_active += 1
                        self._foreground_waiting -= 1
                        acquired = True
                        break
                    self._foreground_condition.wait(0.05)
            yield token
        finally:
            with self._foreground_condition:
                if acquired:
                    self._foreground_active -= 1
                else:
                    self._foreground_waiting -= 1
                self._foreground_tokens.discard(token)
                self._foreground_condition.notify_all()

    def submit(self, resource, item, **options):
        if resource not in self.pools:
            raise ValueError("unknown recognition resource")
        return self.pools[resource].submit(item, **options)

    def refresh_available(self):
        with self._foreground_condition:
            return not self._closing and not self._foreground_waiting

    @contextmanager
    def background(self, cancellation):
        with self._foreground_condition:
            if self._closing or self._foreground_waiting:
                raise ResourceBusy("foreground has priority")
            self._background_tokens.add(cancellation)
        try:
            cancellation.check()
            yield
        finally:
            with self._foreground_condition:
                self._background_tokens.discard(cancellation)

    def shutdown(self):
        if self.owner_pid != os.getpid():
            return
        with self._foreground_condition:
            self._closing = True
            for token in self._foreground_tokens | self._background_tokens:
                token.cancel(CancellationReason.SERVICE_SHUTDOWN)
            self._foreground_condition.notify_all()
        for pool in self.pools.values():
            pool.shutdown(wait=False)
        for pool in self.pools.values():
            pool.shutdown()

    def cancel_active(self, reason=CancellationReason.SERVICE_SHUTDOWN):
        with self._foreground_condition:
            for token in self._foreground_tokens | self._background_tokens:
                token.cancel(reason)
            self._foreground_condition.notify_all()
        for pool in self.pools.values():
            with pool._condition:
                for task in list(pool._pending)+[slot.task for slot in pool._slots if slot.task is not None]:
                    task.future.cancel(reason)
                pool._condition.notify_all()


_runtime, _runtime_lock = None, RLock()


def _after_fork():
    global _runtime, _runtime_lock
    if _runtime is not None:
        for pool in _runtime.pools.values():
            for slot in pool._slots:
                slot.connection.close()
    _runtime, _runtime_lock = None, RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def get_runtime():
    global _runtime
    if in_worker():
        raise RuntimeError("nested runtime execution is forbidden")
    with _runtime_lock:
        if _runtime is None:
            from config import Config
            settings = Config().get_config("recognition") or {}
            execution, query = settings.get("execution") or {}, settings.get("query") or {}
            _runtime = RecognitionRuntime(parser_workers=execution.get("max_inflight_provider_requests", 4),
                tmdb_workers=query.get("tmdb_concurrency", 4),
                refresh_workers=(settings.get("cache_v3") or {}).get("refresh_concurrency", 1),
                max_queue=execution.get("max_queued_tasks", 100), foreground_limit=execution.get("max_running_tasks", 4))
            atexit.register(_runtime.shutdown)
        return _runtime


def shutdown_runtime():
    global _runtime
    with _runtime_lock:
        old, _runtime = _runtime, None
    if old is not None:
        old.shutdown()


def cancel_runtime():
    with _runtime_lock:
        current = _runtime
    if current is not None:
        current.cancel_active()


def supervised_enabled():
    if in_worker():
        return False
    from app.media.recognition.records import current_recorder
    return current_recorder() is not None


def execute_http(payload, *, resource="tmdb"):
    """重建 requests.Response；绝不传递正在使用的 HTTP 会话。"""
    from app.media.recognition.records import current_recorder, recognition_remaining_seconds, recognition_timeout_reason
    from requests import Response
    from requests.exceptions import Timeout, RequestException
    recorder = current_recorder()
    remaining = recognition_remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise Timeout(recognition_timeout_reason())
    payload = dict(payload)
    if payload.get("timeout") is not None:
        payload["timeout"] = min(payload["timeout"], remaining) if remaining is not None else payload["timeout"]
    else:
        payload["timeout"] = remaining
    deadline = time.monotonic()+remaining if remaining is not None else None
    result = get_runtime().submit(resource, WorkItem("http", payload), deadline=deadline,
        cancellation=recorder.cancellation if recorder is not None else None).result()
    if recorder is not None:
        recorder.action("resource_released", status=result.status, output={
            "task_id": result.task_id, "pid": result.pid,
            "cancel_requested_at": result.cancel_requested_at,
            "resource_released_at": result.resource_released_at,
            "remote_cancellation": "unknown" if result.status != "success" else None})
    if result.status != "success":
        if result.status == "timeout":
            raise Timeout(result.reason)
        raise RequestException(result.reason)
    response = Response()
    response.status_code, response.headers, response._content = result.value["status"], result.value["headers"], result.value["content"]
    response.encoding, response.url = result.value["encoding"], result.value["url"]
    return response
