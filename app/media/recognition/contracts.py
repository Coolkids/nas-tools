"""多媒体名称识别器共用的数据契约。"""

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
    network_required: bool = False


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
    cache_source: str | None = None


class MediaNameRecognizer(ABC):
    """识别器接收标题并返回一份标准化解析结果。"""

    descriptor: RecognizerDescriptor

    @abstractmethod
    def parse(self, request: RecognitionRequest) -> ParseResult:
        raise NotImplementedError

    def close(self) -> None:
        """释放识别器持有的可选资源。"""
