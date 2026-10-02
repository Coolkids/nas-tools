"""Shared contracts for media-name recognition providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RecognizerDescriptor:
    provider_id: str
    display_name: str
    version: str = "1"
    evidence_family: str = ""
    config_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecognitionRequest:
    title: str
    subtitle: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    provider_id: str
    status: str
    parsed: Any = None
    raw_result: Any = None
    error: str | None = None
    elapsed_ms: int = 0


class MediaNameRecognizer(ABC):
    """A parser accepts a title and returns one normalized parse result."""

    descriptor: RecognizerDescriptor

    @abstractmethod
    def parse(self, request: RecognitionRequest) -> ParseResult:
        raise NotImplementedError

    def close(self) -> None:
        """Release optional provider resources."""

