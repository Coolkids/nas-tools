import copy
import os.path
import sys
import time
import unicodedata
import regex as re

import log
from app.helper import WordsHelper
from app.media.meta.metaanime import MetaAnime
from app.media.meta.metavideo import MetaVideo
from app.media.recognition import RecognitionRequest, registry
from app.media.recognition.records import current_recorder, recognition_scope, recognition_config
from app.media.recognition.service import recognition_service
from app.media.recognition.settings import (
    profile_allows_provider, profile_network_allowed, profile_tmdb_allowed,
    provider_disabled_reason, provider_enabled, provider_endpoint,
    provider_requires_network,
)
from app.utils.types import MediaType
from config import RMT_MEDIAEXT


def prepare_media_title(title, subtitle=None):
    """统一应用自定义识别词，供本地解析和外部解析器共用。"""
    title, msg, used_info = WordsHelper().process(title)
    if subtitle:
        subtitle, subtitle_msg, subtitle_used_info = WordsHelper().process(subtitle)
        msg.extend(subtitle_msg)
        for key in ("ignored", "replaced", "offset"):
            used_info.setdefault(key, []).extend(subtitle_used_info.get(key, []))
    if msg:
        for msg_item in msg:
            log.warn("【Meta】%s" % msg_item)
    return title, subtitle, used_info


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
        selected_provider = recorder.context.get("selected_provider", "local_rules")
        parse_conflict = recorder.context.get("decision_reason") == "parse_conflict"
        recorder.set_overall(
            status="success" if result and result.get_name() and not parse_conflict else "failed",
            reason=("parse_conflict" if parse_conflict else
                    None if result and result.get_name() else "no_name_parsed"),
            parsed_result=_meta_snapshot(result),
            selected_provider=selected_provider,
        )
        return result


def _enabled(provider_id, settings):
    if provider_id == "local_rules":
        return True
    provider = ((settings.get("providers") or {}).get(provider_id) or {})
    if provider_id == "anitopy_ml":
        return provider_enabled(provider_id, settings, recognition_config("laboratory"))
    return bool(provider.get("enabled", False))


def _provider_options(provider_id, provider_class, settings):
    provider_settings = ((settings.get("providers") or {}).get(provider_id) or {})
    provider_values = {**provider_settings, **(provider_settings.get("options") or {})}
    schema = provider_class.descriptor.config_schema or {}
    options = {key: provider_values[key] for key in schema if key in provider_values}
    if provider_id == "anitopy_ml":
        options.setdefault("endpoint", provider_endpoint(
            provider_id, settings, recognition_config("laboratory")))
    return options, provider_settings


