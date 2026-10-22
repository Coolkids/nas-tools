"""Shared multi-engine parsing, query planning and candidate ranking."""

from dataclasses import replace


def _merge_query_rounds(plan, searches, later_plan, later):
    """Retain all observed candidates and incomplete work across fallback rounds."""
    from app.media.recognition.candidate_merger import merge_candidates
    from app.media.recognition.tmdb_searcher import SearchBatch
    def unique(items):
        return tuple({item.query_id: item for item in items}.values())
    items = unique(plan.items+later_plan.items)
    executed = {item.query_id for item in items}
    skipped = tuple(item for item in unique(plan.skipped+later_plan.skipped) if item.query_id not in executed)
    requested = executed | {item.query_id for item in skipped}
    combined = replace(plan, items=items, skipped=skipped,
        deferred=tuple(item for item in unique(plan.deferred+later_plan.deferred) if item.query_id not in requested),
        suppressed=unique(plan.suppressed+later_plan.suppressed),
        sources={**plan.sources, **later_plan.sources}, conflicts=tuple(dict.fromkeys(plan.conflicts+later_plan.conflicts)))
    responses = searches.responses+later.responses
    candidates = merge_candidates(responses, combined)
    details = {}
    for candidate in (*searches.candidates, *later.candidates):
        key = (candidate.media_type, candidate.tmdb_id)
        if candidate.detail_fields.get("_verified") or not details.get(key, {}).get("_verified"):
            details[key] = candidate.detail_fields
    for candidate in candidates:
        candidate.detail_fields = details.get((candidate.media_type, candidate.tmdb_id), {})
        if candidate.detail_fields.get("_verified"):
            candidate.title = candidate.detail_fields.get("title") or candidate.detail_fields.get("name") or candidate.title
    return combined, SearchBatch(responses, tuple(candidates),
        "complete" if searches.coverage == later.coverage == "complete" else "partial",
        searches.errors+later.errors, searches.logical_query_count+later.logical_query_count,
        searches.http_attempt_count+later.http_attempt_count, searches.detail_complete and later.detail_complete,
        searches.coverage_issues+later.coverage_issues)


