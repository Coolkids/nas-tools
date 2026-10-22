"""查询和排序共用标题别名及官方别名证据。"""

import re
import unicodedata


def has_cjk(value):
    return bool(re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", value))


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
