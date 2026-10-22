"""使用一份不可变规则快照，并保守地映射 Unicode 源文本。"""

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import hashlib
import json
import unicodedata

import regex

from app.media.recognition.contracts import PreprocessResult, RuleApplication, RuleMatch, TokenSpan
from app.utils.word_rules import WordRuleProcessor


RULE_GROUPS = ("ignored_words_info", "ignored_words_noregex_info", "replaced_words_info",
               "replaced_words_noregex_info", "replaced_offset_words_info", "offset_words_info")
PREPROCESSOR_VERSION = "4"


@dataclass(frozen=True, slots=True)
class WordRule:
    ID: str
    REPLACED: str = ""
    REPLACE: str = ""
    FRONT: str = ""
    BACK: str = ""
    OFFSET: str = ""


@dataclass(frozen=True, slots=True)
class RuleSnapshot:
    groups: tuple[tuple[WordRule, ...], ...] = ((), (), (), (), (), ())

    @property
    def version(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def snapshot_rules(helper):
    return RuleSnapshot(tuple(tuple(WordRule(
        str(getattr(rule, "ID", index)), **{key: str(getattr(rule, key, "") or "")
            for key in ("REPLACED", "REPLACE", "FRONT", "BACK", "OFFSET")})
        for index, rule in enumerate(getattr(helper, group, ()) or ())) for group in RULE_GROUPS))


def _mapped_text(raw, rules, source, normalize=True):
    # 每个输出码位都对应一个源文本区间；发生变化的范围不会声称
    # 字符完全对应（包括兼容性规范化）。
    text = raw
    mapping = [(i, i + 1, False, None, "exact") for i in range(len(raw))]
    applied, errors, matches = [], [], []

    def update(after, rule_id, operation):
        nonlocal text, mapping
        if after == text:
            return
        new_mapping = []
        for tag, a, b, c, d in SequenceMatcher(None, text, after, autojunk=False).get_opcodes():
            if tag == "equal":
                new_mapping.extend(mapping[a:b])
                continue
            spans = mapping[a:b]
            start = min((item[0] for item in spans), default=(mapping[a - 1][1] if a else 0))
            end = max((item[1] for item in spans), default=start)
            applied.append(RuleApplication(rule_id, operation, source, start, end, after[c:d]))
            new_mapping.extend([(start, end, True, rule_id, "range")] * (d - c))
        text, mapping = after, new_mapping

    if normalize:
        update(unicodedata.normalize("NFKC", text), "unicode_nfkc", "normalize")
    used = {"ignored": [], "replaced": [], "offset": []}
    for group_index, group in enumerate(rules.groups):
        for rule in group:
            processor = WordRuleProcessor()
            for index, name in enumerate(RULE_GROUPS):
                setattr(processor, name, (rule,) if index == group_index else ())
            after, messages, result = processor.process(text)
            operation = ("ignore", "ignore", "replace", "replace", "replace_offset", "offset")[group_index]
            if any(result.values()):
                matches.append(RuleMatch(rule.ID, operation, source, text, after,
                    rule.REPLACED, rule.REPLACE, rule.FRONT, rule.BACK, rule.OFFSET))
            errors.extend(messages)
            for key in used:
                used[key].extend(result[key])
            update(after, rule.ID, operation)
    spans = []
    for index, (start, end, synthetic, rule_id, quality) in enumerate(mapping):
        spans.append(TokenSpan(source, start, end, index, index + 1, rule_id, synthetic, quality))
    return text, spans, applied, used, errors, matches


def preprocess_with_details(title, secondary_title=None, *, rules=None, normalize=True):
    """不访问数据库、配置或 HTTP；所有解析器使用同一快照。"""
    rules = RuleSnapshot() if rules is None else rules
    if not isinstance(title, str) or secondary_title is not None and not isinstance(secondary_title, str):
        raise ValueError("title must be text")
    main, spans, applied, used, errors, matches = _mapped_text(title, rules, "title", normalize)
    secondary = None
    if secondary_title is not None:
        secondary, extra_spans, extra_applied, _used, _errors, extra_matches = _mapped_text(secondary_title, rules, "secondary_title", normalize)
        spans.extend(extra_spans)
        applied.extend(extra_applied)
        errors.extend(_errors)
        matches.extend(extra_matches)
        for key in used:
            used[key].extend(_used[key])
    technical_text = main.replace("_", " ")
    tokens = regex.findall(r"(?i)\bS\d{1,3}\b|S\d{1,3}(?=E)", technical_text)
    episodes = regex.findall(r"(?i)(?:E|EP)\d{1,5}(?:(?:-E?|E)\d{1,5})*", main)
    codecs = regex.findall(r"(?i)\b(?:H[ .]?26[45]|HEVC|AV1|X26[45])\b", technical_text)
    resolutions = regex.findall(r"(?i)\b(?:\d{3,4}[pi]|[248]K)\b", technical_text)
    # 1920x1080 这类尺寸属于技术信息，不能作为发行年份。
    year_text = regex.sub(r"(?i)(?<!\d)\d{3,4}\s*[x×]\s*\d{3,4}(?!\d)", "", main)
    years = regex.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", year_text)
    result = PreprocessResult(title, main, rules.version, PREPROCESSOR_VERSION, secondary_title, secondary,
        candidate_titles=tuple(dict.fromkeys(item for item in (main, secondary) if item)),
        year_hint=int(years[-1]) if years else None, season_tokens=tuple(tokens),
        episode_tokens=tuple(episodes), codecs=tuple(codecs), resolutions=tuple(resolutions),
        applied_rules=tuple(applied), token_spans=tuple(spans), rule_matches=tuple(matches))
    return result, used, errors


def preprocess(title, secondary_title=None, *, rules=None):
    return preprocess_with_details(title, secondary_title, rules=rules)[0]
