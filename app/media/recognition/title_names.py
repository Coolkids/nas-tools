"""Title alternatives and official alias evidence shared by query and ranking."""

import re
import unicodedata


def has_cjk(value):
    return bool(re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", value))


def title_variants(value):
    """Split explicit alias separators, preserving names such as AC/DC and Fate/stay night."""
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
    """A substantial complete official phrase may occur inside a longer release title."""
    query, official = canonical_name(query), canonical_name(official)
    if not query or not official or query == official:
        return False
    words = official.split()
    # Very short aliases and fragments such as 'It', 'TV' or 'Up' are not proof.
    substantial = len(official.replace(" ", "")) >= 8 and len(words) >= 2
    if has_cjk(official):
        substantial = len(official.replace(" ", "")) >= 4
    return substantial and f" {official} " in f" {query} "
