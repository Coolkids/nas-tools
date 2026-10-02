from app.media.recognition.contracts import MediaNameRecognizer, ParseResult, RecognitionRequest, RecognizerDescriptor


class DuplicateTwo(MediaNameRecognizer):
    descriptor = RecognizerDescriptor("duplicate_case", "Duplicate two", evidence_family="test")

    def parse(self, request: RecognitionRequest) -> ParseResult:
        return ParseResult("duplicate_case", "success")
