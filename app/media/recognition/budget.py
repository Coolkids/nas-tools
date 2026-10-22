"""使用单调时钟管理请求和阶段预算。限制为 null 时表示确实不设上限。

令牌采用协作式取消，用于停止准入和提交。阻塞工作进程的终止由受监管的执行层负责，不属于此时钟抽象的职责。
"""

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from threading import Lock
import time

from app.media.recognition.contracts import BudgetSnapshot


class CancellationReason(StrEnum):
    USER_CANCELLED = "user_cancelled"
    SERVICE_SHUTDOWN = "service_shutdown"
    DEADLINE_EXCEEDED = "recognition_deadline_exceeded"


class RecognitionCancelled(RuntimeError):
    def __init__(self, reason):
        self.reason = CancellationReason(reason)
        super().__init__(self.reason.value)


class BudgetExceeded(TimeoutError):
    def __init__(self, scope):
        self.scope = scope
        super().__init__("recognition_deadline_exceeded" if scope == "total"
                         else f"{scope}_deadline_exceeded")


class CancellationToken:
    """首次取消原因优先；子令牌可以观察父令牌，但不拥有父令牌。"""

    def __init__(self, parent=None):
        self._parent = parent
        self._reason = None
        self._lock = Lock()

    @property
    def reason(self):
        with self._lock:
            reason = self._reason
        return reason or (self._parent.reason if self._parent is not None else None)

    def cancel(self, reason=CancellationReason.USER_CANCELLED):
        reason = CancellationReason(reason)
        with self._lock:
            if self._reason is None:
                self._reason = (self._parent.reason if self._parent is not None else None) or reason
            return self._reason

    def child(self):
        return CancellationToken(self)

    def check(self):
        if self.reason is not None:
            raise RecognitionCancelled(self.reason)


def _timeout(value):
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("timeout must be a positive integer in milliseconds or null")
    return value


def total_timeout_ms(settings):
    """优先读取 V3，同时保留旧版秒数的精确值以及显式 null。"""
    timeouts = settings.get("timeouts") or {}
    if "total_ms" in timeouts:
        return _timeout(timeouts["total_ms"])
    seconds = (settings.get("execution") or {}).get("total_timeout_seconds", 30)
    if seconds is None:
        return None
    try:
        millis = Decimal(str(seconds)) * 1000
        if isinstance(seconds, bool) or not millis.is_finite() or millis <= 0 or millis != millis.to_integral_value():
            raise ValueError
        return int(millis)
    except (ValueError, InvalidOperation):
        raise ValueError("invalid legacy total_timeout_seconds") from None


class RecognitionBudget:
    def __init__(self, total_ms=None, *, clock_ns=None, started_ns=None, cancellation=None):
        total_ms = _timeout(total_ms)
        self.clock_ns = clock_ns or time.monotonic_ns
        self.started_ns = self.clock_ns() if started_ns is None else started_ns
        self.total_deadline_ns = None if total_ms is None else self.started_ns + total_ms * 1_000_000
        self.cancellation = cancellation if cancellation is not None else CancellationToken()

    @property
    def failure_reason(self):
        reason = self.cancellation.reason
        if reason is not None:
            return reason.value
        if self.total_deadline_ns is not None and self.clock_ns() >= self.total_deadline_ns:
            return CancellationReason.DEADLINE_EXCEEDED.value
        return None

    def remaining_seconds(self):
        if self.cancellation.reason is not None:
            return 0.0
        deadline = self.total_deadline_ns
        return None if deadline is None else max(0, deadline - self.clock_ns()) / 1_000_000_000

    def check(self):
        self.cancellation.check()
        if self.total_deadline_ns is not None and self.clock_ns() >= self.total_deadline_ns:
            raise BudgetExceeded("total")

    def stage(self, name, timeout_ms=None):
        return StageBudget(self, name, timeout_ms)

    def snapshot(self):
        remaining = self.remaining_seconds()
        return BudgetSnapshot(self.total_deadline_ns, None,
                              None if remaining is None else remaining * 1000,
                              self.failure_reason)


class StageBudget:
    def __init__(self, parent, name, timeout_ms=None):
        timeout_ms = _timeout(timeout_ms)
        self.parent = parent
        self.root = parent.root if isinstance(parent, StageBudget) else parent
        self.name = name
        self.started_ns = self.root.clock_ns()
        self.stage_deadline_ns = (None if timeout_ms is None
                                  else self.started_ns + timeout_ms * 1_000_000)

    @property
    def deadline_ns(self):
        outer = self.parent.deadline_ns if isinstance(self.parent, StageBudget) else self.root.total_deadline_ns
        deadlines = [value for value in (outer, self.stage_deadline_ns) if value is not None]
        return min(deadlines) if deadlines else None

    def remaining_seconds(self):
        if self.root.cancellation.reason is not None:
            return 0.0
        deadline = self.deadline_ns
        return None if deadline is None else max(0, deadline - self.root.clock_ns()) / 1_000_000_000

    def check(self):
        self.parent.check()
        if self.stage_deadline_ns is not None and self.root.clock_ns() >= self.stage_deadline_ns:
            raise BudgetExceeded(self.name)

    def stage(self, name, timeout_ms=None):
        return StageBudget(self, name, timeout_ms)

    def snapshot(self):
        remaining = self.remaining_seconds()
        return BudgetSnapshot(self.root.total_deadline_ns, self.stage_deadline_ns,
                              None if remaining is None else remaining * 1000,
                              self.root.failure_reason)


def effective_deadline_reason(budget):
    if isinstance(budget, StageBudget):
        if budget.stage_deadline_ns is not None and budget.stage_deadline_ns == budget.deadline_ns:
            return budget.name+"_deadline_exceeded"
        return effective_deadline_reason(budget.parent)
    return CancellationReason.DEADLINE_EXCEEDED.value
