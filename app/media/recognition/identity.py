"""复用已验证的身份信息，并持久保存带版本的人工作用选择。"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from threading import Event, Thread
import time

from sqlalchemy import select
from app.db.models import RECOGNITIONOVERRIDE as OverrideRow
from app.media.recognition.contracts import IdentityEntry
from app.media.recognition.query_planner import canonical_title, query_planning_policy
from app.media.recognition.ranker import MATCHING_VERSION
from app.media.recognition.repository import RecognitionRepository, encode, utc_now
from app.media.recognition.episode_resolver import find_season, find_bracket_episode
from app.media.recognition.release_year import explicit_release_year, release_year


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def detail_signature(detail):
    """仅包含身份和校验相关数据；人气变化不会造成影响。"""
    return digest({key: detail.get(key) for key in ("id", "media_type", "title", "name", "original_title", "original_name",
        "release_date", "first_air_date", "adult", "seasons", "genres")})


def media_kind(value):
    text = getattr(value, "name", value)
    return {"movie": "movie", "tv": "tv", "anime": "tv", "电影": "movie",
            "电视剧": "tv", "动漫": "tv"}.get(str(text or "").lower(), "unknown")


@dataclass(frozen=True)
class IdentityScope:
    canonical_name: str
    media_type: str = "unknown"
    year: int | None = None

    def __post_init__(self):
        if not self.canonical_name or len(self.canonical_name) > 1024 or canonical_title(self.canonical_name) != self.canonical_name:
            raise ValueError("invalid_identity_scope_name")
        if self.media_type not in {"movie", "tv", "unknown"}:
            raise ValueError("invalid_identity_scope_type")
        if self.year is not None and (type(self.year) is not int or not 1870 <= self.year <= 2200):
            raise ValueError("invalid_identity_scope_year")

    @property
    def key(self):
        return digest({"scope_version": 1, **asdict(self)})


def scope_for(prepared, *, name=None, mtype=None, year=None):
    text = name if name is not None else prepared.clean_title
    text = text.replace("_", " ")
    text = re.sub(r"(?i)\.(?:mkv|mp4|avi|ts|m2ts|iso|mov|wmv|flv|strm)$", "", text)
    if name is None:
        # 发布组不属于作品名。如果方括号标题后只有集数或技术后缀，
        # 则保留方括号中的标题。
        if group := re.match(r"^\s*(?:(?:\[[^\]]+\]|【[^】]+】)\s*)+", text):
            tail = text[group.end():]
            if tail and not re.match(r"(?i)^(?:-\s*\d|S\d|E[P]?\d|第|\d{3,4}[pi]\b)", tail):
                text = tail
    episodic = re.search(r"(?i)(?:(?<![A-Za-z0-9])S\d{1,3}(?:E\d{1,5})?|(?<![A-Za-z0-9])(?:EP?|SP)\d{1,5}|第\s*\d+\s*[季集话話]|\bSeason\s*\d{1,3}\b|\s-\s\d{1,5}(?:\s*[-~]\s*\d{1,5})?(?=\s|\[|\.|$))", text)
    bracket = find_bracket_episode(text)
    if bracket is not None and (episodic is None or bracket.start() < episodic.start()):
        episodic = bracket
    _season, season_marker = find_season(text)
    if season_marker is not None and (episodic is None or season_marker.start() < episodic.start()):
        episodic = season_marker
    if name is None:
        technical = re.search(r"(?i)\b(?:\d{3,4}[pi]|[248]k|h[ .]?26[45]|x26[45]|hevc|av1|web[ .-]?dl|webrip|bluray|bdrip|hdtv)\b", text)
        boundaries = [match.start() for match in (episodic, technical) if match is not None]
        if boundaries:
            # 将集数坐标和技术后缀视为文件元数据。不要
            # 从真实标题前缀中删除 HDR、Atmos 等词。
            text = text[:min(boundaries)]
        text = text.rstrip(" .-_[【(")
        text = re.sub(r"(?i)(?:\s*(?:\[[^\]]*(?:字幕|简[繁体中]|繁[体中]|chs|cht|aac|flac|hdr)[^\]]*\]|【[^】]*字幕[^】]*】))+$", "", text)
    kind = media_kind(mtype)
    if kind == "unknown" and episodic:
        kind = "tv"
    # 自动年份不能删除标题中的数字；电视剧要求独立的年份标记。
    if year is None:
        year = release_year(prepared.year_hint)
        if kind == "tv" and not explicit_release_year(year, (prepared.clean_title, prepared.clean_secondary_title)):
            year = None
    elif isinstance(year, str) and year.isdecimal():
        year = int(year)
    if year is not None:
        text = re.sub(r"(?<!\d)"+str(year)+r"(?!\d)", " ", text)
    name = canonical_title(text)
    # 仅含集数的名称无法确定作品身份；协调器会使用
    # 单独准备的父目录，并保留当前文件的集数信息。
    return IdentityScope(name, kind, year) if name and not name.isdecimal() else None


class OverrideConflict(ValueError):
    pass


class OverrideRepository:
    def __init__(self, repository=None):
        self.repository = repository or RecognitionRepository()

    def get(self, scope):
        with self.repository.transaction(lock=False) as (session, _state):
            row = session.get(OverrideRow, scope.key)
            if row is None:
                return {"version": 0, "choice": None, "scope": asdict(scope)}
            value = json.loads(row.PAYLOAD)
            if value.get("scope") != asdict(scope) or value.get("kind") != "override":
                raise ValueError("invalid_override_scope")
            return {**value, "version": row.VERSION}

    def save(self, scope, choice, *, expected_version, actor, request_id=None, task_fence=None):
        if type(expected_version) is not int or expected_version < 0 or not str(actor).strip() or len(str(actor)) > 128:
            raise ValueError("invalid_override_update")
        if choice is not None:
            if IdentityEntry(**choice).verification_level != "verified":
                raise ValueError("unverified_override_choice")
        with self.repository.transaction() as (session, _state):
            if task_fence is not None:
                from app.db.models import RECOGNITIONTASK
                from app.media.recognition.budget import RecognitionCancelled
                task = session.get(RECOGNITIONTASK, request_id)
                control = json.loads(task.CONTROL or "{}") if task is not None else {}
                if (task is None or task.OWNER_ID != task_fence["owner"] or task.LEASE_UNTIL <= time.time()
                        or control.get("generation") != task_fence["generation"] or task.CANCEL_REASON
                        or task.STATE != "RUNNING"):
                    raise RecognitionCancelled(task.CANCEL_REASON if task is not None and task.CANCEL_REASON else "service_shutdown")
            row = session.get(OverrideRow, scope.key)
            version = row.VERSION if row else 0
            if version != expected_version:
                raise OverrideConflict("VALIDATION_CONFLICT")
            old = json.loads(row.PAYLOAD).get("choice") if row else None
            version += 1
            audit = {"kind": "override_audit", "scope": asdict(scope), "version": version,
                "actor": str(actor), "request_id": request_id, "updated_at": utc_now(),
                "before": old, "after": choice}
            session.add(OverrideRow(OVERRIDE_KEY=digest({"audit": scope.key, "version": version}),
                VERSION=version, PAYLOAD=encode(audit)))
            value = {"kind": "override", "scope": asdict(scope), "choice": choice, "updated_at": audit["updated_at"]}
            if row is None:
                session.add(OverrideRow(OVERRIDE_KEY=scope.key, VERSION=version, PAYLOAD=encode(value)))
            else:
                row.VERSION, row.PAYLOAD = version, encode(value)
        return {**value, "version": version}

    def audit(self, scope, *, after_version=0, limit=100):
        if type(after_version) is not int or after_version < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_audit_page")
        current = self.get(scope)["version"]
        keys = [digest({"audit": scope.key, "version": version})
                for version in range(after_version+1, min(current, after_version+limit)+1)]
        if not keys:
            return []
        with self.repository.transaction(lock=False) as (session, _state):
            rows = session.execute(select(OverrideRow).where(OverrideRow.OVERRIDE_KEY.in_(keys))).scalars()
            return sorted((json.loads(row.PAYLOAD) for row in rows), key=lambda value: value["version"])


class IdentityService:
    def __init__(self, cache, settings, *, overrides=None):
        self.cache, self.settings = cache, settings
        self.overrides = overrides or OverrideRepository()

    def versions(self, prepared):
        from app.media.recognition.registry import registry
        config = self.settings["recognition"]
        providers = {}
        discovered = registry.discover()
        for name in ("anitopy", "anitopy_ml", "openai"):
            value = config["providers"][name]
            if name != "anitopy" and not value.get("enabled"):
                continue
            providers[name] = {"implementation": discovered[name].descriptor.version,
                "options": {key: item for key, item in value.items() if key not in {"api_key", "api_key_set", "options"}}}
            if name == "openai":
                from app.media.recognition.secrets import SecretManager
                try:
                    providers[name]["secret_revision"] = SecretManager().metadata()["secret_revision"]
                except Exception:
                    providers[name]["secret_revision"] = "unavailable"
        return {"rule": prepared.rule_version, "preprocessor": prepared.preprocessor_version,
            "parser": digest(providers), "matching": MATCHING_VERSION,
            "policy": digest({"query": config["query"], "matching": config["matching"],
                "query_planning": query_planning_policy(self.settings)})}

    def key(self, scope):
        return self.cache.key("identity", {"title": scope.canonical_name})

    def get(self, key, scope, override_version, *, hit=True):
        if key != self.key(scope):
            return None
        envelope = self.cache.get("identity", key, hit=False)
        if envelope is None or envelope.needs_revalidation:
            return None
        try:
            value = IdentityEntry(**envelope.payload)
        except (ValueError, TypeError):
            return None
        params = envelope.request_params or {}
        if not isinstance(params, dict):
            return None
        try:
            stored_scope = IdentityScope(**params["scope"])
        except (KeyError, ValueError, TypeError):
            return None
        evidence = params.get("verification", {})
        if not isinstance(evidence, dict):
            return None
        def valid_score(field):
            item = evidence.get(field)
            return type(item) in (int, float) and math.isfinite(item) and 0 <= item <= 1
        if (params.get("coverage") != "complete" or stored_scope.canonical_name != scope.canonical_name or
                value.verification_level != "verified" or value.override_version != envelope.override_version or
                (stored_scope == scope and value.override_version != override_version) or
                (stored_scope != scope and (value.override_version != 0 or override_version != 0)) or
                scope.media_type != "unknown" and value.media_type != scope.media_type or
                scope.canonical_name not in value.normalized_titles or value.ambiguity_group != stored_scope.key or
                type(value.score) not in (int, float) or not math.isfinite(value.score) or
                evidence.get("entity") != [value.media_type, value.tmdb_id] or
                evidence.get("detail_verified") is not True or evidence.get("hard_reject") != [] or
                not all(valid_score(field) for field in ("name_similarity", "score", "gap")) or
                evidence.get("score") != value.score):
            return None
        # 只有成功接受的读取才计为身份缓存命中。
        current = self.cache.get("identity", key, hit=hit)
        return value if current is not None and not current.needs_revalidation and current.cycle_id == envelope.cycle_id and current.payload_version == envelope.payload_version else None

    @contextmanager
    def singleflight(self, key, budget):
        import uuid
        owner, stopped = uuid.uuid4().hex, Event()
        namespace, fence = self.cache.namespace("identity"), None
        if not self.cache.enabled():
            yield None
            return
        while fence is None:
            budget.check()
            fence = self.cache.store.lease_acquire(namespace, key, owner, 30) or None
            if fence is None:
                time.sleep(.02)
        def heartbeat():
            while not stopped.wait(5):
                try:
                    if not self.cache.store.lease_renew(namespace, key, owner, fence, 30):
                        return
                except Exception:
                    return
        thread = Thread(target=heartbeat, name="recognition-identity-lease", daemon=True)
        thread.start()
        try:
            yield (owner, fence)
        finally:
            stopped.set()
            thread.join(1)
            try:
                self.cache.store.lease_release(namespace, key, owner, fence)
            except Exception:
                pass  # 后端不可用期间，带栅栏的过期租约仍是安全的

    def put(self, key, value, scope, versions, *, origin, language, lease=None, verification=None):
        # SQL 与外部缓存无法组成同一个事务。保存的
        # 版本号可确保并发中的旧写入不会被后续读取者使用。
        if not isinstance(verification, dict) or self.overrides.get(scope)["version"] != value.override_version:
            return False
        owner, fence = lease or (None, None)
        return self.cache.put("identity", key, value, override_version=value.override_version,
            source_trace_id=value.source_trace_id, owner=owner, fence=fence,
            request_params={"scope": asdict(scope), "versions": versions, "coverage": "complete",
                            "origin": origin, "language": language, "verification": verification})
