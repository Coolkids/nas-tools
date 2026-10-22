"""每个文件对应一个请求和一条 Trace；只有经过验证的结果才能进入业务层。"""

from contextlib import nullcontext, ExitStack
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
import time
import uuid

from app.media.recognition.adapters import meta_from_normalized, meta_from_outcome, _apply_episode_context
from app.media.recognition.budget import RecognitionBudget, BudgetExceeded, RecognitionCancelled, effective_deadline_reason, total_timeout_ms
from app.media.recognition.cache_repository import CacheRepository, cache_hit_scope
from app.media.recognition.configuration import migrate_recognition_v3
from app.media.recognition.contracts import IdentityEntry, NormalizedParse, RecognitionOutcome, RecognitionStatus
from app.media.recognition.episode_resolver import resolve_episode, default_episode_season, merge_episode_context
from app.media.recognition.identity import IdentityService, IdentityScope, OverrideRepository, OverrideConflict, detail_signature, media_kind, scope_for
from app.media.recognition.preprocessor import PREPROCESSOR_VERSION, snapshot_rules
from app.media.recognition.release_year import explicit_release_year, release_year
from app.media.recognition.repository import RecognitionRepository, utc_now
from app.media.recognition.runtime import WorkItem, ResourceBusy, get_runtime
from app.media.recognition.settings import profile_network_allowed, profile_tmdb_allowed
from app.media.recognition.tmdb_client import TMDBClient, TMDBRequestError
from app.media.recognition.trace import Trace
from app.media.recognition.title_names import tmdb_titles


def effective_settings(value=None):
    if value is None:
        from config import Config
        value = Config().get_config()
    # Config 使用可往返的 YAML 标量子类；运行时约定使用 JSON。
    return migrate_recognition_v3(json.loads(json.dumps(value, allow_nan=False)))


@dataclass
class Resolution:
    outcome: RecognitionOutcome
    meta: object | None
    detail: dict | None
    trace: Trace | None
    scope: IdentityScope | None = None


