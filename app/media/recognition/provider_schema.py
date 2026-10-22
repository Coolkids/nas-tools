"""Bounded extraction contracts; parser output never establishes a media ID."""

import hashlib
import json
from app.media.recognition.contracts import NormalizedParse


FIELDS = {"title", "secondary_title", "aliases", "media_type", "year", "season", "episodes", "episode_numbering"}


def strict_json_loads(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate_json_field")
            value[key] = item
        return value
    def constant(_value):
        raise ValueError("nonfinite_json_number")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


EXTRACTION_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "title": {"type": ["string", "null"]}, "secondary_title": {"type": ["string", "null"]},
    "aliases": {"type": "array", "items": {"type": "string"}},
    "media_type": {"type": "string", "enum": ["movie", "tv", "unknown"]},
    "year": {"type": ["integer", "null"]}, "season": {"type": ["integer", "null"]},
    "episodes": {"type": "array", "items": {"type": "integer"}},
    "episode_numbering": {"type": "string", "enum": ["seasonal", "absolute", "unknown"]},
}, "required": sorted(FIELDS)}


def validate_extraction(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("invalid_extraction_fields")
    for name in ("title", "secondary_title"):
        item = value[name]
        if item is not None and (not isinstance(item, str) or not item.strip() or len(item) > 512):
            raise ValueError("invalid_extraction_title")
    aliases, episodes = value["aliases"], value["episodes"]
    if (not isinstance(aliases, list) or len(aliases) > 16 or
            any(not isinstance(x, str) or not x.strip() or len(x) > 512 for x in aliases)):
        raise ValueError("invalid_extraction_aliases")
    if (not isinstance(episodes, list) or len(episodes) > 200 or
            any(type(x) is not int or not 1 <= x <= 100000 for x in episodes)):
        raise ValueError("invalid_extraction_episodes")
    for name, low, high in (("year", 1870, 2200), ("season", 0, 10000)):
        x = value[name]
        if x is not None and (type(x) is not int or not low <= x <= high):
            raise ValueError("invalid_extraction_" + name)
    if value["media_type"] not in ("movie", "tv", "unknown") or value["episode_numbering"] not in ("seasonal", "absolute", "unknown"):
        raise ValueError("invalid_extraction_enum")
    if not value["title"] and not value["aliases"]:
        raise ValueError("missing_extraction_title")
    return value


def normalized(value, provider, version, family, *, duration_ms=0):
    value = validate_extraction(value)
    return NormalizedParse(provider, version, family, "success", **value, duration_ms=duration_ms)


def signature(provider, version, prepared, options):
    # Credential changes are represented by a revision, never their plaintext.
    safe = {k: v for k, v in options.items() if k not in {"api_key", "timeout"}}
    value = {"provider": provider, "version": version, "options": safe,
             "rule_version": prepared.rule_version, "preprocessor_version": prepared.preprocessor_version}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def ml_extraction(result):
    """Normalize known ML field aliases, rejecting malformed ranges and identities."""
    if not isinstance(result, dict):
        raise ValueError("invalid_ml_schema")
    data = result.get("extracted", result)
    if not isinstance(data, dict) or any(key in data for key in ("tmdb_id", "tvdb_id", "imdb_id")):
        raise ValueError("invalid_ml_identity")
    def scalar(key, *aliases):
        return next((data[k] for k in (key, *aliases) if data.get(k) is not None), None)
    def integer(value):
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        return value
    title = scalar("title", "anime_title", "name")
    aliases = scalar("aliases", "title_aliases", "all_titles", "titles") or []
    if title is None and isinstance(aliases, list) and aliases:
        title = aliases[0]
    episodes = scalar("episodes", "episode", "episode_number")
    if episodes is None:
        episodes = []
    elif not isinstance(episodes, list):
        episodes = [episodes]
    episodes = [integer(x.get("value") if isinstance(x, dict) else x) for x in episodes]
    season = scalar("season", "season_number", "anime_season")
    if isinstance(season, list):
        if len(season) != 1:
            raise ValueError("ambiguous_ml_season")
        season = season[0]
    kind = scalar("media_type", "type") or ("tv" if season is not None or episodes else "unknown")
    if kind == "anime":
        kind = "tv"
    return validate_extraction({"title": title, "secondary_title": data.get("secondary_title"), "aliases": aliases,
        "media_type": kind, "year": integer(scalar("year", "anime_year")), "season": integer(season),
        "episodes": episodes, "episode_numbering": data.get("episode_numbering", "unknown")})
