import os.path
import sys
import regex as re

import log
from app.helper import WordsHelper
from app.media.meta.metaanime import MetaAnime
from app.media.meta.metavideo import MetaVideo
from app.media.recognition import RecognitionRequest, registry
from app.media.recognition.records import current_recorder, recognition_scope
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
        ("app.rss", "rss"),
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


def MetaInfo(title, subtitle=None, mtype=None, apply_custom_words=True):
    """Parse a title through the discoverable local recognizer and record it."""
    recorder = current_recorder()
    if recorder is not None:
        return _record_meta_parse(recorder, title, subtitle, mtype, apply_custom_words)

    caller_module = sys._getframe(1).f_globals.get("__name__", "")
    source = _infer_recognition_source(caller_module)
    with recognition_scope(title, source=source, stage="parse_only",
                           context={"caller_module": caller_module} if caller_module else None) as recorder:
        result = _record_meta_parse(recorder, title, subtitle, mtype, apply_custom_words)
        recorder.set_overall(
            status="success" if result and result.get_name() else "failed",
            reason=None if result and result.get_name() else "no_name_parsed",
            parsed_result=_meta_snapshot(result),
            selected_provider="local_rules",
        )
        return result


def _record_meta_parse(recorder, title, subtitle, mtype, apply_custom_words):
    provider = registry.create("local_rules")
    if provider is None:
        parsed = _parse_meta_info(title, subtitle, mtype, apply_custom_words)
        recorder.add_provider_result("local_rules", "success" if parsed else "no_result",
                                     input={"title": title, "subtitle": subtitle},
                                     normalized_result=_meta_snapshot(parsed))
        return parsed
    result = provider.parse(RecognitionRequest(
        title=title,
        subtitle=subtitle,
        context={"mtype": mtype, "apply_custom_words": apply_custom_words},
    ))
    parsed = result.parsed
    if parsed is None and result.status == "error":
        parsed = _parse_meta_info(title, subtitle, mtype, apply_custom_words)
    recorder.add_provider_result(
        result.provider_id,
        result.status,
        input={"title": title, "subtitle": subtitle, "mtype": getattr(mtype, "value", mtype)},
        raw_result=result.raw_result,
        normalized_result=_meta_snapshot(parsed),
        elapsed_ms=result.elapsed_ms,
        error=result.error,
    )
    return parsed


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
