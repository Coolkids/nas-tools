"""查询和排序共用标题别名及官方别名证据。"""

import re
import unicodedata


def has_cjk(value):
    return bool(re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", value))


def tmdb_titles(info):
    """汇总主名称、原名、官方别名及各语言译名，用于名称和年份校验。"""
    names = {value.strip() for key in ("title", "name", "original_title", "original_name", "_tmdb_title")
             if isinstance((value := info.get(key)), str) and value.strip()}
    alternatives = info.get("alternative_titles", {})
    if isinstance(alternatives, dict):
        for row in alternatives.get("titles", alternatives.get("results", [])):
            if isinstance(row, dict) and isinstance(row.get("title"), str) and row["title"].strip():
                names.add(row["title"].strip())
    translations = info.get("translations", {})
    if isinstance(translations, dict):
        for row in translations.get("translations", []):
            data = row.get("data", {}) if isinstance(row, dict) else {}
            if not isinstance(data, dict):
                continue
            for key in ("title", "name"):
                if isinstance(data.get(key), str) and data[key].strip():
                    names.add(data[key].strip())
    return sorted(names)


def localize_tmdb_detail(detail, kind, language):
    """中文请求优先使用 TMDB 中文译名，并保留响应原名作为匹配证据。"""
    info = dict(detail)
    if not str(language).lower().startswith("zh"):
        return info
    field = "title" if kind == "movie" else "name"
    original = info.get(field)
    names = []
    def add(value, priority):
        # 排除含假名或韩文的名称，避免把日文原名当作中文译名。
        if (isinstance(value, str) and re.search(r"[\u3400-\u9fff]", value)
                and not re.search(r"[\u3040-\u30ff\uac00-\ud7af]", value)):
            names.append((priority, len(names), value.strip()))
    regions = {"CN": 0, "SG": 1, "MY": 2, "": 3, "TW": 4, "HK": 5, "MO": 6}
    translations = info.get("translations")
    translations = translations.get("translations", []) if isinstance(translations, dict) else []
    for row in translations if isinstance(translations, list) else []:
        if not isinstance(row, dict) or row.get("iso_639_1") != "zh":
            continue
        data = row.get("data")
        if isinstance(data, dict):
            region = str(row.get("iso_3166_1") or "").upper()
            add(data.get(field), regions.get(region, 7) * 10)
    # 中文详情的主名称优先于别名，简体翻译优先于其他地区译名。
    add(original, 5)
    alternatives = info.get("alternative_titles")
    alternatives = alternatives.get("titles", alternatives.get("results", [])) if isinstance(alternatives, dict) else []
    for row in alternatives if isinstance(alternatives, list) else []:
        if not isinstance(row, dict):
            continue
        region = str(row.get("iso_3166_1") or "").upper()
        if region in {"CN", "SG", "MY", "TW", "HK", "MO"}:
            add(row.get("title"), regions[region] * 10 + 6)
    if names:
        title = min(names)[2]
        if title != original:
            info["_tmdb_title"] = original
            info[field] = title
    return info


def title_variants(value):
    """按明确的别名分隔符拆分，同时保留 AC/DC 和 Fate/stay night 等名称。"""
    if not isinstance(value, str) or not value.strip():
        return ()
    value = value.strip()
    spaced = tuple(dict.fromkeys(part.strip() for part in re.split(r"\s+[/／]\s*|[/／]\s+", value) if part.strip()))
    if len(spaced) > 1:
        return spaced
    parts = tuple(dict.fromkeys(part.strip() for part in re.split(r"[/／]", value) if part.strip()))
    bilingual = len(parts) > 1 and len({has_cjk(part) for part in parts}) > 1
    if bilingual:
        groups = []
        for part in parts:
            if groups and has_cjk(groups[-1]) == has_cjk(part):
                groups[-1] += "/" + part
            else:
                groups.append(part)
        return tuple(groups)
    return (value,)


def canonical_name(value):
    return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def official_keyword_in_title(query, official):
    """完整且有辨识度的官方短语可能出现在更长的发布标题中。"""
    query, official = canonical_name(query), canonical_name(official)
    if not query or not official or query == official:
        return False
    words = official.split()
    # “It”“TV”或“Up”等很短的别名和片段不能作为佐证。
    substantial = len(official.replace(" ", "")) >= 8 and len(words) >= 2
    if has_cjk(official):
        substantial = len(official.replace(" ", "")) >= 4
    return substantial and f" {official} " in f" {query} "