class RecognitionCoordinator:
    def __init__(self, settings=None, *, runtime=None, cache=None, repository=None, rules=None,
                 api_key=None, tmdb_base_url=None, trace_root=None):
        self.settings = effective_settings(settings)
        self.rec = self.settings["recognition"]
        self.runtime = runtime
        self.cache = cache if cache is not None else CacheRepository(settings=self.settings)
        self.repository = repository or RecognitionRepository()
        self.overrides = OverrideRepository(self.repository)
        self.identities = IdentityService(self.cache, self.settings, overrides=self.overrides)
        self.rules, self.api_key, self.tmdb_base_url, self.trace_root = rules, api_key, tmdb_base_url, trace_root

    def _runtime(self):
        return self.runtime or get_runtime()

    def resolve_files(self, files, *, provided=None, mtype=None, season=None, episode_format=None, chinese=True,
                      source="media.get_media_info_on_files"):
        from concurrent.futures import ThreadPoolExecutor
        paths = files if isinstance(files, list) else [files]
        if not paths:
            return {}
        batch_id = uuid.uuid4().hex
        def resolve_file(path):
            episodes = None
            if episode_format is not None:
                try:
                    first, last = episode_format.split_episode(Path(path).name)
                    if first is not None:
                        if type(first) is not int or type(last if last is not None else first) is not int or not 0 <= (last or first)-first < 2000:
                            episodes = (-1,)
                        else:
                            episodes = tuple(range(first, (last or first)+1))
                except Exception:
                    episodes = (-1,)
            result = self.resolve(Path(path).name, file_path=path, provided=provided, mtype=mtype,
                season=season, episodes_override=episodes, chinese=chinese,
                source=source, batch_id=batch_id)
            return path, result.meta if result.outcome.business_identity else None
        workers = max(1, min(self.rec["execution"]["max_running_tasks"], len(paths), 32))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recognition-files") as executor:
            return {path: result for path, result in executor.map(resolve_file, paths, buffersize=workers) if result is not None}

    def _prepare(self, title, secondary, rules, budget):
        stage = budget.stage("preprocess", self.rec["timeouts"].get("preprocess_ms"))
        stage.check()
        left = stage.remaining_seconds()
        work = self._runtime().submit("parser", WorkItem("preprocess", {
            "title": title, "secondary_title": secondary, "rules": asdict(rules)["groups"]}),
            deadline=time.monotonic()+left if left is not None else None,
            cancellation=budget.cancellation, deadline_reason=effective_deadline_reason(stage),
            queue_deadline=(time.monotonic()+self.rec["timeouts"]["queue_ms"]/1000
                            if self.rec["timeouts"].get("queue_ms") is not None else None)).result()
        stage.check()
        if work.status != "success":
            if work.status == "timeout":
                raise BudgetExceeded("preprocess")
            budget.cancellation.check()
            raise ValueError("preprocess_failed")
        return work.value[0]

    @staticmethod
    def _parsed(scope, prepared, episode):
        parsed = NormalizedParse("identity_hint", "1", "local_rules", "success" if scope else "invalid",
            title=scope.canonical_name if scope else None, media_type=scope.media_type if scope else "unknown",
            year=scope.year if scope else None, season=episode.season, episodes=episode.episodes)
        meta = meta_from_normalized(prepared, parsed, episode_context=episode)
        meta.resource_pix = prepared.resolutions[0] if prepared.resolutions else None
        meta.video_encode = prepared.codecs[0] if prepared.codecs else None
        technical_text = prepared.clean_title.replace("_", " ")
        source = re.search(r"(?i)\b(?:WEB[ ._-]?DL|WEBRIP|BLURAY|HDTV|UHDTV|BDRIP|DVDRIP|HDDVD)\b", technical_text)
        meta.resource_type = source[0] if source else None
        audio = re.search(r"(?i)\b(?:AAC|DDP|AC3|DTS(?:HD)?|TRUEHD|ATMOS|FLAC)\b", technical_text)
        meta.audio_encode = audio[0] if audio else None
        effects = re.findall(r"(?i)\b(?:REMUX|UHD|SDR|HDR\d*|DOLBY|DOVI|DV|3D|REPACK)\b", technical_text)
        meta.resource_effect = " ".join(dict.fromkeys(x.upper() for x in effects)) or None
        part = re.search(r"(?i)\b(?:PART|CD|DVD|DISK|DISC)[ .]?(?:\d{1,2}|III|II|IV|[ABCI])\b", technical_text)
        meta.part = re.sub(r"[ .]", "", part[0]) if part else None
        return meta

    def resolve(self, title, *, subtitle=None, mtype=None, name=None, year=None, season=None,
                cache=True, force=False, pre_parsed=None, file_path=None, provided=None,
                episodes_override=None, absolute_mapping=None, source="recognition", batch_id=None,
                cancellation=None, chinese=True, strict=None, append_to_response=None,
                task_trace=None, task_budget=None, bypass_identity_cache=False):
        budget = task_budget or RecognitionBudget(total_timeout_ms(self.rec), cancellation=cancellation)
        request_id = task_trace.request_id if task_trace is not None else uuid.uuid4().hex
        started_at = task_trace.started_at if task_trace is not None else utc_now()
        trace, parsed_meta, scope, detail = task_trace, None, None, None
        result, validated_at = None, None
        meta_validated = False
        state = {}
        state["bypass_identity_cache"] = bypass_identity_cache
        try:
            from app.media.recognition.records import v3_recognition_lease
            with v3_recognition_lease(budget):
                trace = task_trace or Trace({"title": title, "secondary_title": subtitle, "name_override": name,
                    "year_override": year, "season_override": season, "media_type": media_kind(mtype),
                    "batch_id": batch_id, "forced": force}, self.rec, repository=self.repository,
                    source=source, request_id=request_id, root=self.trace_root, budget=budget, started_at=started_at)
                try:
                    with ExitStack() as work_scope:
                        work_scope.enter_context(cache_hit_scope())
                        work_scope.enter_context(self._runtime().foreground(
                            deadline=budget.total_deadline_ns/1e9 if budget.total_deadline_ns is not None else None,
                            cancellation=budget.cancellation))
                        result, parsed_meta, detail, scope, validated_at = self._resolve(
                            title, subtitle=subtitle, mtype=mtype, name=name, year=year, season=season,
                            cache=cache and not force, pre_parsed=pre_parsed, file_path=file_path,
                            provided=provided, episodes_override=episodes_override, absolute_mapping=absolute_mapping,
                            trace=trace, budget=budget, chinese=chinese, strict=strict,
                            append_to_response=append_to_response, work_scope=work_scope, state=state)
                        if result.business_identity:
                            try:
                                parsed_meta = meta_from_outcome(result, parsed_meta, detail)
                            except Exception:
                                raise ValueError("tmdb_invalid_compatibility_details") from None
                            meta_validated = True
                            budget.check()
                            validated_at = budget.clock_ns()
                            if state.get("identity_hit_key"):
                                try:
                                    self.cache.get("identity", state["identity_hit_key"])
                                except Exception:
                                    pass  # 统计信息丢失不会使已验证的详情失效。
                        result = replace(result, duration_ms=(budget.clock_ns()-budget.started_ns)/1e6)
                        result = trace.finish(result, validated_at_ns=validated_at)
                        publication = state.get("publication")
                        if result.business_identity and publication and budget.failure_reason is None:
                            identity = replace(result.result, source_trace_id=trace.source_trace_id)
                            try:
                                self.identities.put(publication["key"], identity, scope, publication["versions"],
                                    origin=publication["origin"], language=publication["language"], lease=publication["lease"],
                                    verification=publication["verification"])
                            except Exception:
                                # 缓存丢失不会回滚已验证的业务结果。
                                pass
                except (BudgetExceeded, RecognitionCancelled, TMDBRequestError, ResourceBusy, ValueError) as error:
                    result = self._failure(request_id, error)
                except Exception as error:
                    trace.emit("request", "internal_error", "error", {"error_type": type(error).__name__})
                    result = RecognitionOutcome(request_id, RecognitionStatus.FAILED, failure_code="recognition_internal_error")
                if trace.finished_at is None:
                    result = replace(result, duration_ms=(budget.clock_ns()-budget.started_ns)/1e6)
                    result = trace.finish(result, validated_at_ns=validated_at)
        except (BudgetExceeded, RecognitionCancelled) as error:
            result = self._failure(request_id, error)
        if result is None:
            result = RecognitionOutcome(request_id, RecognitionStatus.FAILED, failure_code="recognition_unavailable")
        parsed_meta = parsed_meta or state.get("meta")
        scope = scope or state.get("scope")
        if parsed_meta is not None:
            if not meta_validated or not result.business_identity:
                parsed_meta = meta_from_outcome(result, parsed_meta, detail)
            parsed_meta.recognition_identity_scope = asdict(scope) if scope is not None else None
        return Resolution(result, parsed_meta, detail if result.business_identity else None, trace, scope)

    @staticmethod
    def _failure(request_id, error):
        review = str(error) in {"absolute_mapping_required", "invalid_episode_range", "season_not_found", "season_unverified", "episode_not_found", "episode_unverified", "movie_episode_conflict"}
        status = RecognitionStatus.REVIEW_REQUIRED if review else RecognitionStatus.TIMEOUT if isinstance(error, BudgetExceeded) or "timeout" in str(error) or str(error).endswith("deadline_exceeded") else (
            RecognitionStatus.CANCELLED if isinstance(error, RecognitionCancelled) else RecognitionStatus.FAILED)
        code = str(error) if isinstance(error, (BudgetExceeded, RecognitionCancelled, TMDBRequestError, ValueError)) else "recognition_busy"
        return RecognitionOutcome(request_id, status, failure_code=code)

    def _resolve(self, title, *, subtitle, mtype, name, year, season, cache, pre_parsed, file_path,
                 provided, episodes_override, absolute_mapping, trace, budget, chinese, strict, append_to_response,
                 work_scope, state):
        def outcome(status, reason=None, identity=None, source=None):
            return RecognitionOutcome(trace.request_id, status, identity, reason,
                episode.season, episode.episodes, identity.score if identity else None, cache_source=source)
        def adopt_episode(context):
            nonlocal episode
            if context is not None and context != episode:
                trace.emit("episode", "episode_numbering_resolved", "review" if context.review_reason else "success",
                    {"input": asdict(episode), "output": asdict(context), "source": context.source})
                episode = context
                if state.get("meta") is not None:
                    _apply_episode_context(state["meta"], episode.season, episode.episodes)
                    state["meta"].recognition_review_reason = episode.review_reason
        def adopt_release_group(parses):
            # 自定义规则已在适配时优先匹配；没有命中时再使用当前文件的解析结果。
            meta = state.get("meta")
            if meta is not None and not meta.resource_team:
                meta.resource_team = next((parsed.release_metadata["release_group"] for parsed in parses
                    if parsed.status == "success" and isinstance(parsed.release_metadata, dict)
                    and parsed.release_metadata.get("release_group")), None)
        if not title or not isinstance(title, str):
            return RecognitionOutcome(trace.request_id, RecognitionStatus.FAILED, failure_code="empty_title"), None, None, None, None
        if file_path is not None:
            from app.utils import PathUtils
            if not Path(file_path).exists():
                return RecognitionOutcome(trace.request_id, RecognitionStatus.SKIPPED, failure_code="file_not_found"), None, None, None, None
            if not Path(file_path).is_dir() and PathUtils.get_bluray_dir(str(file_path)):
                return RecognitionOutcome(trace.request_id, RecognitionStatus.SKIPPED, failure_code="bluray_directory_child"), None, None, None, None
        rules = self.rules
        if rules is None:
            from app.helper import WordsHelper
            rules = snapshot_rules(WordsHelper())
        prepared = getattr(pre_parsed, "_recognition_preprocess", None)
        trace.emit("preprocess", "preprocess_started", "running", {"input": {"title": title, "secondary_title": subtitle}})
        if (prepared is None or prepared.raw_title != title or prepared.raw_secondary_title != subtitle or
                prepared.rule_version != rules.version or prepared.preprocessor_version != PREPROCESSOR_VERSION):
            prepared = self._prepare(title, subtitle, rules, budget)
        for match in prepared.rule_matches:
            trace.emit("preprocess", "word_rule_matched", "success", asdict(match))
        trace.emit("preprocess", "preprocess_completed", "success", {
            "input": {"title": prepared.raw_title, "secondary_title": prepared.raw_secondary_title},
            "output": {"title": prepared.clean_title, "secondary_title": prepared.clean_secondary_title},
            "rule_version": prepared.rule_version, "matched_rule_count": len(prepared.rule_matches)})
        parent = Path(file_path).parent.name if file_path is not None else None
        episode = resolve_episode(prepared.clean_title, secondary_title=prepared.clean_secondary_title,
            parent_title=parent, season_override=season, episodes_override=episodes_override, absolute_mapping=absolute_mapping)
        episode_input = episode
        file_parses = None
        lookup = prepared
        scope = scope_for(lookup, name=name, mtype=mtype, year=year)
        if provided and scope is None:
            provided_name = provided.get("name") or provided.get("title") or f"tmdb {media_kind(provided.get('media_type') or mtype)} {provided.get('id')}"
            scope = scope_for(lookup, name=str(provided_name), mtype=provided.get("media_type") or mtype, year=year)
        if file_path is not None:
            for parent_name in (Path(file_path).parent.name, Path(file_path).parent.parent.name):
                if scope is not None and scope.year is not None:
                    break
                if not parent_name or re.fullmatch(r"(?i)(?:season\s*\d+|s\d+|第\s*\d+\s*季)", parent_name):
                    continue
                parent_prepared = self._prepare(parent_name, None, rules, budget)
                if scope is None:
                    lookup = parent_prepared
                    scope = scope_for(lookup, name=name, mtype=mtype or ("tv" if episode.episodes else None), year=year)
                    trace.emit("fallback", "parent_title_used", "success", {"title": parent_name})
                elif (parent_prepared.year_hint is not None and (scope.media_type != "tv" or
                        explicit_release_year(parent_prepared.year_hint, (parent_prepared.clean_title,)))):
                    scope = replace(scope, year=parent_prepared.year_hint)
        if scope is not None and episode.episodes and scope.media_type == "unknown":
            scope = replace(scope, media_type="tv")
        parsed_meta = self._parsed(scope, prepared, episode)
        state.update(meta=parsed_meta, scope=scope)
        trace.emit("episode", "file_episode_resolved", "review" if episode.review_reason else "success",
            {"output": asdict(episode), "source": episode.source})
        if scope is not None:
            trace.input["scope"] = asdict(scope)
        if pre_parsed is not None:
            for field in ("resource_type", "resource_pix", "resource_effect", "resource_team", "video_encode", "audio_encode", "part"):
                if field == "resource_team" and parsed_meta.resource_team:
                    continue
                if getattr(pre_parsed, field, None) is not None:
                    setattr(parsed_meta, field, deepcopy(getattr(pre_parsed, field)))
        if not profile_tmdb_allowed("resolve", self.rec):
            return outcome(RecognitionStatus.PARSED, "tmdb_disallowed_by_profile"), parsed_meta, None, scope, None
        network = profile_network_allowed("resolve", self.rec)
        if not network:
            return outcome(RecognitionStatus.PARSED, "network_disallowed_by_profile"), parsed_meta, None, scope, None
        client = TMDBClient(self.settings, runtime=self._runtime(), cache=self.cache, budget=budget,
            api_key=self.api_key, base_url=self.tmdb_base_url, language="zh-CN" if chinese else "en-US",
            append_to_response=append_to_response)
        if not client.api_key:
            return outcome(RecognitionStatus.FAILED, "tmdb_unavailable"), parsed_meta, None, scope, None
        language = "zh-CN" if chinese else "en-US"
        if scope is None:
            return outcome(RecognitionStatus.REVIEW_REQUIRED, "identity_scope_unresolved"), parsed_meta, None, None, None
        # 在自动识别身份前，始终先读取已保存的用户选择。
        override = self.overrides.get(scope)
        versions = self.identities.versions(lookup)
        key = self.identities.key(scope)
        trace.input["scope"] = asdict(scope)
        trace.emit("identity", "identity_scope", "success", {"scope": asdict(scope), "cache_title": scope.canonical_name,
            "override_version": override["version"]})
        if provided:
            kind = media_kind(provided.get("media_type") or mtype)
            if kind == "unknown":
                kind = "tv" if provided.get("name") else "movie"
            identity, detail = self._explicit(client, kind, provided.get("id"), scope, versions, override["version"], cache, language)
            trace.emit("identity", "provided_tmdb_verified", "success", {"media_type": kind, "tmdb_id": identity.tmdb_id})
            source = "provided"
        elif override["choice"] is not None:
            choice = override["choice"]
            identity, detail = self._explicit(client, choice["media_type"], choice["tmdb_id"], scope,
                versions, override["version"], cache, language)
            source = "override"
        else:
            lease_context = self.identities.singleflight(key, budget) if cache else nullcontext(None)
            lease = work_scope.enter_context(lease_context)
            budget.check()
            current = self.overrides.get(scope)
            if current["version"] != override["version"]:
                if current["choice"] is None:
                    return outcome(RecognitionStatus.REVIEW_REQUIRED, "override_version_changed"), parsed_meta, None, scope, None
                choice = current["choice"]
                identity, detail = self._explicit(client, choice["media_type"], choice["tmdb_id"], scope,
                    versions, current["version"], cache, language)
                override, source = current, "override"
            else:
                identity = self.identities.get(key, scope, override["version"], hit=False) if cache and not state.get("bypass_identity_cache") else None
                identity_miss_reason = "continue_parsing"
                if identity is not None:
                    attempts_before = client.http_attempt_count
                    detail = client.detail(identity.media_type, identity.tmdb_id, language, cache=cache)
                    trace.emit("identity", "identity_detail_loaded", "success", {
                        "tmdb_id": identity.tmdb_id, "media_type": identity.media_type,
                        "cache_source": "tmdb" if client.http_attempt_count > attempts_before else "cache"})
                    if identity.media_type == "tv":
                        adopt_episode(default_episode_season(episode_input))
                    if self.rec["matching"]["adult_policy"] == "exclude" and detail.get("adult") is True:
                        trace.emit("identity", "identity_rejected", "review", {"reason": "adult_excluded"})
                        return outcome(RecognitionStatus.REVIEW_REQUIRED, "adult_excluded"), parsed_meta, None, scope, None
                    # 根据当前 TMDB 数据校验同名作品的年份，
                    # 而不是按首个文件的年份拆分标题键。
                    date = detail.get("release_date" if identity.media_type == "movie" else "first_air_date")
                    if identity.media_type == "tv" and episode.season is not None and episode.season > 1:
                        seasons = detail.get("seasons")
                        row = next((item for item in seasons if isinstance(item, dict) and item.get("season_number") == episode.season), None) if isinstance(seasons, list) else None
                        date = row.get("air_date") if row else None
                    date = date if isinstance(date, str) else ""
                    checked_year = release_year(scope.year, titles=tmdb_titles(detail))
                    if checked_year is not None and date[:4].isdecimal() and date[:4] != str(checked_year):
                        identity_miss_reason = "year_conflict"
                        identity = None
                    else:
                        identity = replace(identity, title=detail.get("title") or detail.get("name"),
                            year=scope.year, ambiguity_group=scope.key, override_version=override["version"])
                if identity is not None:
                    trace.emit("identity", "identity_hit", "success", {"tmdb_id": identity.tmdb_id, "media_type": identity.media_type,
                        "cache_source": "identity", "override_version": override["version"]})
                    for node in ("anitopy", "anitopy_ml", "openai", "query_plan", "tmdb_search", "rank"):
                        trace.emit(node, "node_skipped", "skipped", {"reason": "verified_identity_hit"})
                    source = "identity"
                else:
                    from app.media.recognition.service import recognition_service
                    adopt_episode(episode_input)
                    trace.emit("identity", "identity_miss", "success", {
                        "cache_source": "bypassed" if not cache or state.get("bypass_identity_cache") else "miss",
                        "reason": identity_miss_reason})
                    previous_identity = self.cache.get("identity", key, hit=False) if cache else None
                    revalidate = previous_identity is not None and previous_identity.needs_revalidation
                    overrides = {"name": name} if name is not None else {}
                    if scope.year is not None:
                        overrides["year"] = scope.year
                    if media_kind(mtype) != "unknown":
                        overrides["media_type"] = media_kind(mtype)
                    def emit(event, **payload):
                        node = payload.pop("node_id", payload.get("provider_id", "parser"))
                        trace.emit(node, event, payload.get("status", "success"), payload)
                    analysis = recognition_service.analyze_v3(lookup, self.settings, runtime=self._runtime(), budget=budget,
                        network_allowed=True, cache=cache and not revalidate, episode=episode_input, lookup_overrides=overrides, client=client, emit=emit,
                        relax_year=not strict and str(self.settings.get("app", {}).get("rmt_match_mode") or "normal").lower() == "normal")
                    ranked = analysis["ranking"]
                    if lookup is prepared:
                        file_parses = analysis["parsers"].results
                        adopt_release_group(file_parses)
                    episode_input = analysis["episode"]
                    adopt_episode(episode_input)
                    if ranked.status == RecognitionStatus.MATCHED:
                        adopt_episode(ranked.episode)
                    trace.candidates = [{"media_type": c.media_type, "tmdb_id": c.tmdb_id, "title": c.title[:512],
                        "rank": c.rank, "score": c.score, "gap": c.gap, "scores": c.scores,
                        "available_weights": c.available_weights, "hard_reject": c.hard_reject,
                        "title_checks": c.search_fields.get("_title_checks", []),
                        "episode_context": c.search_fields.get("_episode_context"),
                        "query_ids": c.search_fields.get("_queries", []),
                        "providers": sorted({item["provider_id"] for item in c.search_fields.get("_sources", [])})}
                        for c in ranked.candidates]
                    for parsed in analysis["parsers"].results:
                        trace.emit(parsed.provider_id, "parser_result", parsed.status, asdict(parsed))
                    planned = {item.query_id: item for item in analysis["plan"].items}
                    for page in analysis["search"].responses:
                        raw_rows = page.raw_json["results"]
                        results = [{"media_type": row.get("media_type", planned[page.query_id].endpoint_type), "id": row.get("id"),
                            "title": str(row.get("title") or row.get("name") or "")[:128],
                            "original_title": str(row.get("original_title") or row.get("original_name") or "")[:128]}
                            for row in raw_rows[:40]]
                        trace.emit("tmdb_search", "search_page", "success", {"query_id": page.query_id, "page": page.page,
                            "total_pages": page.total_pages, "total_results": page.total_results,
                            "returned_count": len(raw_rows), "consumed_count": len(page.consumed_results if page.consumed_results is not None else raw_rows),
                            "results": results, "results_truncated": len(raw_rows) > len(results),
                            "cache_source": page.cache_source, "request_params": page.request_params})
                    for error in analysis["search"].errors:
                        item = planned.get(error.get("query_id"))
                        trace.emit("tmdb_search", "search_failed", "error", {**error, "query": item.query if item else None})
                    snapshots = [{**candidate, "title": candidate["title"][:128], "title_checks": [
                        {**check, "title": check["title"][:128], "matched_title": check["matched_title"][:128],
                            "primary_title": (check.get("primary_title") or "")[:128]}
                        for check in candidate["title_checks"]]} for candidate in trace.candidates[:5]]
                    conflict_details = [{key: value[:128] if key in {"title", "primary_title", "matched_title"} and isinstance(value, str)
                        else value for key, value in item.items()} for item in ranked.conflict_details]
                    trace.emit("rank", "rank_completed", ranked.status.value, {"reason": ranked.reason,
                        "coverage": ranked.coverage, "queries": analysis["search"].logical_query_count,
                        "coverage_issues": analysis["search"].coverage_issues,
                        "discarded_queries": analysis["search"].discarded_queries,
                        "detail_complete": analysis["search"].detail_complete,
                        "http_attempts": analysis["search"].http_attempt_count, "episode": asdict(episode),
                        "conflicts": conflict_details, "candidate_count": len(trace.candidates),
                        "thresholds": {key: self.rec["matching"][key] for key in ("min_name_similarity", "match_threshold", "min_score_gap")},
                        "candidates": snapshots})
                    best_parse = next(iter(analysis["lookup_parses"]), None)
                    if best_parse is not None and best_parse.status == "success":
                        candidate_meta = meta_from_normalized(prepared, best_parse, episode_context=episode)
                        for field in ("cn_name", "en_name", "alternative_names", "year"):
                            setattr(parsed_meta, field, getattr(candidate_meta, field))
                    if ranked.status != RecognitionStatus.MATCHED:
                        return outcome(ranked.status, ranked.reason), parsed_meta, None, scope, None
                    selected = ranked.selected
                    detail = selected.detail_fields
                    identity = IdentityEntry(selected.media_type, selected.tmdb_id, selected.title,
                        versions["rule"], versions["parser"], versions["matching"], "verified", year=scope.year,
                        normalized_titles=(scope.canonical_name,), ambiguity_group=scope.key, score=selected.score,
                        source_trace_id=trace.request_id if trace.mode == "all" else None, override_version=override["version"])
                    source = "resolved"
                    budget.check()
                    self._check_episode(detail, episode, identity.media_type)
                    if cache:
                        state["publication"] = {"key": key, "versions": versions,
                            "origin": client.base_url, "language": language, "lease": lease,
                            "verification": {"entity": [identity.media_type, identity.tmdb_id],
                                "name_similarity": selected.scores["name_similarity"], "score": selected.score,
                                "gap": selected.gap, "detail_verified": True, "hard_reject": []}}
                        state["publication"]["verification"]["detail_signature"] = detail_signature(detail)
        # 完成网络或缓存操作后重新读取用户选择。较晚返回的自动结果
        # 不能覆盖人工选择，即使缓存失效处理有所延迟。
        current = self.overrides.get(scope)
        if provided is None and current["version"] != identity.override_version:
            if current["choice"] is None:
                return outcome(RecognitionStatus.REVIEW_REQUIRED, "override_version_changed"), parsed_meta, None, scope, None
            choice = current["choice"]
            identity, detail = self._explicit(client, choice["media_type"], choice["tmdb_id"], scope,
                versions, current["version"], cache, language)
            source = "override"
            state.pop("publication", None)
        if identity.media_type == "tv":
            # 身份缓存保存作品信息，不保存当前文件的集数坐标。缓存或
            # 已提供 ID 的路径仍需在本地提取一次集数信息；
            # 若已完整解析当前文件，则直接复用已有结果。
            protected = {"override", "absolute_mapping"}
            if episode_input.season_source not in protected or episode_input.episodes_source not in protected:
                reused = file_parses is not None
                if file_parses is None:
                    from app.media.recognition.parser_manager import ParserManager
                    trace.emit("episode", "file_anitopy_started", "running", {
                        "input": {"title": prepared.clean_title}, "provider_id": "anitopy"})
                    file_parses = ParserManager(self.settings, runtime=self._runtime(), budget=budget).run(
                        prepared, network_allowed=False, cache=False, provider_ids=("anitopy",)).results
                adopt_episode(merge_episode_context(episode_input, file_parses))
                local = next((item for item in file_parses if item.provider_id == "anitopy"), None)
                trace.emit("episode", "file_anitopy_completed", local.status if local else "unavailable", {
                    "provider_id": "anitopy", "reused": reused,
                    "cache_source": local.cache_source if local else None,
                    "error": local.error if local else "provider_unavailable",
                    "input": {"title": prepared.clean_title},
                    "extraction": {"season": local.season, "episodes": local.episodes} if local else None,
                    "output": asdict(episode)})
            else:
                adopt_episode(default_episode_season(episode_input))
        if not parsed_meta.resource_team and file_parses is None:
            # 电影缓存命中或手动指定季集时也只从当前文件恢复制作组，不继承其他文件的组名。
            from app.media.recognition.parser_manager import ParserManager
            file_parses = ParserManager(self.settings, runtime=self._runtime(), budget=budget).run(
                prepared, network_allowed=False, cache=cache, provider_ids=("anitopy",)).results
        adopt_release_group(file_parses or ())
        try:
            self._check_episode(detail, episode, identity.media_type,
                trust_tv_coordinates=provided is not None)
        except ValueError as error:
            trace.emit("episode", "episode_validation_failed", "review",
                {"output": asdict(episode), "reason": str(error), "source": episode.source})
            raise
        trace.emit("episode", "episode_coordinates_verified", "success",
            {"output": asdict(episode), "source": episode.source})
        if (provided is None and self.rec["query"].get("episode_details_enabled")
                and identity.media_type == "tv" and episode.episodes):
            from concurrent.futures import ThreadPoolExecutor
            stage = budget.stage("episode", self.rec["timeouts"].get("episode_ms"))
            def load_episode(number):
                return number, client.episode(identity.tmdb_id, episode.season, number, language, cache=cache, budget=stage)
            with ThreadPoolExecutor(max_workers=min(len(episode.episodes), self.rec["query"]["tmdb_concurrency"]),
                                    thread_name_prefix="recognition-episodes") as executor:
                episodes = dict(executor.map(load_episode, episode.episodes,
                    buffersize=self.rec["query"]["tmdb_concurrency"]))
            parsed_meta.recognition_episode_details = episodes
            trace.emit("episode", "episode_details_completed", "success", {"episodes": list(episodes)})
        budget.check()
        validated_at = budget.clock_ns()
        if source == "identity":
            state["identity_hit_key"] = key
        trace.emit("identity_result", "identity_verified", "success", {"media_type": identity.media_type, "tmdb_id": identity.tmdb_id})
        return outcome(RecognitionStatus.MATCHED, identity=identity, source=source), parsed_meta, detail, scope, validated_at

    @staticmethod
    def _check_episode(detail, episode, kind, trust_tv_coordinates=False):
        if episode.review_reason:
            raise ValueError(episode.review_reason)
        if kind == "movie" and (episode.season is not None or episode.episodes):
            raise ValueError("movie_episode_conflict")
        if kind == "tv" and (episode.season is not None or episode.episodes):
            # 手动提供 TMDB 身份属于明确的用户选择。
            # 即使 TMDB 缺少季信息（动漫中很常见），也保留文件中的
            # 季和集坐标；自动识别仍走正常的
            # 校验流程。
            if trust_tv_coordinates:
                return
            rows = detail.get("seasons")
            row = next((item for item in rows if isinstance(item, dict) and item.get("season_number") == episode.season), None) if isinstance(rows, list) else None
            if row is None:
                raise ValueError("season_not_found" if isinstance(rows, list) else "season_unverified")
            count = row.get("episode_count")
            if episode.episodes and (type(count) is not int or max(episode.episodes) > count):
                raise ValueError("episode_not_found" if type(count) is int else "episode_unverified")

    @staticmethod
    def _explicit(client, kind, tmdb_id, scope, versions, override_version, cache, language):
        detail = client.detail(kind, tmdb_id, language, cache=cache)
        identity = IdentityEntry(kind, tmdb_id, detail.get("title") or detail.get("name"), versions["rule"],
            versions["parser"], versions["matching"], "verified", year=scope.year,
            normalized_titles=(scope.canonical_name,), ambiguity_group=scope.key, override_version=override_version)
        return identity, detail

    def confirm(self, scope, *, media_type, tmdb_id, expected_version, actor, request_id=None, clear=False, budget=None, task_fence=None):
        if type(clear) is not bool or type(expected_version) is not int or expected_version < 0:
            raise ValueError("invalid_override_update")
        if isinstance(scope, dict):
            scope = IdentityScope(**scope)
        if not isinstance(scope, IdentityScope):
            raise ValueError("invalid_identity_scope")
        current = self.overrides.get(scope)
        if current["version"] != expected_version:
            raise OverrideConflict("VALIDATION_CONFLICT")
        choice = None
        if not clear:
            budget = budget or RecognitionBudget(total_timeout_ms(self.rec))
            client = TMDBClient(self.settings, runtime=self._runtime(), cache=self.cache, budget=budget,
                api_key=self.api_key, base_url=self.tmdb_base_url)
            detail = client.detail(media_kind(media_type), tmdb_id, cache=False)
            if scope.media_type != "unknown" and media_kind(media_type) != scope.media_type:
                raise ValueError("override_type_conflict")
            choice = asdict(IdentityEntry(media_kind(media_type), tmdb_id, detail.get("title") or detail.get("name"),
                "manual", "manual", "manual", "verified", year=scope.year, override_version=expected_version+1))
            budget.check()
        if budget is not None:
            budget.check()
        return self.overrides.save(scope, choice, expected_version=expected_version, actor=actor, request_id=request_id, task_fence=task_fence)
