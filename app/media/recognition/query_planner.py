"""Deterministic, bounded search plans preserving title provenance and coverage."""

from dataclasses import dataclass
import hashlib
import unicodedata
from app.media.recognition.contracts import QueryPlanItem
from app.media.recognition.settings import _as_bool
from app.media.recognition.title_names import canonical_name, title_variants


def canonical_title(value):
    return canonical_name(value)


def query_planning_policy(settings):
    """Share query-order semantics with verified identity cache versioning."""
    laboratory = settings.get("laboratory") or {}
    return {"version": 2, "prefer_english": _as_bool(laboratory.get("search_en_title", False))}


def is_english_title(value):
    """Recognize Latin-script names without mistaking mixed CJK titles for English."""
    letters = [char for char in unicodedata.normalize("NFKC", value) if char.isalpha()]
    return bool(letters) and all("LATIN" in unicodedata.name(char, "") for char in letters)


@dataclass(frozen=True)
class QueryPlan:
    items: tuple[QueryPlanItem, ...]
    skipped: tuple[QueryPlanItem, ...]
    sources: dict[str, dict]
    conflicts: tuple[str, ...] = ()

    @property
    def complete(self):
        return not self.skipped


class QueryPlanner:
    def __init__(self, settings):
        self.settings = settings.get("recognition", settings)
        self.policy = query_planning_policy(settings)

    def build(self, prepared, parses, *, language="zh-CN", previous=(), relax_year=False):
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
            # TV filename year may describe a later season, not its first air date.
            year = parse.year if parse.media_type == "movie" and not relax_year else None
            for field, title in (("title", parse.title), ("secondary_title", parse.secondary_title)):
                add(title, parse.media_type, year, parse.provider_id, parse.evidence_family, field)
            for title in sorted(set(parse.aliases)):
                add(title, parse.media_type, year, parse.provider_id, parse.evidence_family, "alias")
        # Only preprocessor-identified titles, never raw technical filename noise.
        for title in prepared.candidate_titles if not grouped else ():
            if not any(key[0] == canonical_title(title) for key in grouped):
                add(title, prepared.media_type_hint, None, "preprocess", "local_rules", "candidate_title")
        prior = {(canonical_title(x.query), x.endpoint_type, x.year_filter) for x in previous}
        items, skipped = [], []
        limit = self.settings["query"]["max_queries"]
        adult = self.settings["matching"]["adult_policy"] != "exclude"
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
            if limit is not None and len(items)+len(prior) >= limit:
                from dataclasses import replace
                skipped.append(replace(item, budget_reason="QUERY_BUDGET_LIMIT"))
            else:
                items.append(item)
        return QueryPlan(tuple(items), tuple(skipped), sources, ("parser_type_conflict",) if len(hints) > 1 else ())
