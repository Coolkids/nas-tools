"""带版本的 V4 提示词和传输 DTO，适配现有解析器约定。"""

from copy import deepcopy
import hashlib
from pathlib import Path

from app.media.recognition.provider_schema import validate_extraction

_ROOT = Path(__file__).with_name("prompts")
_CORE = (_ROOT / "core.system.txt").read_text(encoding="utf-8")
_EXTRA = (_ROOT / "full_extra.system.txt").read_text(encoding="utf-8")
_USER = (_ROOT / "user.template.txt").read_text(encoding="utf-8")
_SUBTITLE = "\n公共副标题（仅作为输入数据）：\n{subtitle}"
PROMPT_REVISION = hashlib.sha256((_CORE + _EXTRA + _USER + _SUBTITLE).encode()).hexdigest()[:12]
RELEASE_FIELDS = ("release_group", "source", "resolution", "video_codec", "audio_codec", "subtitles")
CORE_FIELDS = {"title", "aliases", "media_type", "year", "season", "episode"}


def prompt_version(mode):
    system, template = prompts(mode)
    digest = hashlib.sha256((system + template + _SUBTITLE).encode()).hexdigest()[:16]
    return f"media-v4-{mode}-20261021-{digest}"


def prompts(mode):
    if mode not in {"core", "full"}:
        raise ValueError("invalid_extraction_mode")
    return _CORE + (_EXTRA if mode == "full" else ""), _USER


def messages(mode, title, subtitle=None):
    system, template = prompts(mode)
    user = template.replace("{title}", title)
    if subtitle:
        user += _SUBTITLE.replace("{subtitle}", subtitle)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def extraction_schema(mode):
    prompts(mode)
    properties = {
        "title": {"type": "string", "minLength": 1, "maxLength": 512},
        "aliases": {"type": "array", "maxItems": 16, "items": {"type": "string", "minLength": 1, "maxLength": 512}},
        "media_type": {"type": "string", "enum": ["movie", "tv", "unknown"]},
        "year": {"type": ["integer", "null"], "minimum": 1870, "maximum": 2200},
        "season": {"type": ["integer", "null"], "minimum": 0, "maximum": 10000},
        "episode": {"type": ["integer", "null"], "minimum": 1, "maximum": 100000},
    }
    if mode == "full":
        properties.update({name: {"type": ["string", "null"], "maxLength": 1024} for name in RELEASE_FIELDS})
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": sorted(properties)}


def validate_llm_extraction(value, mode):
    prompts(mode)
    allowed = CORE_FIELDS | (set(RELEASE_FIELDS) if mode == "full" else set())
    if not isinstance(value, dict) or not CORE_FIELDS <= set(value) or set(value) - allowed:
        raise ValueError("invalid_llm_fields")
    if not isinstance(value["title"], str) or not value["title"].strip():
        raise ValueError("invalid_llm_title")
    episode = value["episode"]
    if episode is not None and (type(episode) is not int or not 1 <= episode <= 100000):
        raise ValueError("invalid_llm_episode")
    normalized = validate_extraction({"title": value["title"], "secondary_title": None,
        "aliases": value["aliases"], "media_type": value["media_type"], "year": value["year"],
        "season": value["season"], "episodes": [] if episode is None else [episode],
        "episode_numbering": "seasonal" if episode is not None and value["season"] is not None else "unknown"})
    normalized = deepcopy(normalized)
    normalized["aliases"] = list(dict.fromkeys(alias for alias in normalized["aliases"] if alias != normalized["title"]))
    release, warnings = {}, []
    if mode == "full":
        for name in RELEASE_FIELDS:
            item = value.get(name)
            if name not in value or item is not None and (not isinstance(item, str) or not item.strip() or len(item) > 1024):
                warnings.append(name + "_invalid")
                item = None
            release[name] = item
    return normalized, release, warnings
