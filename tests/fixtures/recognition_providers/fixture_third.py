"""A third-party-style implementation that needs no production wiring."""

from app.media.recognition.contracts import (
    MediaNameRecognizer,
    ParseResult,
    RecognitionRequest,
    RecognizerDescriptor,
)


class FixtureThirdRecognizer(MediaNameRecognizer):
    descriptor = RecognizerDescriptor(
        provider_id="fixture_third",
        display_name="测试第三解析器",
        version="test-1",
        evidence_family="fixture",
        config_schema={"mode": {"type": "string", "default": "basic",
                                 "enum": ["basic", "strict"]}},
    )

    def __init__(self, mode="basic"):
        self.mode = mode

    def parse(self, request: RecognitionRequest) -> ParseResult:
        return ParseResult(
            provider_id=self.descriptor.provider_id,
            status="success",
            parsed={"title": request.title, "subtitle": request.subtitle},
            raw_result={"input": request.title},
        )

    def close(self) -> None:
        self.closed = True
