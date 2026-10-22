"""独立且始终启用的 Anitopy 提取，不依赖 AI、TMDB 或词语规则。"""

import re
import time
import anitopy
from app.media.recognition.contracts import MediaNameRecognizer, ParseResult, RecognizerDescriptor
from app.media.recognition.provider_schema import ml_extraction
from app.media.recognition.episode_resolver import find_season, resolve_episode
from app.media.recognition.title_names import title_variants


class AnitopyRecognizer(MediaNameRecognizer):
    descriptor = RecognizerDescriptor("anitopy", "Anitopy", "2.2-v3.5", "local_rules")

    def __init__(self, timeout=None):
        self.timeout = timeout

    def parse(self, request):
        started = time.monotonic()
        try:
            raw = anitopy.parse(request.title)
            value = ml_extraction(raw)
            # Anitopy 会返回范围的两个端点。只有当
            # 当前文件本身也包含该范围时才展开；E04E06 仍按稀疏集数处理。
            coordinates = resolve_episode(request.title)
            if (len(value["episodes"]) == 2 and 2 < len(coordinates.episodes) <= 200 and
                    value["episodes"] == [coordinates.episodes[0], coordinates.episodes[-1]]):
                value["episodes"] = list(coordinates.episodes)
            # Anitopy 会将中文季标签保留在 anime_title 中。保留
            # 原始输出，同时将明确的季坐标与名称分开。
            season, _marker = find_season(request.title)
            if season is None:
                season, _marker = find_season(request.subtitle)
            if season is not None and value["season"] is None:
                value["season"] = season
            if season is not None:
                for field in ("title", "secondary_title"):
                    text = value[field]
                    found, marker = find_season(text)
                    if marker is not None and found == season:
                        cleaned = (text[:marker.start()] + " " + text[marker.end():]).strip()
                        if cleaned:
                            value[field] = re.sub(r"\s+", " ", cleaned)
                if value["media_type"] == "unknown":
                    value["media_type"] = "tv"
                if value["episodes"]:
                    value["episode_numbering"] = "seasonal"
            # Anitopy 有时会在标题中留下未加括号的发行年份。
            # 要求年份位于末尾且后面还跟有技术标记，避免
            # 单独删除 1917、2012 等数字作品名。
            if value["year"] is None and isinstance(value["title"], str):
                years = re.findall(r"(?i)(?<=[ ._(\[])((?:19|20)\d{2})(?=[ ._)\]]*"
                    r"(?:\d{3,4}[pi]|[248]k|bluray|web|hdtv|dvdrip|remux|h[ .]?26[45]|hevc|av1)\b)", request.title)
                if years:
                    year = years[-1]
                    title = re.sub(r"\s+" + year + r"$", "", value["title"])
                    if title and title != value["title"]:
                        value["title"], value["year"] = title, int(year)
            # 副标题是独立来源的信息，不应被悄悄丢弃。
            if request.subtitle:
                second = anitopy.parse(request.subtitle)
                name = second.get("anime_title")
                if isinstance(name, str) and name.strip():
                    _number, marker = find_season(name)
                    if marker is not None and _number == season:
                        name = (name[:marker.start()] + " " + name[marker.end():]).strip() or name
                    value["secondary_title"] = name
            # 保留旧动漫适配器优先显示最后一个名称的习惯，
            # 同时将另一种语言的名称保留为独立的可搜索名称。
            names = title_variants(value["title"])
            alternatives = list(dict.fromkeys(name for item in (value["secondary_title"], *value["aliases"])
                for name in title_variants(item)))
            if len(names) > 1:
                value["title"] = names[-1]
                alternatives = list(dict.fromkeys((*names[:-1], *alternatives)))
            value["secondary_title"] = next((name for name in alternatives if name != value["title"]), None)
            value["aliases"] = [name for name in alternatives if name not in {value["title"], value["secondary_title"]}]
            return ParseResult("anitopy", "success", value, raw,
                               elapsed_ms=int((time.monotonic()-started)*1000))
        except (ValueError, TypeError):
            return ParseResult("anitopy", "invalid", error="invalid_anitopy_output")
