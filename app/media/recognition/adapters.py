"""将通用解析器输出适配为旧媒体对象。"""

from app.media.meta.metaanime import MetaAnime
from app.media.meta.metavideo import MetaVideo
from app.utils import StringUtils
from app.utils.types import MediaType


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


def _first_value(value):
    return value[0] if isinstance(value, (list, tuple)) and value else value


def _values(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _episode_value(value):
    if isinstance(value, dict):
        value = value.get("value")
    try:
        number = float(value)
        return int(number) if number.is_integer() and number > 0 else None
    except (TypeError, ValueError):
        return None


def meta_from_anitopy_result(title, result, subtitle=None, used_info=None):
    """适配 anitopy-ml 原始结构，不通过 MetaInfo 再次解析。"""
    if not isinstance(result, dict):
        return None
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else result
    names = []
    parsed_title = (extracted.get("title") or extracted.get("anime_title")
                    or extracted.get("name"))
    title_spans = extracted.get("title_spans") or []
    if not parsed_title and title_spans and isinstance(title_spans[0], dict):
        parsed_title = title_spans[0].get("text")
    if not parsed_title:
        parsed_title = _first_value(extracted.get("all_titles"))
    aliases = (extracted.get("title_aliases") or extracted.get("all_titles")
               or extracted.get("titles") or [])
    for name in [parsed_title, *_values(aliases)]:
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    if not names:
        return None

    media_type = _media_type(extracted.get("media_type") or extracted.get("type"))
    raw_seasons = (extracted.get("seasons") or extracted.get("season")
                   or extracted.get("season_number") or extracted.get("anime_season"))
    seasons = [int(value) for value in _values(raw_seasons)
               if str(value).isdigit() and int(value) > 0]
    raw_episodes = (extracted.get("episodes") or extracted.get("episode")
                    or extracted.get("episode_number"))
    episodes = [value for value in (_episode_value(item) for item in _values(raw_episodes))
                if value is not None]
    if not media_type:
        if extracted.get("anime_title") or extracted.get("all_titles") \
                or extracted.get("anime_year") is not None:
            media_type = MediaType.ANIME
        elif seasons or episodes or extracted.get("release_kind") in ("episode", "season_pack"):
            media_type = MediaType.TV
        elif extracted.get("release_kind") == "movie":
            media_type = MediaType.MOVIE
    if not media_type:
        return None

    meta_class = MetaAnime if media_type == MediaType.ANIME else MetaVideo
    meta_info = meta_class("", subtitle=subtitle, fileflag=False)
    meta_info.type = media_type
    meta_info.org_string = title
    meta_info.subtitle = subtitle
    meta_info.recognition_source = "ai"
    used_info = used_info or {"ignored": [], "replaced": [], "offset": []}
    meta_info.ignored_words = used_info.get("ignored", [])
    meta_info.replaced_words = used_info.get("replaced", [])
    meta_info.offset_words = used_info.get("offset", [])
    meta_info.alternative_names = names
    for name in names:
        if StringUtils.is_chinese(name) and not meta_info.cn_name:
            meta_info.cn_name = name
        elif not meta_info.en_name:
            meta_info.en_name = StringUtils.str_title(name)
    if not meta_info.cn_name and not meta_info.en_name:
        meta_info.en_name = names[0]
    year = extracted.get("year") or extracted.get("anime_year")
    if year is not None:
        meta_info.year = str(year)
    if seasons:
        meta_info.begin_season = seasons[0]
        meta_info.end_season = seasons[-1] if len(seasons) > 1 else None
    if episodes:
        meta_info.begin_episode = episodes[0]
        meta_info.end_episode = episodes[-1] if len(episodes) > 1 else None
    meta_info.resource_type = _first_value(
        extracted.get("source") or extracted.get("release_type"))
    meta_info.resource_pix = _first_value(
        extracted.get("resolution") or extracted.get("video_resolution"))
    meta_info.video_encode = _first_value(
        extracted.get("video_codecs") or extracted.get("video_encoders")
        or extracted.get("video_term"))
    meta_info.audio_encode = _first_value(
        extracted.get("audio_codecs") or extracted.get("audio_term"))
    meta_info.resource_team = _first_value(
        extracted.get("release_groups") or extracted.get("release_group"))
    return meta_info


def meta_from_standard_result(title, parsed, subtitle=None, used_info=None):
    """适配其他识别器的标准化字段，不再次执行解析。"""
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("name") or parsed.get("title")
    media_type = _media_type(parsed.get("media_type") or parsed.get("type"))
    if not name or not media_type:
        return None
    meta_class = MetaAnime if media_type == MediaType.ANIME else MetaVideo
    meta_info = meta_class("", subtitle=subtitle, fileflag=False)
    meta_info.org_string = title
    meta_info.type = media_type
    meta_info.recognition_source = "provider"
    used_info = used_info or {"ignored": [], "replaced": [], "offset": []}
    meta_info.ignored_words = used_info.get("ignored", [])
    meta_info.replaced_words = used_info.get("replaced", [])
    meta_info.offset_words = used_info.get("offset", [])
    meta_info.alternative_names = parsed.get("alternative_names") or [name]
    if StringUtils.is_chinese(name):
        meta_info.cn_name = name
    else:
        meta_info.en_name = StringUtils.str_title(name)
    meta_info.year = str(parsed.get("year")) if parsed.get("year") is not None else None
    for source, target in (("season", "begin_season"), ("episode", "begin_episode"),
                           ("part", "part"), ("resource_type", "resource_type"),
                           ("resource_pix", "resource_pix"), ("resource_team", "resource_team"),
                           ("video_encode", "video_encode"), ("audio_encode", "audio_encode")):
        value = parsed.get(source)
        if value is not None:
            setattr(meta_info, target, value)
    return meta_info
