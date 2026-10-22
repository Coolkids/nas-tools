import copy
import os.path
import sys
import time
import unicodedata
from dataclasses import asdict
import regex as re

import log
from app.helper import WordsHelper
from app.media.meta.metaanime import MetaAnime
from app.media.meta.metavideo import MetaVideo
from app.media.recognition.records import (
    current_recorder, recognition_scope, recognition_config,
    initialize_recognition_budget, recognition_remaining_seconds,
)
from app.media.recognition.settings import profile_network_allowed
from app.utils.types import MediaType
from app.media.recognition.preprocessor import preprocess_with_details, snapshot_rules
from config import RMT_MEDIAEXT


def prepare_media_title(title, subtitle=None):
    """统一应用自定义识别词，供本地解析和外部解析器共用。"""
    if title is None:
        return title, subtitle, {"ignored": [], "replaced": [], "offset": []}
    rules = snapshot_rules(WordsHelper())
    from app.media.recognition.runtime import supervised_enabled, get_runtime, WorkItem
    if supervised_enabled():
        from app.media.recognition.records import recognition_stage
        with recognition_stage("preprocess"):
            remaining = recognition_remaining_seconds()
            outcome = get_runtime().submit("parser", WorkItem("preprocess", {
                "title": title, "secondary_title": subtitle, "rules": asdict(rules)["groups"],
                "normalize": False}),
                deadline=time.monotonic()+remaining if remaining is not None else None,
                cancellation=current_recorder().cancellation).result()
            if outcome.status != "success":
                raise TimeoutError(outcome.reason)
            prepared, used_info, msg = outcome.value
    else:
        prepared, used_info, msg = preprocess_with_details(title, subtitle, rules=rules, normalize=False)
    if msg:
        for msg_item in msg:
            log.warn("【Meta】%s" % msg_item)
    return prepared.clean_title, prepared.clean_secondary_title, used_info


def _parse_meta_info(title, subtitle=None, mtype=None, apply_custom_words=True):
    """
    媒体整理入口，根据名称和副标题，判断是哪种类型的识别，返回对应对象
    :param title: 标题、种子名、文件名
    :param subtitle: 副标题、描述
    :param mtype: 指定识别类型，为空则自动识别类型
    :return: MetaAnime、MetaVideo
    """

    if apply_custom_words:
        recorder = current_recorder()
        input_title = title
        input_subtitle = subtitle
        title, subtitle, used_info = prepare_media_title(title, subtitle)
        if recorder is not None:
            recorder.action("preprocess", input={"title": input_title, "subtitle": input_subtitle},
                            output={"title": title, "subtitle": subtitle, "rules": used_info})
    else:
        used_info = {"ignored": [], "replaced": [], "offset": []}

    # 判断是否处理文件
    if title and os.path.splitext(title)[-1] in RMT_MEDIAEXT:
        fileflag = True
    else:
        fileflag = False

    if mtype == MediaType.ANIME or is_anime(title):
        meta_info = MetaAnime(title, subtitle, fileflag)
    else:
        meta_info = MetaVideo(title, subtitle, fileflag)

    meta_info.ignored_words = used_info.get("ignored")
    meta_info.replaced_words = used_info.get("replaced")
    meta_info.offset_words = used_info.get("offset")

    return meta_info


def _meta_snapshot(meta_info):
    if not meta_info:
        return None
    return {
        "type": getattr(getattr(meta_info, "type", None), "value", getattr(meta_info, "type", None)),
        "name": meta_info.get_name(),
        "cn_name": meta_info.cn_name,
        "en_name": meta_info.en_name,
        "alternative_names": getattr(meta_info, "alternative_names", []),
        "year": meta_info.year,
        "season": meta_info.get_season_string(),
        "episode": meta_info.get_episode_string(),
        "resource_type": meta_info.resource_type,
        "resource_pix": meta_info.resource_pix,
        "video_encode": meta_info.video_encode,
        "audio_encode": meta_info.audio_encode,
    }


def _infer_recognition_source(module_name=None):
    """Map direct MetaInfo callers to a stable business-source family."""
    if module_name is None:
        try:
            module_name = sys._getframe(2).f_globals.get("__name__", "")
        except (ValueError, AttributeError):
            module_name = ""
    prefixes = (
        ("app.indexer", "indexer"),
        ("app.rsschecker", "rss"),
        ("app.downloader", "downloader"),
        ("app.subscribe", "subscribe"),
        ("app.media.douban", "douban"),
        ("app.doubansync", "douban_sync"),
        ("app.filetransfer", "file_transfer"),
        ("web", "web"),
        ("app.media.media", "media_internal"),
    )
    for prefix, source in prefixes:
        if module_name == prefix or module_name.startswith(prefix + "."):
            return source
    return "unknown"


