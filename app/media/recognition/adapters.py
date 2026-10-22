"""将通用解析器输出适配为旧媒体对象。"""

from app.media.meta.metaanime import MetaAnime
from app.media.meta.metavideo import MetaVideo
from app.media.meta.release_groups import ReleaseGroupsMatcher
from app.utils import StringUtils
from app.utils.types import MediaType
from copy import copy, deepcopy
from app.media.recognition.contracts import NormalizedParse, PreprocessResult, RecognitionStatus


def _media_type(value):
    mapping = {
        "movie": MediaType.MOVIE,
        "tv": MediaType.TV,
        "anime": MediaType.ANIME,
        "电影": MediaType.MOVIE,
        "电视剧": MediaType.TV,
        "动漫": MediaType.ANIME,
    }
    return mapping.get(str(value or "").lower()) or mapping.get(str(value or ""))


def meta_from_normalized(prepared: PreprocessResult, parsed: NormalizedParse, *, episode_context=None):
    """构造兼容对象，不再重复解析或应用规则。"""
    kind = _media_type(parsed.media_type)
    meta = (MetaAnime if kind == MediaType.ANIME else MetaVideo)("", fileflag=False)
    meta.org_string, meta.subtitle, meta.type = prepared.raw_title, prepared.raw_secondary_title, kind
    meta.alternative_names = list(dict.fromkeys(value for value in (parsed.title, parsed.secondary_title, *parsed.aliases) if value))
    meta.cn_name = next((value for value in meta.alternative_names if StringUtils.is_chinese(value)), None)
    meta.en_name = next((value for value in meta.alternative_names if not StringUtils.is_chinese(value)), None)
    meta.year = str(parsed.year) if parsed.year is not None else None
    meta.recognition_source = parsed.provider_id
    # 制作组从当前原始文件名匹配，避免识别词清洗或作品缓存丢掉发布信息。
    matcher = ReleaseGroupsMatcher()
    release = parsed.release_metadata if isinstance(parsed.release_metadata, dict) else {}
    meta.resource_team = (matcher.match(title=prepared.raw_title)
        or matcher.match(title=prepared.clean_title) or release.get("release_group") or None)
    meta._recognition_preprocess = prepared
    meta._recognition_original_title, meta._recognition_original_subtitle = prepared.raw_title, prepared.raw_secondary_title
    meta._recognition_processed_title, meta._recognition_processed_subtitle = prepared.clean_title, prepared.clean_secondary_title
    matches = prepared.rule_matches
    meta._recognition_used_info = {
        "ignored": [match.pattern for match in matches if match.operation == "ignore"],
        "replaced": [f"{match.pattern}@{match.replacement}" for match in matches
                     if match.operation in {"replace", "replace_offset"}],
        "offset": [f"{match.front}@{match.back}@{match.offset}" for match in matches
                   if match.operation in {"offset", "replace_offset"}],
    }
    season, episodes = parsed.season, parsed.episodes
    if episode_context is not None:
        season, episodes = episode_context.season, episode_context.episodes
        meta.recognition_review_reason = episode_context.review_reason
        if episode_context.numbering == "absolute":
            episodes = ()
    _apply_episode_context(meta, season, episodes)
    return meta


def _apply_episode_context(meta, season, episodes):
    if season is not None and (type(season) is not int or season < 0):
        raise ValueError("invalid season")
    if any(type(value) is not int or value <= 0 for value in episodes):
        raise ValueError("invalid episodes")
    meta.begin_season, meta.end_season = season, None
    meta.explicit_episode_numbers = tuple(episodes)
    meta.begin_episode = episodes[0] if episodes else None
    meta.end_episode = episodes[-1] if len(episodes) > 1 else None
    meta.total_episodes = len(episodes)


def meta_from_outcome(outcome, parsed_meta, detail=None):
    meta = copy(parsed_meta)
    meta.tmdb_info, meta.tmdb_id, meta.tvdb_id, meta.imdb_id, meta.douban_id = {}, 0, 0, "", 0
    meta.recognition_request_id = outcome.request_id
    meta.recognition_status = outcome.status.value
    if outcome.status != RecognitionStatus.MATCHED:
        return meta
    identity = outcome.business_identity
    if not isinstance(detail, dict) or detail.get("id") != identity.tmdb_id:
        raise ValueError("verified identity requires matching TMDB details")
    supplied = _media_type(detail.get("media_type"))
    expected = MediaType.MOVIE if identity.media_type == "movie" else MediaType.TV
    if supplied is not None and supplied not in ({MediaType.TV, MediaType.ANIME} if expected == MediaType.TV else {expected}):
        raise ValueError("TMDB entity type mismatch")
    info = deepcopy(detail)
    info["media_type"] = expected
    meta.set_tmdb_info(info)
    _apply_episode_context(meta, outcome.season, outcome.episodes)
    return meta
