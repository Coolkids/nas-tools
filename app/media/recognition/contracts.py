"""多媒体名称识别器共用的数据契约。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from enum import StrEnum
import math


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


class RecognitionStatus(StrEnum):
    MATCHED = "MATCHED"
    PARSED = "PARSED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


def _positive_identity(media_type, tmdb_id):
    if media_type not in ("movie", "tv") or type(tmdb_id) is not int or tmdb_id <= 0:
        raise ValueError("作品身份必须是movie/tv及正整数TMDB ID")


@dataclass(frozen=True, slots=True)
class TokenSpan:
    source: str
    original_start: int
    original_end: int
    clean_start: int
    clean_end: int
    rule_id: str | None = None
    synthetic: bool = False
    mapping_quality: str = "exact"

    def __post_init__(self):
        if self.source not in ("title", "secondary_title"):
            raise ValueError("跨度来源必须是主标题或副标题")
        bounds = (self.original_start, self.original_end, self.clean_start, self.clean_end)
        if any(type(value) is not int or value < 0 for value in bounds):
            raise ValueError("跨度必须使用非负Unicode码点索引")
        if self.original_start > self.original_end or self.clean_start > self.clean_end:
            raise ValueError("跨度区间不能倒置")
        if self.mapping_quality not in ("exact", "range"):
            raise ValueError("不支持的跨度映射质量")


@dataclass(frozen=True, slots=True)
class RuleApplication:
    rule_id: str
    operation: str
    source: str
    original_start: int
    original_end: int
    replacement: str | None = None
    episode_offset: int | None = None


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule_id: str
    operation: str
    source: str
    input_title: str
    output_title: str
    pattern: str = ""
    replacement: str = ""
    front: str = ""
    back: str = ""
    offset: str = ""


@dataclass(frozen=True, slots=True)
class PreprocessResult:
    raw_title: str
    clean_title: str
    rule_version: str
    preprocessor_version: str
    raw_secondary_title: str | None = None
    clean_secondary_title: str | None = None
    candidate_titles: tuple[str, ...] = ()
    year_hint: int | None = None
    media_type_hint: str = "unknown"
    season_tokens: tuple[str, ...] = ()
    episode_tokens: tuple[str, ...] = ()
    codecs: tuple[str, ...] = ()
    resolutions: tuple[str, ...] = ()
    applied_rules: tuple[RuleApplication, ...] = ()
    token_spans: tuple[TokenSpan, ...] = ()
    rule_matches: tuple[RuleMatch, ...] = ()

    def __post_init__(self):
        # Immutable, pickleable snapshots can be passed to reusable spawn workers.
        if not isinstance(self.raw_title, str) or not isinstance(self.clean_title, str):
            raise ValueError("主标题必须是字符串")
        if any(value is not None and not isinstance(value, str)
               for value in (self.raw_secondary_title, self.clean_secondary_title)):
            raise ValueError("副标题必须是字符串或null")
        for name in ("candidate_titles", "season_tokens", "episode_tokens", "codecs", "resolutions", "applied_rules", "token_spans", "rule_matches"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for name in ("candidate_titles", "season_tokens", "episode_tokens", "codecs", "resolutions"):
            if any(not isinstance(value, str) for value in getattr(self, name)):
                raise ValueError("预处理文本标签必须是不可变字符串")
        if any(not isinstance(rule, RuleApplication) for rule in self.applied_rules):
            raise ValueError("规则记录必须使用RuleApplication契约")
        if any(not isinstance(rule, RuleMatch) for rule in self.rule_matches):
            raise ValueError("规则命中记录必须使用RuleMatch契约")
        if not self.rule_version or not self.preprocessor_version:
            raise ValueError("预处理必须携带规则和实现版本")
        for span in self.token_spans:
            if not isinstance(span, TokenSpan):
                raise ValueError("跨度必须使用TokenSpan契约")
            raw = self.raw_title if span.source == "title" else self.raw_secondary_title or ""
            clean = self.clean_title if span.source == "title" else self.clean_secondary_title or ""
            if span.original_end > len(raw) or span.clean_end > len(clean):
                raise ValueError("跨度超出对应原文或处理后文本")


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    field: str
    source: str
    value: str
    span: TokenSpan | None = None


@dataclass(frozen=True, slots=True)
class NormalizedParse:
    provider_id: str
    provider_version: str
    evidence_family: str
    status: str
    title: str | None = None
    secondary_title: str | None = None
    aliases: tuple[str, ...] = ()
    media_type: str = "unknown"
    year: int | None = None
    season: int | None = None
    episodes: tuple[int, ...] = ()
    episode_numbering: str = "unknown"
    field_evidence: tuple[FieldEvidence, ...] = ()
    error: str | None = None
    duration_ms: float = 0
    cache_source: str | None = None
    llm: dict[str, Any] | None = None
    release_metadata: dict[str, str | None] | None = None

    def __post_init__(self):
        for name in ("aliases", "episodes", "field_evidence"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class QueryPlanItem:
    query_id: str
    query: str
    endpoint_type: str
    sources: tuple[str, ...] = ()
    year_filter: int | None = None
    language: str = "zh-CN"
    include_adult: bool = False
    page: int = 1
    priority: int = 0
    budget_reason: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass
class SearchResponse:
    query_id: str
    request_params: dict[str, Any]
    raw_json: dict[str, Any]
    page: int
    total_pages: int
    total_results: int
    http_status: int
    retries: int = 0
    duration_ms: float = 0
    cache_source: str | None = None
    consumed_results: list[dict] | None = None


@dataclass
class Candidate:
    media_type: str
    tmdb_id: int
    title: str
    search_fields: dict[str, Any] = field(default_factory=dict)
    detail_fields: dict[str, Any] = field(default_factory=dict)
    evidence: list[FieldEvidence] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    available_weights: dict[str, float] = field(default_factory=dict)
    hard_reject: list[str] = field(default_factory=list)
    score: float | None = None
    rank: int | None = None
    gap: float | None = None

    def __post_init__(self):
        _positive_identity(self.media_type, self.tmdb_id)


@dataclass(frozen=True, slots=True)
class IdentityEntry:
    media_type: str
    tmdb_id: int
    title: str
    rule_version: str
    parser_version: str
    matching_version: str
    verification_level: str
    year: int | None = None
    normalized_titles: tuple[str, ...] = ()
    ambiguity_group: str | None = None
    score: float | None = None
    source_trace_id: str | None = None
    override_version: int = 0

    def __post_init__(self):
        _positive_identity(self.media_type, self.tmdb_id)
        object.__setattr__(self, "normalized_titles", tuple(self.normalized_titles))


@dataclass
class CacheEnvelope:
    layer: str
    cache_key: str
    payload: Any
    cycle_id: str
    storage_generation: str
    payload_version: int = 1
    hit_count: int = 0
    fresh_until: float | None = None
    stale_until: float | None = None
    last_access: float | None = None
    source_trace_id: str | None = None
    override_version: int = 0
    request_params: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 3
    created_at: float | None = None
    next_retry_at: float = 0.0
    refresh_failures: int = 0
    refresh_state: str = "idle"
    refresh_error: str | None = None
    needs_revalidation: bool = False

    def __post_init__(self):
        if self.layer not in ("identity", "search", "detail", "episode"):
            raise ValueError("不支持的识别缓存层")
        if type(self.schema_version) is not int or self.schema_version != 3 or not self.cycle_id or not self.storage_generation:
            raise ValueError("缓存必须携带V3版本、周期和存储代次")
        for value, minimum in ((self.payload_version, 1), (self.hit_count, 0), (self.override_version, 0)):
            if type(value) is not int or value < minimum:
                raise ValueError("缓存版本和计数必须是合法整数")
        for value in (self.fresh_until, self.stale_until, self.last_access, self.created_at, self.next_retry_at):
            if value is not None and (type(value) not in (int, float) or type(value) is float and not math.isfinite(value)):
                raise ValueError("缓存时间必须是有限epoch秒数或null")
        if (self.fresh_until is None) != (self.stale_until is None):
            raise ValueError("不过期缓存的fresh和stale界限必须同时为null")
        if self.fresh_until is not None and self.stale_until < self.fresh_until:
            raise ValueError("stale界限不能早于fresh界限")
        if (type(self.refresh_failures) is not int or self.refresh_failures < 0 or type(self.needs_revalidation) is not bool
                or self.refresh_state not in {"idle", "retry", "needs_revalidation"}
                or self.refresh_error is not None and (not isinstance(self.refresh_error, str) or len(self.refresh_error) > 128)):
            raise ValueError("invalid refresh metadata")
        self.next_retry_at = float(self.next_retry_at)
        if self.created_at is not None:
            self.created_at = float(self.created_at)


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    total_deadline_ns: int | None
    stage_deadline_ns: int | None
    remaining_ms: float | None
    cancellation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RecognitionOutcome:
    request_id: str
    status: RecognitionStatus
    result: IdentityEntry | None = None
    failure_code: str | None = None
    season: int | None = None
    episodes: tuple[int, ...] = ()
    score: float | None = None
    duration_ms: float = 0
    cache_source: str | None = None
    warnings: tuple[str, ...] = ()
    source_trace_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "status", RecognitionStatus(self.status))
        object.__setattr__(self, "episodes", tuple(self.episodes))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if self.status == RecognitionStatus.MATCHED and not isinstance(self.result, IdentityEntry):
            raise ValueError("MATCHED必须提供已验证作品身份")
        if self.status == RecognitionStatus.MATCHED and self.result.verification_level != "verified":
            raise ValueError("MATCHED不能使用未经完整验证的候选身份")
        if self.status != RecognitionStatus.MATCHED and self.result is not None:
            raise ValueError("非MATCHED结果不得携带业务作品身份")

    @property
    def business_identity(self):
        return self.result if self.status == RecognitionStatus.MATCHED else None


@dataclass(frozen=True, slots=True)
class RecognitionEvent:
    request_id: str
    seq: int
    node_id: str
    event_type: str
    status: str
    emitted_at_utc: str
    offset_ns: int
    schema_version: int = 3
    parent_id: str | None = None
    started_at_utc: str | None = None
    duration_ms: float | None = None
    durability: str = "volatile"
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if type(self.seq) is not int or self.seq < 1:
            raise ValueError("事件序号必须是正整数")
        if type(self.schema_version) is not int or self.schema_version != 3 or not self.node_id:
            raise ValueError("事件必须携带V3版本和稳定node_id")
        if self.durability not in ("persistent", "temporary", "volatile"):
            raise ValueError("不支持的事件留存能力")
