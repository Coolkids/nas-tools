"""Resolve this file's episode context without borrowing cached episode numbers."""

from dataclasses import dataclass
import re
import unicodedata
import cn2an


SEASON_PATTERN = re.compile(r"(?i)(?<![A-Za-z])S(\d{1,3})(?=E|\b)|第\s*([\d零〇一二两兩三四五六七八九十百千]+)\s*季(?![度节節])")


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


def resolve_single_season_episode(episode, detail):
    """Absolute numbering equals seasonal numbering only for a verified single first season."""
    if (episode is None or episode.numbering != "absolute" or episode.season is not None
            or episode.review_reason != "absolute_mapping_required" or not episode.episodes):
        return episode
    rows = detail.get("seasons")
    if not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get("season_number")) is not int
            or row["season_number"] < 0 for row in rows):
        return episode
    seasons = [row for row in rows if row["season_number"] > 0]
    if len(seasons) != 1 or seasons[0]["season_number"] != 1:
        return episode
    declared = detail.get("number_of_seasons")
    if declared is not None and (type(declared) is not int or declared != 1):
        return episode
    count = seasons[0].get("episode_count")
    if type(count) is not int or count <= 0 or max(episode.episodes) > count:
        return episode
    return EpisodeContext(1, episode.episodes, "season", "tmdb_single_season")


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
    if season is None:
        parent = re.search(r"(?i)\bSeason\s*(\d{1,3})\b", parent_title or "")
        season = int(parent[1]) if parent else find_season(parent_title)[0]
    if season_override is not None:
        season = season_override
    if episodes_override is not None:
        return EpisodeContext(season if season is not None else 1, _numbers(episodes_override), "season", "override")
    numeric_file = re.fullmatch(r"(?i)(\d{1,4})(?:\.(?:mkv|mp4|avi|ts|mov|strm))?", text.strip())
    if numeric_file:
        return EpisodeContext(season if season is not None else 1, _numbers((int(numeric_file[1]),)), "season")
    match = re.search(r"(?i)(?:S\d{1,3})?(?:EP?|SP)(\d{1,5})((?:(?:-E?|E)\d{1,5})*)\b", text)
    if not match:
        match = re.search(r"第\s*(\d{1,5})(?:\s*[-~]\s*(\d{1,5}))?\s*[集话話]", text + " " + secondary)
        if match:
            start, end = int(match[1]), int(match[2] or match[1])
            if end < start or end - start >= 2000:
                return EpisodeContext(season, review_reason="invalid_episode_range")
            return EpisodeContext(season if season is not None else 1, tuple(range(start, end + 1)), "season")
    if match:
        first = int(match[1])
        tail = match[2] or ""
        if tail.startswith("-"):
            end = int(re.sub(r"(?i)^-E?", "", tail)) if re.fullmatch(r"(?i)-E?\d+", tail) else -1
            if end < first or end - first >= 2000:
                return EpisodeContext(season, review_reason="invalid_episode_range")
            values = range(first, end + 1)
        else:
            values = [first, *map(int, re.findall(r"(?i)E(\d+)", tail))]
        special = match[0].upper().startswith("SP")
        return EpisodeContext(0 if special and season_override is None else season if season is not None else 1,
                              _numbers(values), "season")
    absolute = re.search(r"\s-\s(\d{1,5})(?=\s|\[|\.|$)", text)
    if absolute:
        number = int(absolute[1])
        mapped = (absolute_mapping or {}).get(number)
        if mapped is None:
            if season is not None:
                return EpisodeContext(season, _numbers((number,)), "season")
            return EpisodeContext(season, (number,), "absolute", review_reason="absolute_mapping_required")
        mapped_season, episode = mapped
        if type(mapped_season) is not int or mapped_season < 0:
            raise ValueError("invalid absolute mapping")
        return EpisodeContext(mapped_season, _numbers((episode,)), "season", "absolute_mapping")
    return EpisodeContext(season)
