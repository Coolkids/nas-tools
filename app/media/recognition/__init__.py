"""多媒体名称识别扩展契约与识别记录。"""

from app.media.recognition.contracts import (
    MediaNameRecognizer,
    ParseResult,
    RecognitionRequest,
    RecognizerDescriptor,
)
from app.media.recognition.registry import registry

__all__ = [
    "MediaNameRecognizer", "ParseResult", "RecognitionRequest",
    "RecognizerDescriptor", "registry",
]
