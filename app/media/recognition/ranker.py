"""提供可解释的评分、强制硬性条件/名称/覆盖门槛和稳定排序。"""

from copy import deepcopy
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import math
import re
from app.media.recognition.contracts import RecognitionStatus
from app.media.recognition.query_planner import canonical_title
from app.media.recognition.title_names import official_keyword_in_title, title_variants, tmdb_titles
from app.media.recognition.release_year import release_year
from app.media.recognition.episode_resolver import EpisodeContext, default_episode_season, merge_episode_context


MATCHING_VERSION = "rank-v3.5"
DEFAULT_WEIGHTS = {"title": .55, "type_episode": .20, "year": .15, "consensus": .07, "popularity": .03}


def title_similarity(first, second):
    a, b = canonical_title(first), canonical_title(second)
    return SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0


def title_match(first, second):
    first_key, second_key = canonical_title(first), canonical_title(second)
    if first_key == second_key and first_key:
        return 1.0, "exact"
    if official_keyword_in_title(first, second):
        return .97, "official_keyword"
    return SequenceMatcher(None, first_key, second_key, autojunk=False).ratio() if first_key and second_key else 0.0, "similarity"


def parser_title_check(parsed, titles, threshold):
    names = [(field, name) for field, text in (("title", parsed.title), ("secondary_title", parsed.secondary_title),
        *(("alias", name) for name in parsed.aliases)) for name in title_variants(text)]
    choices = [(title_match(name, official), field, name, official) for field, name in names for official in titles]
    if not choices:
        return {"provider_id": parsed.provider_id, "title": parsed.title or "", "primary_title": parsed.title,
            "matched_title": "", "similarity": 0, "threshold": threshold, "match_kind": "unavailable", "source_field": "title"}
    (score, kind), field, name, official = max(choices, key=lambda item: (item[0][0],
        item[0][1] == "exact", len(canonical_title(item[3])), item[2], item[3]))
    return {"provider_id": parsed.provider_id, "title": name, "primary_title": parsed.title,
        "matched_title": official, "similarity": score, "threshold": threshold,
        "raw_similarity": title_similarity(name, official), "match_kind": kind, "source_field": field}


def official_titles(candidate):
    return tmdb_titles({**candidate.search_fields, **candidate.detail_fields})


def sequel_number(title):
    text = canonical_title(title)
    match = re.search(r"(?:^|\s)([2-9]|[1-9]\d|ii|iii|iv|vi|vii|viii|ix|v|x)$", text)
    if not match:
        return None
    value = match[1]
    return {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}.get(value, int(value) if value.isdecimal() else None)


@dataclass(frozen=True)
class RankingResult:
    status: RecognitionStatus
    selected: object | None
    candidates: tuple
    reason: str
    coverage: str
    matching_version: str = MATCHING_VERSION
    conflict_details: tuple[dict, ...] = ()
    episode: EpisodeContext | None = None


