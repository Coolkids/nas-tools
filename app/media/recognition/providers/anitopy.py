"""Independent always-on Anitopy extraction, without AI, TMDB or word rules."""

import re
import time
import anitopy
from app.media.recognition.contracts import MediaNameRecognizer, ParseResult, RecognizerDescriptor
from app.media.recognition.provider_schema import ml_extraction
from app.media.recognition.episode_resolver import find_season
from app.media.recognition.title_names import title_variants


class AnitopyRecognizer(MediaNameRecognizer):
    descriptor = RecognizerDescriptor("anitopy", "Anitopy", "2.2-v3.4", "local_rules")

    def __init__(self, timeout=None):
        self.timeout = timeout

    def parse(self, request):
        started = time.monotonic()
        try:
            raw = anitopy.parse(request.title)
            value = ml_extraction(raw)
            # Anitopy leaves Chinese season labels in anime_title. Preserve the
            # raw output while separating this explicit coordinate from the name.
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
            # Anitopy sometimes leaves an unbracketed release year in its title.
            # Require a trailing year plus a following technical marker, so a
            # numeric work name (1917, 2012, etc.) is never removed on its own.
            if value["year"] is None and isinstance(value["title"], str):
                years = re.findall(r"(?i)(?<=[ ._(\[])((?:19|20)\d{2})(?=[ ._)\]]*"
                    r"(?:\d{3,4}[pi]|[248]k|bluray|web|hdtv|dvdrip|remux|h[ .]?26[45]|hevc|av1)\b)", request.title)
                if years:
                    year = years[-1]
                    title = re.sub(r"\s+" + year + r"$", "", value["title"])
                    if title and title != value["title"]:
                        value["title"], value["year"] = title, int(year)
            # A secondary title is a separate source, not silently discarded.
            if request.subtitle:
                second = anitopy.parse(request.subtitle)
                name = second.get("anime_title")
                if isinstance(name, str) and name.strip():
                    _number, marker = find_season(name)
                    if marker is not None and _number == season:
                        name = (name[:marker.start()] + " " + name[marker.end():]).strip() or name
                    value["secondary_title"] = name
            # Retain the old anime adapter's last-name display preference while
            # keeping the other language as an independent searchable name.
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
