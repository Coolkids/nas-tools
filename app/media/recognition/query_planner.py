"""生成确定且有界的搜索计划，并保留标题来源和覆盖信息。"""

from dataclasses import dataclass, replace
import hashlib
import unicodedata
from app.media.recognition.contracts import QueryPlanItem
from app.media.recognition.title_names import canonical_name, title_variants, has_cjk
from app.media.recognition.release_year import release_year


def canonical_title(value):
    return canonical_name(value)


def query_planning_policy(settings):
    """让查询顺序语义与已验证身份缓存的版本规则保持一致。"""
    recognition = settings.get("recognition") or settings
    query = recognition.get("query") or {}
    return {"version": 6, "prefer_english": bool(query.get("prefer_english", True))}


def is_english_title(value):
    """识别拉丁字母名称，同时避免将中日韩混合标题误判为英文。"""
    letters = [char for char in unicodedata.normalize("NFKC", value) if char.isalpha()]
    return bool(letters) and all("LATIN" in unicodedata.name(char, "") for char in letters)


@dataclass(frozen=True)
class QueryPlan:
    items: tuple[QueryPlanItem, ...]
    skipped: tuple[QueryPlanItem, ...]
    sources: dict[str, dict]
    conflicts: tuple[str, ...] = ()
    suppressed: tuple[QueryPlanItem, ...] = ()
    deferred: tuple[QueryPlanItem, ...] = ()

    @property
    def complete(self):
        return not self.skipped


class QueryPlanner:
    def __init__(self, settings):
        self.settings = settings.get("recognition", settings)
        self.policy = query_planning_policy(settings)

    def build(self, prepared, parses, *, language="zh-CN", previous=(), relax_year=False, include_deferred=False):
        grouped, sources = {}, {}
        hints = {p.media_type for p in parses if p.status == "success" and p.media_type in {"tv", "movie"}}
        def add_variant(title, kind, year, provider, family, field):
            if not isinstance(title, str) or not title.strip():
                return
            title = title.strip()
            canonical = canonical_title(title)
            if not canonical:
                return
            source_id = hashlib.sha256((provider+"\0"+field+"\0"+title).encode()).hexdigest()[:20]
            sources[source_id] = {"provider_id": provider, "family": family, "field": field, "title": title}
            endpoint = kind if kind in {"tv", "movie"} else next(iter(hints)) if len(hints) == 1 else "multi"
            key = (canonical, endpoint, year)
            group = grouped.setdefault(key, {"names": set(), "sources": set()})
            group["names"].add(title)
            group["sources"].add(source_id)
        def add(title, kind, year, provider, family, field):
            for name in title_variants(title):
                add_variant(name, kind, year, provider, family, field)
        for parse in sorted(parses, key=lambda x: x.provider_id):
            if parse.status != "success":
                continue
            if parse.media_type in {"tv", "movie"}:
                hints.add(parse.media_type)
            # 电视剧文件名中的年份可能指后续某一季，而非首播日期。
            year = (release_year(parse.year, titles=(parse.title, parse.secondary_title, *parse.aliases))
                    if parse.media_type == "movie" and not relax_year else None)
            for field, title in (("title", parse.title), ("secondary_title", parse.secondary_title)):
                add(title, parse.media_type, year, parse.provider_id, parse.evidence_family, field)
            for title in sorted(set(parse.aliases)):
                add(title, parse.media_type, year, parse.provider_id, parse.evidence_family, "alias")
        # 只使用预处理器识别出的标题，不使用文件名中的原始技术噪声。
        for title in prepared.candidate_titles if not grouped else ():
            if not any(key[0] == canonical_title(title) for key in grouped):
                add(title, prepared.media_type_hint, None, "preprocess", "local_rules", "candidate_title")
        # 单个引擎可能会拆分真实标题中的标点（例如
        # “魔法★探险家 -> 魔法 / 探险家”）。如果其他引擎认同完整名称，
        # 就不要把这些片段扩展成范围过大的必选搜索。
        raw_names = [canonical_title(value) for value in (prepared.clean_title, prepared.clean_secondary_title) if value]
        raw_titles = {key for key in grouped if any(f" {key[0]} " in f" {raw} " for raw in raw_names)}
        providers = {key: {sources[source]["provider_id"] for source in group["sources"]}
                     for key, group in grouped.items()}
        supported = {key for key, group in grouped.items()
            if len(providers[key]) >= 2
            and len({sources[source]["family"] for source in group["sources"]}) >= 2
            and key in raw_titles}
        corroborated = {(key[1:], first, second) for key in supported
                        for first in providers[key] for second in providers[key]}
        fragments = set()
        for key in grouped:
            if len(providers[key]) != 1:
                continue
            provider = next(iter(providers[key]))
            for full in raw_titles:
                if key == full or key[1:] != full[1:]:
                    continue
                # 多个引擎共享的完整别名也能佐证另一种语言的
                # 完整标题；仅使用 Anitopy 和 ML 时也适用。
                confirmed = full in supported or any((key[1:], provider, other) in corroborated
                    for other in providers[full])
                if not confirmed:
                    continue
                phrase = f" {key[0]} " in f" {full[0]} "
                compact, full_compact = key[0].replace(" ", ""), full[0].replace(" ", "")
                cjk_part = has_cjk(key[0]) and len(compact) < len(full_compact) and compact in full_compact
                if phrase or cjk_part:
                    fragments.add(key)
                    break
        prior = {(canonical_title(x.query), x.endpoint_type, x.year_filter) for x in previous}
        items, skipped, suppressed, deferred = [], [], [], []
        limit = self.settings["query"]["max_queries"]
        adult = self.settings["matching"]["adult_policy"] != "exclude"
        english_first = self.policy["prefer_english"] and any(key not in fragments and
            any(is_english_title(name) for name in group["names"]) for key, group in grouped.items())
        def priority(entry):
            key, group = entry
            english = self.policy["prefer_english"] and any(is_english_title(name) for name in group["names"])
            return (not english, *key[:2], key[2] or 0)
        for key, group in sorted(grouped.items(), key=priority):
            if key in prior:
                continue
            query_id = hashlib.sha256(repr((key, language, adult)).encode()).hexdigest()[:24]
            item = QueryPlanItem(query_id, sorted(group["names"])[0], key[1], tuple(sorted(group["sources"])),
                key[2], language, adult, priority=len(items))
            if key in fragments:
                suppressed.append(replace(item, budget_reason="REDUNDANT_TITLE_FRAGMENT"))
            elif english_first and not include_deferred and not any(is_english_title(name) for name in group["names"]):
                deferred.append(replace(item, budget_reason="ENGLISH_NAME_PREFERRED"))
            elif limit is not None and len(items)+len(prior) >= limit:
                skipped.append(replace(item, budget_reason="QUERY_BUDGET_LIMIT"))
            else:
                items.append(item)
        return QueryPlan(tuple(items), tuple(skipped), sources,
            ("parser_type_conflict",) if len(hints) > 1 else (), tuple(suppressed), tuple(deferred))
