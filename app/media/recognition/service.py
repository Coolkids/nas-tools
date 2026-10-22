"""Shared multi-engine parsing, query planning and candidate ranking."""

from dataclasses import replace


class RecognitionService:
    def analyze_v3(self, prepared, settings, *, runtime=None, budget=None,
                   api_key=None, tmdb_base_url=None, network_allowed=True, cache=True, episode=None,
                   lookup_overrides=None, client=None, emit=None, relax_year=False):
        """S12–S15 analysis API; business identity commits belong to S16."""
        from app.media.recognition.budget import RecognitionBudget
        from app.media.recognition.parser_manager import ParserManager
        from app.media.recognition.query_planner import QueryPlanner
        from app.media.recognition.ranker import CandidateRanker
        from app.media.recognition.tmdb_client import TMDBClient
        from app.media.recognition.tmdb_searcher import TMDBSearcher, SearchBatch
        rec = settings.get("recognition", settings)
        emit = emit or (lambda *args, **kwargs: None)
        def record_plan(plan):
            for item in (*plan.items, *plan.skipped):
                emit("query_planned", node_id="query_plan", status="skipped" if item.budget_reason else "success",
                    query_id=item.query_id, query=item.query, endpoint_type=item.endpoint_type,
                    year_filter=item.year_filter, priority=item.priority, reason=item.budget_reason)
            emit("query_plan_completed", node_id="query_plan", status="success",
                query_count=len(plan.items), skipped_count=len(plan.skipped), complete=plan.complete)
        budget = budget or RecognitionBudget(rec["timeouts"].get("total_ms"))
        parsers = ParserManager(settings, runtime=runtime, budget=budget, emit=emit).run(prepared, network_allowed=network_allowed, cache=cache)
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
        plan = QueryPlanner(settings).build(prepared, lookup, language=client.language if client is not None else "zh-CN")
        record_plan(plan)
        with_budget.check()
        if not network_allowed:
            return {"parsers": parsers, "plan": plan, "search": None, "ranking": None}
        client = client or TMDBClient(settings, api_key=api_key, base_url=tmdb_base_url, runtime=runtime, budget=budget)
        search_budget = budget.stage("tmdb_stage", rec["timeouts"].get("tmdb_stage_ms"))
        searches = TMDBSearcher(client).run(plan, lookup, cache=cache, budget=search_budget)
        # A normal-mode empty movie query may drop the server-side year filter.
        # Ranking still validates the original year; failures/partial coverage
        # must never trigger another query or reset the stage/count budget.
        if (relax_year and not searches.candidates and not searches.errors and searches.coverage == "complete"
                and any(item.year_filter is not None for item in plan.items)):
            fallback_budget = search_budget.stage("fallback", rec["timeouts"].get("fallback_ms"))
            fallback_budget.check()
            fallback = QueryPlanner(settings).build(prepared, lookup, language=client.language,
                previous=plan.items, relax_year=True)
            record_plan(fallback)
            later = TMDBSearcher(client).run(fallback, lookup, cache=cache, budget=fallback_budget)
            fallback_budget.check()
            searches = SearchBatch(searches.responses+later.responses, later.candidates, later.coverage,
                searches.errors+later.errors, searches.logical_query_count+later.logical_query_count,
                searches.http_attempt_count+later.http_attempt_count, later.detail_complete)
            plan = replace(plan, items=plan.items+fallback.items, skipped=plan.skipped+fallback.skipped,
                sources={**plan.sources, **fallback.sources})
        if episode is None:
            from app.media.recognition.episode_resolver import resolve_episode
            episode = resolve_episode(prepared.clean_title, secondary_title=prepared.clean_secondary_title)
        rank_budget = budget.stage("rank", rec["timeouts"].get("rank_ms"))
        rank_budget.check()
        ranked = CandidateRanker(settings).rank(searches.candidates, lookup, episode=episode,
            coverage=searches.coverage, conflicts=plan.conflicts, budget=rank_budget)
        if not searches.candidates and searches.errors:
            from app.media.recognition.contracts import RecognitionStatus
            codes = sorted({error["error"] for error in searches.errors})
            timed_out = next((code for code in codes if "timeout" in code or code.endswith("deadline_exceeded")), None)
            ranked = replace(ranked, status=RecognitionStatus.TIMEOUT if timed_out else RecognitionStatus.FAILED,
                             reason=timed_out or codes[0])
        rank_budget.check()
        return {"parsers": parsers, "lookup_parses": lookup, "plan": plan, "search": searches, "ranking": ranked}



recognition_service = RecognitionService()