class CandidateRanker:
    def __init__(self, settings):
        self.settings = settings.get("recognition", settings)["matching"]

    def rank(self, candidates, parses, *, episode=None, coverage="complete", conflicts=(), budget=None):
        settings = self.settings
        parsed = [p for p in parses if p.status == "success"]
        episode = merge_episode_context(episode or EpisodeContext(), parsed)
        primary = sorted({name for p in parsed for value in (p.title, p.secondary_title) for name in title_variants(value)})
        types = {p.media_type for p in parsed if p.media_type in {"tv", "movie"}}
        weights = settings.get("weights", DEFAULT_WEIGHTS)
        output = deepcopy(list(candidates))
        for c in output:
            if budget:
                budget.check()
            c.hard_reject, c.scores, c.available_weights = [], {}, {}
            info = {**c.search_fields, **c.detail_fields}
            adult = c.search_fields.get("adult") is True or c.detail_fields.get("adult") is True
            titles = official_titles(c)
            # 年份证据按候选分别核对，官方名称中的同一数字属于标题。
            years = {year for p in parsed if (year := release_year(p.year, titles=titles)) is not None}
            checks = [parser_title_check(p, titles, settings["min_name_similarity"])
                for p in sorted(parsed, key=lambda p: p.provider_id) if p.title or p.secondary_title or p.aliases]
            name = max((check["similarity"] for check in checks), default=0)
            c.scores["name_similarity"] = name
            c.scores["primary_min_similarity"] = min((max((title_similarity(a, b) for b in titles), default=0) for a in primary), default=0)
            c.scores["parser_group_min_similarity"] = min((check["similarity"] for check in checks), default=0)
            c.search_fields["_title_checks"] = checks
            if settings["adult_policy"] == "exclude" and adult:
                c.hard_reject.append("adult_excluded")
            if types and c.media_type not in types or len(types) > 1:
                c.hard_reject.append("type_conflict")
            if info.get("manual_rejected") is True:
                c.hard_reject.append("manual_rejected")
            # 缺少续作标记同样属于冲突；绝不移除数字。
            primary_sequels = {sequel_number(x) for x in primary if sequel_number(x) is not None}
            official_sequels = {sequel_number(x) for x in titles}
            if primary_sequels and not primary_sequels.intersection(official_sequels):
                c.hard_reject.append("sequel_conflict")
            elif primary and not primary_sequels and official_sequels and None not in official_sequels:
                c.hard_reject.append("sequel_conflict")
            def component(key, value):
                if value is not None:
                    weight = float(weights.get(key, DEFAULT_WEIGHTS[key]))
                    c.available_weights[key] = weight
                    c.scores[key] = value
                    c.scores[key+"_contribution"] = weight*value
            component("title", name)
            type_score = 1 if types == {c.media_type} else None
            context = default_episode_season(episode) if c.media_type == "tv" else episode
            c.search_fields["_episode_context"] = asdict(context) if context is not None else None
            season = context.season if context else next((p.season for p in parsed if p.season is not None), None)
            episodes = context.episodes if context else ()
            season_rows = c.detail_fields.get("seasons")
            if c.media_type == "movie" and (season is not None or episodes):
                c.hard_reject.append("movie_episode_conflict")
            season_row = None
            if c.media_type == "tv" and season is not None:
                if isinstance(season_rows, list):
                    season_row = next((x for x in season_rows if isinstance(x, dict) and x.get("season_number") == season), None)
                    if season_row is None:
                        c.hard_reject.append("season_not_found")
                    else:
                        type_score = 1
                        count = season_row.get("episode_count")
                        if type(count) is int and count >= 0 and episodes and max(episodes) > count:
                            c.hard_reject.append("episode_not_found")
                elif type(c.detail_fields.get("number_of_seasons")) is int and season > c.detail_fields["number_of_seasons"]:
                    c.hard_reject.append("season_not_found")
            episode_sufficient = not (c.media_type == "tv" and season is not None) or bool(season_row)
            if c.media_type == "tv" and episodes:
                episode_sufficient = bool(season_row) and type(season_row.get("episode_count")) is int
            component("type_episode", type_score)
            date = info.get("release_date") if c.media_type == "movie" else info.get("first_air_date")
            if c.media_type == "tv" and season is not None and season > 1:
                # 没有季信息就视为未知，绝不与首播年份比较。
                date = season_row.get("air_date") if season_row else None
            date_year = int(date[:4]) if isinstance(date, str) and date[:4].isdecimal() else None
            if len(years) > 1:
                c.hard_reject.append("parser_year_conflict")
            year_score = None
            if years and date_year:
                year_score = 1 if date_year in years else 0
                if not year_score:
                    c.hard_reject.append("year_conflict")
            component("year", year_score)
            families = {source["family"] for source in c.search_fields.get("_sources", [])
                if max((title_match(name, official)[0] for name in title_variants(source["title"]) for official in titles), default=0) >= settings["min_name_similarity"]}
            total_families = {p.evidence_family for p in parsed}
            component("consensus", min(1, max(0, len(families)-1)) if len(total_families) >= 2 else None)
            pop = info.get("popularity")
            component("popularity", min(1, math.log1p(max(0, pop))/math.log1p(1000)) if type(pop) in (int, float) and math.isfinite(pop) else None)
            denom = sum(c.available_weights.values())
            c.score = sum(c.scores[k+"_contribution"] for k in c.available_weights)/denom if denom else 0
            c.scores["evidence_sufficient"] = float(bool(primary) and bool(c.detail_fields.get("_verified")) and episode_sufficient and
                (any(check["match_kind"] in {"exact", "official_keyword"} for check in checks) or year_score == 1 or type_score == 1))
            c.scores["title_conflict"] = float(c.scores["parser_group_min_similarity"] < settings["min_name_similarity"])
        # 偏好只在已满足条件且得分相同的候选组内用于排序。
        def qualifies(c):
            return (not c.hard_reject and c.score >= settings["match_threshold"] and
                c.scores["name_similarity"] >= settings["min_name_similarity"] and not c.scores["title_conflict"] and
                bool(c.scores["evidence_sufficient"]))
        nonadult_qualified = any(qualifies(c) and c.search_fields.get("adult") is not True and c.detail_fields.get("adult") is not True for c in output)
        def ordering(c):
            info = {**c.search_fields, **c.detail_fields}
            adult_group = int(c.search_fields.get("adult") is True or c.detail_fields.get("adult") is True) if nonadult_qualified and settings["adult_policy"] in {"lowest", "lowest_priority"} else 0
            genre_ids = info.get("genre_ids") or [x.get("id") for x in (info.get("genres") or []) if isinstance(x, dict)]
            return (bool(c.hard_reject), adult_group, -c.score,
                    c.media_type != settings["preferred_type"], settings["preferred_genre_id"] not in genre_ids, c.media_type, c.tmdb_id)
        output.sort(key=ordering)
        allowed = [c for c in output if not c.hard_reject]
        for i, c in enumerate(output, 1):
            c.rank = i
        if not output:
            return RankingResult(RecognitionStatus.FAILED, None, (), "no_candidates", coverage)
        if not allowed:
            return RankingResult(RecognitionStatus.REVIEW_REQUIRED, None, tuple(output), "all_candidates_rejected", coverage)
        top = allowed[0]
        top_episode = default_episode_season(episode) if top.media_type == "tv" else episode
        details = [{"code": code} for code in conflicts]
        if top_episode and top_episode.review_reason:
            details.append({"code": top_episode.review_reason, "season": top_episode.season, "episodes": list(top_episode.episodes)})
        details.extend({"code": "parser_names_mismatch", **check}
            for check in top.search_fields["_title_checks"] if check["similarity"] < check["threshold"])
        group = [c for c in allowed if ordering(c)[1] == ordering(top)[1]]
        top.gap = top.score-group[1].score if len(group) > 1 else 1
        if top.scores["name_similarity"] < settings["min_name_similarity"]:
            reason = "insufficient_title_similarity"
        elif top.scores["title_conflict"] or conflicts or top_episode and top_episode.review_reason:
            reason = "conflicting_evidence"
        elif not top.scores["evidence_sufficient"] or coverage != "complete":
            reason = "insufficient_coverage"
        elif top.score < settings["match_threshold"]:
            reason = "below_match_threshold"
        elif top.gap+1e-12 < settings["min_score_gap"]:
            reason = "insufficient_score_gap"
        else:
            return RankingResult(RecognitionStatus.MATCHED, top, tuple(output), "verified_match", coverage, episode=top_episode)
        return RankingResult(RecognitionStatus.REVIEW_REQUIRED, None, tuple(output), reason, coverage,
            conflict_details=tuple(details), episode=top_episode)