class RecognitionService:
    def analyze_v3(self, prepared, settings, *, runtime=None, budget=None,
                   api_key=None, tmdb_base_url=None, network_allowed=True, cache=True, episode=None,
                   lookup_overrides=None, client=None, emit=None, relax_year=False):
        """S12–S15 analysis API; business identity commits belong to S16."""
        from app.media.recognition.budget import RecognitionBudget
        from app.media.recognition.parser_manager import ParserManager
        from app.media.recognition.query_planner import QueryPlanner, is_english_title
        from app.media.recognition.ranker import CandidateRanker
        from app.media.recognition.tmdb_client import TMDBClient
        from app.media.recognition.tmdb_searcher import TMDBSearcher
        from app.media.recognition.contracts import RecognitionStatus
        from app.media.recognition.episode_resolver import resolve_episode, merge_episode_context
        rec = settings.get("recognition", settings)
        emit = emit or (lambda *args, **kwargs: None)
        def record_plan(plan, phase):
            for item in (*plan.items, *plan.skipped, *plan.suppressed, *plan.deferred):
                emit("query_planned", node_id="query_plan", status="skipped" if item.budget_reason else "success",
                    query_id=item.query_id, query=item.query, endpoint_type=item.endpoint_type,
                    year_filter=item.year_filter, priority=item.priority, reason=item.budget_reason, phase=phase)
            emit("query_plan_completed", node_id="query_plan", status="success",
                query_count=len(plan.items), skipped_count=len(plan.skipped), suppressed_count=len(plan.suppressed),
                deferred_count=len(plan.deferred), complete=plan.complete, phase=phase, reason=None)
        budget = budget or RecognitionBudget(rec["timeouts"].get("total_ms"))
        parsers = ParserManager(settings, runtime=runtime, budget=budget, emit=emit).run(prepared, network_allowed=network_allowed, cache=cache)
        episode = merge_episode_context(episode or resolve_episode(prepared.clean_title,
            secondary_title=prepared.clean_secondary_title), parsers.results)
        lookup = parsers.results
        overrides = lookup_overrides or {}
        if overrides:
            lookup = tuple(replace(p,
                title=overrides.get("name", p.title), aliases=() if "name" in overrides else p.aliases,
                secondary_title=None if "name" in overrides else p.secondary_title,
                year=overrides.get("year", p.year), media_type=overrides.get("media_type", p.media_type))
                if p.status == "success" else p for p in lookup)
        with_budget = budget.stage("query_plan", rec["timeouts"].get("query_plan_ms"))
        with_budget.check()
        planner = QueryPlanner(settings)
        language = client.language if client is not None else "zh-CN"
        plan = planner.build(prepared, lookup, language=language)
        english_first = planner.policy["prefer_english"] and any(is_english_title(item.query) for item in plan.items)
        record_plan(plan, "english_first" if english_first else "initial")
        with_budget.check()
        if not network_allowed:
            return {"parsers": parsers, "plan": plan, "search": None, "ranking": None, "episode": episode}
        client = client or TMDBClient(settings, api_key=api_key, base_url=tmdb_base_url, runtime=runtime, budget=budget)
        search_budget = budget.stage("tmdb_stage", rec["timeouts"].get("tmdb_stage_ms"))
        searches = TMDBSearcher(client).run(plan, lookup, cache=cache, budget=search_budget)
        fallback_budget = None
        year_relaxed = False
        def fallback_stage():
            nonlocal fallback_budget
            if fallback_budget is None:
                fallback_budget = search_budget.stage("fallback", rec["timeouts"].get("fallback_ms"))
            fallback_budget.check()
            return fallback_budget
        def run_fallback(next_plan, phase):
            nonlocal plan, searches
            stage = fallback_stage()
            record_plan(next_plan, phase)
            later = TMDBSearcher(client).run(next_plan, lookup, cache=cache, budget=stage)
            stage.check()
            plan, searches = _merge_query_rounds(plan, searches, next_plan, later)
        # A normal-mode empty movie query may drop the server-side year filter.
        # Ranking still validates the original year; failures/partial coverage
        # must never trigger another query or reset the stage/count budget.
        if (relax_year and not searches.candidates and not searches.errors and searches.coverage == "complete"
                and any(item.year_filter is not None for item in plan.items)):
            fallback_stage()
            fallback = planner.build(prepared, lookup, language=client.language,
                previous=plan.items, relax_year=True)
            run_fallback(fallback, "relaxed_year")
            year_relaxed = True
        def rank_current():
            rank_budget = budget.stage("rank", rec["timeouts"].get("rank_ms"))
            rank_budget.check()
            ranked = CandidateRanker(settings).rank(searches.candidates, lookup, episode=episode,
                coverage=searches.coverage, conflicts=plan.conflicts, budget=rank_budget)
            if not searches.candidates and searches.errors:
                codes = sorted({error["error"] for error in searches.errors})
                timed_out = next((code for code in codes if "timeout" in code or code.endswith("deadline_exceeded")), None)
                ranked = replace(ranked, status=RecognitionStatus.TIMEOUT if timed_out else RecognitionStatus.FAILED,
                                 reason=timed_out or codes[0])
            rank_budget.check()
            return ranked
        ranked = rank_current()
        if ranked.status != RecognitionStatus.MATCHED and plan.deferred:
            fallback_stage()
            emit("language_fallback_started", node_id="query_plan", status="running", reason=ranked.reason)
            fallback = planner.build(prepared, lookup, language=client.language,
                previous=plan.items, relax_year=year_relaxed, include_deferred=True)
            run_fallback(fallback, "other_names")
            if (relax_year and not year_relaxed and not searches.candidates and not searches.errors
                    and searches.coverage == "complete" and any(item.year_filter is not None for item in plan.items)):
                fallback_stage()
                fallback = planner.build(prepared, lookup, language=client.language,
                    previous=plan.items, relax_year=True, include_deferred=True)
                run_fallback(fallback, "relaxed_year")
            ranked = rank_current()
        elif ranked.status == RecognitionStatus.MATCHED and plan.deferred:
            emit("english_query_confirmed", node_id="query_plan", status="success", deferred_count=len(plan.deferred),
                media_type=ranked.selected.media_type, tmdb_id=ranked.selected.tmdb_id, reason=None)
        return {"parsers": parsers, "lookup_parses": lookup, "plan": plan, "search": searches, "ranking": ranked, "episode": episode}



recognition_service = RecognitionService()
