from app.media.recognition.contracts import MediaNameRecognizer, ParseResult, RecognitionRequest


class InvalidDescriptor(MediaNameRecognizer):
    descriptor = {"provider_id": "invalid_case"}

    def parse(self, request: RecognitionRequest) -> ParseResult:
        return ParseResult("invalid_case", "success")