def MetaInfo(title, subtitle=None, mtype=None, apply_custom_words=True,
             include_ai=True, ai_skip_reason=None, record=True, pre_parsed=None):
    """Parse a title through the discoverable local recognizer and record it."""
    if not record:
        result = _record_meta_parse(None, title, subtitle, mtype, apply_custom_words,
                                    include_ai=include_ai, ai_skip_reason=ai_skip_reason,
                                    pre_parsed=pre_parsed)
        if result is not None:
            result._recognition_original_title = title
            result._recognition_original_subtitle = subtitle
            result._recognition_processed_title = getattr(
                result, "_recognition_processed_title", title)
            result._recognition_processed_subtitle = getattr(
                result, "_recognition_processed_subtitle", subtitle)
            result._recognition_used_info = getattr(
                result, "_recognition_used_info", {"ignored": [], "replaced": [], "offset": []})
        return result
    recorder = current_recorder()
    if recorder is not None:
        result = _record_meta_parse(recorder, title, subtitle, mtype, apply_custom_words,
                                    include_ai=include_ai, ai_skip_reason=ai_skip_reason,
                                    pre_parsed=pre_parsed)
        if result is not None:
            result.recognition_request_id = recorder.request_id
        return result

    caller_module = sys._getframe(1).f_globals.get("__name__", "")
    source = _infer_recognition_source(caller_module)
    with recognition_scope(title, source=source, stage="parse_only",
                           context={"caller_module": caller_module} if caller_module else None) as recorder:
        result = _record_meta_parse(recorder, title, subtitle, mtype, apply_custom_words,
                                    include_ai=include_ai, ai_skip_reason=ai_skip_reason,
                                    pre_parsed=pre_parsed)
        if result is not None:
            result.recognition_request_id = recorder.request_id
        selected_provider = recorder.context.get("selected_provider", "anitopy")
        parse_conflict = recorder.context.get("decision_reason") == "parse_conflict"
        recorder.set_overall(
            status="success" if result and result.get_name() and not parse_conflict else "failed",
            reason=("parse_conflict" if parse_conflict else
                    None if result and result.get_name() else "no_name_parsed"),
            parsed_result=_meta_snapshot(result),
            selected_provider=selected_provider,
        )
        return result


