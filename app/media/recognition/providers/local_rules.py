"""Adapter for the existing MetaVideo and MetaAnime title rules."""

import time

from app.media.recognition.contracts import (
    MediaNameRecognizer,
    ParseResult,
    RecognitionRequest,
    RecognizerDescriptor,
)


class LocalRulesRecognizer(MediaNameRecognizer):
    descriptor = RecognizerDescriptor(
        provider_id="local_rules",
        display_name="本地规则",
        evidence_family="local_rules",
    )

    def parse(self, request: RecognitionRequest) -> ParseResult:
        from app.media.meta.metainfo import _parse_meta_info

        started = time.monotonic()
        try:
            parsed = _parse_meta_info(request.title, subtitle=request.subtitle,
                                      mtype=request.context.get("mtype"),
                                      apply_custom_words=request.context.get("apply_custom_words", True))
            return ParseResult(provider_id=self.descriptor.provider_id, status="success",
                               parsed=parsed, elapsed_ms=int((time.monotonic() - started) * 1000))
        except Exception as error:
            return ParseResult(provider_id=self.descriptor.provider_id, status="error",
                               error=str(error), elapsed_ms=int((time.monotonic() - started) * 1000))

