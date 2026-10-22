"""Bounded query/page/detail orchestration; only worker processes perform I/O."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from app.media.recognition.budget import BudgetExceeded, RecognitionCancelled
from app.media.recognition.candidate_merger import merge_candidates
from app.media.recognition.ranker import official_titles, title_match
from app.media.recognition.title_names import title_variants
from app.media.recognition.tmdb_client import TMDBRequestError


@dataclass(frozen=True)
class SearchBatch:
    responses: tuple
    candidates: tuple
    coverage: str
    errors: tuple[dict, ...]
    logical_query_count: int
    http_attempt_count: int
    detail_complete: bool


class TMDBSearcher:
    def __init__(self, client):
        self.client, self.settings = client, client.settings

    def run(self, plan, parses, *, cache=True, budget=None):
        query = self.settings["query"]
        stage = budget or self.client.budget.stage("tmdb_stage", self.settings["timeouts"].get("tmdb_stage_ms"))
        responses, errors, partial = [], [], bool(plan.skipped)
        initial_attempts = self.client.http_attempt_count
        def consume(item):
            rows, consumed, complete, failures = [], 0, True, []
            for page in range(1, query["max_pages_per_query"]+1):
                try:
                    stage.check()
                    response = self.client.search(item, page, cache=cache, budget=stage)
                except (TMDBRequestError, BudgetExceeded, RecognitionCancelled) as error:
                    failures.append({"query_id": item.query_id, "page": page, "error": str(error)})
                    complete = False
                    break
                take = min(query["candidates_per_query"]-consumed, len(response.raw_json["results"]))
                response.consumed_results = response.raw_json["results"][:take]
                consumed += take
                rows.append(response)
                if take < len(response.raw_json["results"]):
                    complete = False
                if page >= response.total_pages:
                    break
                if consumed >= query["candidates_per_query"]:
                    complete = False
                    break
            if rows and rows[-1].page < rows[-1].total_pages:
                complete = False
            return rows, complete, failures
        workers = max(1, min(query["tmdb_concurrency"], len(plan.items)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recognition-query") as executor:
            futures = {executor.submit(consume, item): item for item in plan.items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    rows, complete, failures = future.result()
                    responses.extend(rows)
                    errors.extend(failures)
                    partial = partial or not complete
                except (TMDBRequestError, BudgetExceeded, RecognitionCancelled) as error:
                    errors.append({"query_id": item.query_id, "error": str(error)})
                    partial = True
        responses.sort(key=lambda value: (value.query_id, value.page))
        candidates = merge_candidates(responses, plan)
        titles = [name for p in parses if p.status == "success" for value in (p.title, p.secondary_title, *p.aliases) for name in title_variants(value)]
        def priority(c):
            return (-max((title_match(a, b)[0] for a in titles for b in official_titles(c)), default=0), c.media_type, c.tmdb_id)
        selected = sorted((c for c in candidates if not (self.settings["matching"]["adult_policy"] == "exclude" and c.search_fields.get("adult") is True)),
                          key=priority)[:query["detail_top_k"]]
        detail_complete = True
        with ThreadPoolExecutor(max_workers=max(1, min(query["tmdb_concurrency"], len(selected))), thread_name_prefix="recognition-details") as executor:
            futures = {executor.submit(self.client.detail, c.media_type, c.tmdb_id, cache=cache, budget=stage): c for c in selected}
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    candidate.detail_fields = {**future.result(), "_verified": True}
                    candidate.title = candidate.detail_fields["title" if candidate.media_type == "movie" else "name"]
                except (TMDBRequestError, BudgetExceeded, RecognitionCancelled) as error:
                    candidate.detail_fields = {"_error": str(error)}
                    errors.append({"media_type": candidate.media_type, "id": candidate.tmdb_id, "error": str(error)})
                    detail_complete = False
        return SearchBatch(tuple(responses), tuple(candidates), "partial" if partial or not detail_complete else "complete",
            tuple(sorted(errors, key=lambda value: str(sorted(value.items())))), len(plan.items),
            self.client.http_attempt_count-initial_attempts, detail_complete)