def _merge_parse_results(results):
    """兼容返回对象只补齐无冲突字段，发生明确冲突时保留冲突并标记未解决。"""
    def title_key(value):
        return re.sub(r"[\s._-]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())

    candidates = [(provider_id, result) for provider_id, result in results
                  if result is not None and result.get_name()]
    if not candidates:
        # 保留只有季集或资源字段的局部解析对象；整体结果仍会标记为无名称失败。
        partial = next(((provider_id, result) for provider_id, result in results
                        if result is not None), ("local_rules", None))
        return partial[1], partial[0]
    selected_provider, selected = next(
        ((provider_id, result) for provider_id, result in candidates
         if provider_id == "local_rules"), candidates[0])
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
        for start_field, end_field in (("begin_season", "end_season"),
                                       ("begin_episode", "end_episode")):
            current_range = (getattr(selected, start_field, None),
                             getattr(selected, end_field, None))
            incoming_range = (getattr(candidate, start_field, None),
                              getattr(candidate, end_field, None))
            if all(value is not None for value in current_range + incoming_range) \
                    and current_range != incoming_range:
                conflicts.append({"field": start_field.removeprefix("begin_"),
                                  "provider_id": provider_id,
                                  "selected": list(map(str, current_range)),
                                  "candidate": list(map(str, incoming_range))})
            elif all(value is None for value in current_range) \
                    and any(value is not None for value in incoming_range):
                setattr(selected, start_field, incoming_range[0])
                setattr(selected, end_field, incoming_range[1])
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


def _record_meta_parse(recorder, title, subtitle, mtype, apply_custom_words,
                       include_ai=True, ai_skip_reason=None, pre_parsed=None):
    if recorder is not None and recorder.deadline_monotonic is None:
        execution = (recognition_config("recognition") or {}).get("execution") or {}
        try:
            timeout_seconds = max(float(execution.get("total_timeout_seconds", 30)), 0.001)
        except (TypeError, ValueError):
            timeout_seconds = 30
        recorder.deadline_monotonic = time.monotonic() + timeout_seconds
    original_title, original_subtitle = title, subtitle
    if pre_parsed is not None:
        title = getattr(pre_parsed, "_recognition_processed_title", title)
        subtitle = getattr(pre_parsed, "_recognition_processed_subtitle", subtitle)
        used_info = getattr(pre_parsed, "_recognition_used_info", {}) or {}
    elif apply_custom_words:
        title, subtitle, used_info = prepare_media_title(title, subtitle)
    else:
        used_info = {"ignored": [], "replaced": [], "offset": []}
    if recorder is not None and (apply_custom_words or title != original_title
                                 or subtitle != original_subtitle):
        recorder.action("preprocess", input={"title": original_title, "subtitle": original_subtitle},
                        output={"title": title, "subtitle": subtitle, "rules": used_info})

    settings = recognition_config("recognition") or {}
    stage = recorder.stage if recorder is not None else "parse_only"
    resolve_parse_only = stage == "resolve" and not profile_tmdb_allowed(stage, settings)
    request_context = {"mtype": mtype, "apply_custom_words": False}
    local_allowed = profile_allows_provider(stage, "local_rules", settings)
    provider = (None if pre_parsed is not None or not local_allowed
                else registry.create("local_rules"))
    if not local_allowed:
        local_meta = None
        if recorder is not None:
            recorder.add_provider_result("local_rules", "skipped", input={"title": title},
                                         error="provider_excluded_by_profile")
    elif pre_parsed is not None:
        local_meta = copy.copy(pre_parsed)
        if recorder is not None:
            recorder.add_provider_result(
                "local_rules", "success" if local_meta.get_name() else "no_result",
                input={"title": title, "subtitle": subtitle,
                       "mtype": getattr(mtype, "value", mtype)},
                normalized_result=_meta_snapshot(local_meta))
    elif provider is None:
        local_meta = _parse_meta_info(title, subtitle, mtype, apply_custom_words=False)
        if recorder is not None:
            recorder.add_provider_result("local_rules", "success" if local_meta else "no_result",
                                         input={"title": title, "subtitle": subtitle},
                                         normalized_result=_meta_snapshot(local_meta))
    else:
        try:
            local_result = provider.parse(RecognitionRequest(
                title=title, subtitle=subtitle, context=request_context))
            local_meta = local_result.parsed
            if local_meta is None and local_result.status == "error":
                local_meta = _parse_meta_info(title, subtitle, mtype, apply_custom_words=False)
            if recorder is not None:
                recorder.add_provider_result(
                    local_result.provider_id, local_result.status,
                    input={"title": title, "subtitle": subtitle,
                           "mtype": getattr(mtype, "value", mtype)},
                    raw_result=local_result.raw_result,
                    normalized_result=_meta_snapshot(local_meta),
                    elapsed_ms=local_result.elapsed_ms, error=local_result.error)
        finally:
            registry.release(provider)
    if local_meta:
        local_meta.ignored_words = used_info.get("ignored", [])
        local_meta.replaced_words = used_info.get("replaced", [])
        local_meta.offset_words = used_info.get("offset", [])

    candidates = [("local_rules", local_meta)]
    ai_allowed = include_ai and (stage == "parse_only" or resolve_parse_only)
    for provider_id, provider_class in sorted(registry.discover().items()):
        if provider_id == "local_rules":
            continue
        # Media.resolve owns all non-local attempts for its enclosing request.
        if stage == "resolve" and not resolve_parse_only:
            continue
        if not profile_allows_provider(stage, provider_id, settings):
            if recorder is not None:
                recorder.add_provider_result(provider_id, "skipped",
                                             input={"title": title},
                                             error="provider_excluded_by_profile")
            continue
        if provider_requires_network(provider_class) and not profile_network_allowed(
                stage, settings):
            if recorder is not None:
                recorder.add_provider_result(provider_id, "skipped",
                                             input={"title": title},
                                             error="network_disallowed_by_profile")
            continue
        if not _enabled(provider_id, settings):
            if recorder is not None:
                laboratory = recognition_config("laboratory") or {}
                if provider_id == "anitopy_ml" and not provider_enabled(
                        provider_id, settings, laboratory):
                    reason = provider_disabled_reason(provider_id, settings, laboratory)
                elif provider_id == "anitopy_ml" and not provider_endpoint(
                        provider_id, settings, laboratory):
                    reason = "ai_inference_endpoint_missing"
                else:
                    reason = "provider_disabled"
                recorder.add_provider_result(provider_id, "skipped",
                                             input={"title": title}, error=reason)
            continue
        if not title:
            if recorder is not None:
                recorder.add_provider_result(provider_id, "skipped", input={"title": title},
                                             error="empty_title")
            continue
        if provider_id == "anitopy_ml" and not ai_allowed:
            if recorder is not None:
                recorder.add_provider_result(
                    provider_id, "skipped", input={"title": title},
                    error=ai_skip_reason or ("ai_deferred_to_resolve" if recorder.stage == "resolve"
                                             else "ai_deferred_by_caller"))
            continue
        if not include_ai and provider_id != "anitopy_ml":
            if recorder is not None:
                recorder.add_provider_result(provider_id, "skipped", input={"title": title},
                                             error=ai_skip_reason or "provider_deferred_by_caller")
            continue

        options, provider_settings = _provider_options(provider_id, provider_class, settings)
        timeout = None
        if recorder is not None and recorder.deadline_monotonic is not None:
            timeout = max(recorder.deadline_monotonic - time.monotonic(), 0.001)
        request_context = {"mtype": getattr(mtype, "value", mtype)}
        if recorder is not None and recorder.deadline_monotonic is not None:
            request_context.update({"deadline_monotonic": recorder.deadline_monotonic,
                                    "remaining_timeout_seconds": timeout})
        result = recognition_service.run_provider(
            provider_id,
            RecognitionRequest(title=title, subtitle=subtitle,
                               context=request_context),
            options=options,
            cache_payload=({"title": title,
                            "model_revision": provider_settings.get(
                                "model_revision", (provider_settings.get("options") or {}).get(
                                    "model_revision", "unknown"))}
                           if provider_id == "anitopy_ml" else None),
            timeout=timeout,
        )
        if result.status != "success":
            continue
        if provider_id == "anitopy_ml":
            from app.media.recognition.adapters import meta_from_anitopy_result
            provider_meta = meta_from_anitopy_result(title, result.parsed, subtitle, used_info)
        else:
            from app.media.recognition.adapters import meta_from_standard_result
            provider_meta = meta_from_standard_result(title, result.parsed, subtitle, used_info)
        if provider_meta:
            if mtype:
                provider_meta.type = mtype
            candidates.append((provider_id, provider_meta))

    selected, selected_provider = _merge_parse_results(candidates)
    if recorder is not None:
        recorder.context["selected_provider"] = selected_provider
    if selected is not None:
        selected._recognition_original_title = original_title
        selected._recognition_original_subtitle = original_subtitle
        selected._recognition_processed_title = title
        selected._recognition_processed_subtitle = subtitle
        selected._recognition_used_info = used_info
    return selected


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
