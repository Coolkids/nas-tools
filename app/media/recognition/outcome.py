"""Final admission of recognition identities to automated business callers."""

from dataclasses import replace

from app.media.recognition.budget import CancellationReason
from app.media.recognition.contracts import RecognitionStatus


def admit_outcome(outcome, budget, *, allow_partial_success=False, validated_at_ns=None):
    """Only an explicitly completed validation can survive the total deadline.

    A parser result or candidate timestamp is not validation evidence. The
    resolver supplies this internal timestamp after all verification finishes.
    User cancellation and shutdown always take precedence over partial success.
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


