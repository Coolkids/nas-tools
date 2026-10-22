"""最终校验识别身份是否可提供给自动化业务调用方。"""

from dataclasses import replace

from app.media.recognition.budget import CancellationReason
from app.media.recognition.contracts import RecognitionStatus


def admit_outcome(outcome, budget, *, allow_partial_success=False, validated_at_ns=None):
    """只有明确完成的校验结果才能在总时限结束后保留。

    解析器结果或候选时间戳不构成校验证据。所有验证完成后，
    resolver 才会提供此内部时间戳。
    用户取消和关停始终优先于部分成功。
    """
    reason = budget.failure_reason
    if reason is None:
        return outcome
    deadline = budget.total_deadline_ns
    if (reason == CancellationReason.DEADLINE_EXCEEDED and allow_partial_success is True
            and outcome.status == RecognitionStatus.MATCHED
            and type(validated_at_ns) is int and deadline is not None
            and budget.started_ns <= validated_at_ns < deadline):
        return replace(outcome, warnings=outcome.warnings + ("partial_success_on_deadline",))
    status = (RecognitionStatus.TIMEOUT if reason == CancellationReason.DEADLINE_EXCEEDED
              else RecognitionStatus.CANCELLED)
    return replace(outcome, status=status, result=None, failure_code=reason)


