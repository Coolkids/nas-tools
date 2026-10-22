"""Resolve this file's episode context without borrowing cached episode numbers."""

from dataclasses import dataclass, replace
import re
import unicodedata
import cn2an


SEASON_PATTERN = re.compile(r"(?i)(?<![A-Za-z])S(\d{1,3})(?=E|\b)|第\s*([\d零〇一二两兩三四五六七八九十百千]+)\s*季(?![度节節])")
EPISODE_PROVIDER_PRIORITY = ("anitopy", "openai", "anitopy_ml")


def find_season(text):
    match = SEASON_PATTERN.search(text or "")
    if match is None:
        return None, None
    value = next(value for value in match.groups() if value is not None)
    try:
        number = int(value) if value.isdecimal() else int(cn2an.cn2an(value.replace("兩", "两"), mode="smart"))
    except (ValueError, TypeError):
        return None, None
    return (number, match) if 0 <= number <= 10000 else (None, None)


@dataclass(frozen=True, slots=True)
class EpisodeContext:
    season: int | None = None
    episodes: tuple[int, ...] = ()
    numbering: str = "unknown"
    source: str = "file"
    review_reason: str | None = None
    season_source: str | None = None
    episodes_source: str | None = None


def default_episode_season(episode):
    """An episode without a known season belongs to season one by default."""
    if (episode is None or episode.season is not None or not episode.episodes or
            episode.review_reason not in (None, "absolute_mapping_required")):
        return episode
    return replace(episode, season=1, numbering="season", source="default_season",
                   review_reason=None, season_source="default")


def merge_episode_context(fallback, parses):
    """Select each coordinate by provider priority, preserving explicit overrides."""
    if (fallback.review_reason not in (None, "absolute_mapping_required") or
            fallback.review_reason is not None and fallback.source == "absolute_mapping"):
        return fallback
    results = {item.provider_id: item for item in parses if item.status == "success"}
    season_parse = next((results[name] for name in EPISODE_PROVIDER_PRIORITY
                         if name in results and results[name].season is not None), None)
    episode_parse = next((results[name] for name in EPISODE_PROVIDER_PRIORITY
                          if name in results and results[name].episodes), None)
    season, episodes = fallback.season, fallback.episodes
    season_source, episodes_source = fallback.season_source, fallback.episodes_source
    if season_parse is not None and season_source not in {"override", "absolute_mapping"}:
        season, season_source = season_parse.season, season_parse.provider_id
    if episode_parse is not None and episodes_source not in {"override", "absolute_mapping"}:
        episodes, episodes_source = _numbers(episode_parse.episodes), episode_parse.provider_id
    if (season == fallback.season and episodes == fallback.episodes and
            season_source == fallback.season_source and episodes_source == fallback.episodes_source):
        return default_episode_season(fallback)
    return default_episode_season(EpisodeContext(season, episodes,
        "season" if episodes else fallback.numbering, "parser_priority", None, season_source, episodes_source))


def find_bracket_episode(text):
    """Recognize release suffixes such as [02], excluding years/resolutions."""
    for match in re.finditer(r"\[(\d{1,5})(?:\s*[-~]\s*(\d{1,5}))?\]", text or ""):
        number = int(match[1])
        if not text[:match.start()].strip():
            continue
        if match[2] is None and (1870 <= number <= 2200 or number in {480, 576, 720, 1080, 1440, 2160, 4320}):
            continue
        if number > 0:
            return match
    return None


def _numbers(values, *, minimum=1):
    result = tuple(values)
    if len(result) > 2000 or any(type(value) is not int or not minimum <= value <= 100000 for value in result):
        raise ValueError("invalid episode numbers")
    return tuple(dict.fromkeys(result))


def resolve_episode(title, *, secondary_title=None, parent_title=None, season_override=None,
                    episodes_override=None, absolute_mapping=None):
    text = unicodedata.normalize("NFKC", title or "").replace("_", " ")
    secondary = unicodedata.normalize("NFKC", secondary_title or "")
    if season_override is not None and (type(season_override) is not int or not 0 <= season_override <= 1000):
        raise ValueError("invalid season override")
    season, _match = find_season(text + " " + secondary)
    season_source = "file" if season is not None else None
    if season is None:
        parent = re.search(r"(?i)\bSeason\s*(\d{1,3})\b", parent_title or "")
        season = int(parent[1]) if parent else find_season(parent_title)[0]
        season_source = "parent" if season is not None else None
    if season_override is not None:
        season = season_override
        season_source = "override"
    def context(episodes=(), *, source="file", review_reason=None):
        return default_episode_season(EpisodeContext(season, tuple(episodes), "season" if episodes else "unknown",
            source, review_reason, season_source, "override" if source == "override" else "file" if episodes else None))
    if episodes_override is not None:
        return context(_numbers(episodes_override), source="override")
    numeric_file = re.fullmatch(r"(?i)(\d{1,4})(?:\.(?:mkv|mp4|avi|ts|mov|strm))?", text.strip())
    if numeric_file:
        return context(_numbers((int(numeric_file[1]),)))
    match = re.search(r"(?i)(?<![A-Za-z0-9])(?:S\d{1,3})?(?:EP?|SP)(\d{1,5})((?:(?:-E?|E)\d{1,5})*)\b", text)
    if not match:
        match = re.search(r"第\s*(\d{1,5})(?:\s*[-~]\s*(\d{1,5}))?\s*[集话話]", text + " " + secondary)
        if match:
            start, end = int(match[1]), int(match[2] or match[1])
            if end < start or end - start >= 2000:
                return context(review_reason="invalid_episode_range")
            return context(range(start, end + 1))
    if match:
        first = int(match[1])
        tail = match[2] or ""
        if tail.startswith("-"):
            end = int(re.sub(r"(?i)^-E?", "", tail)) if re.fullmatch(r"(?i)-E?\d+", tail) else -1
            if end < first or end - first >= 2000:
                return context(review_reason="invalid_episode_range")
            values = range(first, end + 1)
        else:
            values = [first, *map(int, re.findall(r"(?i)E(\d+)", tail))]
        special = match[0].upper().startswith("SP")
        if special and season_override is None:
            season, season_source = 0, "file"
        return context(_numbers(values))
    absolute = re.search(r"\s-\s(\d{1,5})(?:\s*[-~]\s*(\d{1,5}))?(?=\s|\[|\.|$)", text)
    if absolute:
        first, last = int(absolute[1]), int(absolute[2] or absolute[1])
        if last < first or last - first >= 2000:
            return context(review_reason="invalid_episode_range")
        numbers = _numbers(range(first, last + 1))
        mapped = [(absolute_mapping or {}).get(number) for number in numbers]
        if not any(item is not None for item in mapped):
            return context(numbers)
        if any(item is None for item in mapped):
            return context(source="absolute_mapping", review_reason="absolute_mapping_required")
        mapped_season = mapped[0][0]
        if any(type(item[0]) is not int or item[0] < 0 for item in mapped):
            raise ValueError("invalid absolute mapping")
        if any(item[0] != mapped_season for item in mapped):
            return context(source="absolute_mapping", review_reason="absolute_mapping_required")
        return EpisodeContext(mapped_season, _numbers(item[1] for item in mapped), "season", "absolute_mapping",
                              season_source="absolute_mapping", episodes_source="absolute_mapping")
    bracket = find_bracket_episode(text)
    if bracket:
        first, last = int(bracket[1]), int(bracket[2] or bracket[1])
        if last < first or last - first >= 2000:
            return context(review_reason="invalid_episode_range")
        return context(_numbers(range(first, last + 1)))
    return context()