def _merge_parse_results(results):
    """兼容返回对象只补齐无冲突字段，发生明确冲突时保留冲突并标记未解决。"""
    def title_key(value):
        return re.sub(r"[\s._-]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())

    candidates = [(provider_id, result) for provider_id, result in results
                  if result is not None and result.get_name()]
    if not candidates:
        # 保留只有季集或资源字段的局部解析对象；整体结果仍会标记为无名称失败。
        partial = next(((provider_id, result) for provider_id, result in results
                        if result is not None), ("anitopy", None))
        return partial[1], partial[0]
    selected_provider, selected = next(
        ((provider_id, result) for provider_id, result in candidates
         if provider_id == "anitopy"), candidates[0])
    conflicts = []
    for provider_id, candidate in candidates:
        if candidate is selected:
            continue
        local_name = selected.get_name()
        candidate_name = candidate.get_name()
        selected_aliases = getattr(selected, "alternative_names", []) or []
        candidate_aliases = getattr(candidate, "alternative_names", []) or []
        if isinstance(selected_aliases, str):
            selected_aliases = [selected_aliases]
        if isinstance(candidate_aliases, str):
            candidate_aliases = [candidate_aliases]
        selected_aliases = set(selected_aliases)
        candidate_aliases = set(candidate_aliases)
        equivalent_name = title_key(local_name) == title_key(candidate_name)
        equivalent_name = equivalent_name or any(
            title_key(local_name) == title_key(alias) for alias in candidate_aliases)
        equivalent_name = equivalent_name or any(
            title_key(candidate_name) == title_key(alias) for alias in selected_aliases)
        if not equivalent_name:
            conflicts.append({"field": "name", "provider_id": provider_id,
                              "selected": local_name, "candidate": candidate_name})
        for field in ("year", "type"):
            current = getattr(selected, field, None)
            incoming = getattr(candidate, field, None)
            same_value = current == incoming or (field == "year" and current is not None
                                                  and incoming is not None
                                                  and str(current) == str(incoming))
            if current is not None and incoming is not None and not same_value:
                conflicts.append({"field": field, "provider_id": provider_id,
                                  "selected": str(current), "candidate": str(incoming)})
                continue
            if current is None and incoming is not None:
                setattr(selected, field, incoming)
        # 合并后，根据统一的解析器结果批次和明确的提供方优先级选择季与集。
        for field in ("cn_name", "en_name", "part", "resource_type", "resource_pix",
                      "resource_team", "video_encode", "audio_encode"):
            if not getattr(selected, field, None) and getattr(candidate, field, None):
                setattr(selected, field, getattr(candidate, field))
        selected_names = getattr(selected, "alternative_names", []) or []
        candidate_names = getattr(candidate, "alternative_names", []) or []
        if isinstance(selected_names, str):
            selected_names = [selected_names]
        if isinstance(candidate_names, str):
            candidate_names = [candidate_names]
        selected.alternative_names = list(dict.fromkeys(selected_names + candidate_names))
    recorder = current_recorder()
    if recorder is not None and conflicts:
        recorder.context["decision_reason"] = "parse_conflict"
        recorder.action("parse_conflict", status="failed", output={"conflicts": conflicts})
    return selected, selected_provider


def is_anime(name):
    """
    判断是否为动漫
    :param name: 名称
    :return: 是否动漫
    """
    if not name:
        return False
    if re.search(r'【[+0-9XVPI-]+】\s*【', name, re.IGNORECASE):
        return True
    if re.search(r'\s+-\s+[\dv]{1,4}\s+', name, re.IGNORECASE):
        return True
    if re.search(r"S\d{2}\s*-\s*S\d{2}|S\d{2}|\s+S\d{1,2}|EP?\d{2,4}\s*-\s*EP?\d{2,4}|EP?\d{2,4}|\s+EP?\d{1,4}", name,
                 re.IGNORECASE):
        return False
    if re.search(r'\[[+0-9XVPI-]+]\s*\[', name, re.IGNORECASE):
        return True
    return False


def _record_meta_parse(recorder, title, subtitle, mtype, apply_custom_words,
                       include_ai=True, ai_skip_reason=None, pre_parsed=None):
    """Keep cheap local probes; all enabled external engines use the shared pool."""
    from app.media.recognition.coordinator import effective_settings
    from app.media.recognition.budget import RecognitionBudget
    from app.media.recognition.parser_manager import ParserManager
    from app.media.recognition.adapters import meta_from_normalized, _apply_episode_context
    from app.media.recognition.episode_resolver import resolve_episode, merge_episode_context
    from app.media.recognition.preprocessor import RuleSnapshot

    settings = effective_settings(recognition_config())
    initialize_recognition_budget(recorder, settings["recognition"])
    original_title, original_subtitle = title, subtitle
    reused = pre_parsed is not None and getattr(pre_parsed, "_recognition_original_title", None) == title
    if reused:
        local_meta = copy.copy(pre_parsed)
        title = getattr(pre_parsed, "_recognition_processed_title", title)
        subtitle = getattr(pre_parsed, "_recognition_processed_subtitle", subtitle)
        used_info = getattr(pre_parsed, "_recognition_used_info", {}) or {}
    else:
        if apply_custom_words:
            title, subtitle, used_info = prepare_media_title(title, subtitle)
        else:
            used_info = {"ignored": [], "replaced": [], "offset": []}
        local_meta = _parse_meta_info(title, subtitle, mtype, apply_custom_words=False)
    candidates = [("anitopy", local_meta)]
    episode_context = None
    if include_ai:
        # 标题已经应用过自定义规则，因此解析器内部不能再次应用这些规则。
        prepared, _, _ = preprocess_with_details(title or "", subtitle, rules=RuleSnapshot())
        budget = recorder.budget if recorder is not None else RecognitionBudget(settings["recognition"]["timeouts"]["total_ms"])
        def emit(event, provider_id=None, status=None, **payload):
            if recorder is not None:
                recorder.action(event, provider_id=provider_id, status=status, output=payload)
        batch = ParserManager(settings, budget=budget, emit=emit).run(prepared,
            network_allowed=profile_network_allowed("parse_only", settings["recognition"]))
        episode_context = merge_episode_context(resolve_episode(title, secondary_title=subtitle), batch.results)
        if recorder is not None:
            from app.media.recognition.llm_usage import combine_usage
            recorder.context["llm_usage"] = combine_usage(parsed.llm["usage"] for parsed in batch.results if parsed.llm)
        for parsed in batch.results:
            if recorder is not None:
                recorder.add_provider_result(parsed.provider_id, parsed.status,
                    normalized_result=asdict(parsed), error=parsed.error, elapsed_ms=parsed.duration_ms)
            if parsed.status == "success" and parsed.title:
                meta = meta_from_normalized(prepared, parsed)
                if parsed.provider_id == "anitopy":
                    # 保留文件转移和筛选功能所需的技术字段。
                    for field in ("resource_type", "resource_pix", "resource_effect", "resource_team", "video_encode", "audio_encode", "part"):
                        setattr(meta, field, getattr(local_meta, field, None))
                    candidates[0] = ("anitopy", meta)
                else:
                    candidates.append((parsed.provider_id, meta))
    elif recorder is not None:
        recorder.add_provider_result("anitopy", "success" if local_meta.get_name() else "no_result",
                                     normalized_result=_meta_snapshot(local_meta))
        for provider in ("anitopy_ml", "openai"):
            recorder.add_provider_result(provider, "skipped", error=ai_skip_reason or "local_parse_only")
    selected, selected_provider = _merge_parse_results(candidates)
    if selected is not None and episode_context is not None:
        _apply_episode_context(selected, episode_context.season, episode_context.episodes)
        selected.recognition_review_reason = episode_context.review_reason
        if recorder is not None:
            recorder.action("episode_coordinates_selected", status="success", output=asdict(episode_context))
    if recorder is not None:
        recorder.context["selected_provider"] = selected_provider
    if selected is not None:
        selected._recognition_original_title = original_title
        selected._recognition_original_subtitle = original_subtitle
        selected._recognition_processed_title = title
        selected._recognition_processed_subtitle = subtitle
        selected._recognition_used_info = used_info
    return selected
