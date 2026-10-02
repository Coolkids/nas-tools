"""Extensible media-name recognition contracts and audit records."""

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

