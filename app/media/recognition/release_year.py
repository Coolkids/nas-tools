"""区分发行年份和作品名称中的四位数字。"""

from datetime import datetime
import re


def plausible_release_year(value):
    """允许近期预告作品，但不把遥远未来的数字当作发行年份。"""
    return type(value) is int and 1870 <= value <= datetime.now().year + 1


def year_in_titles(value, titles):
    if type(value) is not int:
        return False
    pattern = re.compile(r"(?<!\d)" + str(value) + r"(?!\d)")
    return any(isinstance(title, str) and pattern.search(title) for title in titles)


def explicit_release_year(value, texts):
    """电视剧只采信独立括号或明确年份标签，不从裸标题数字推断。"""
    if not plausible_release_year(value):
        return False
    year = str(value)
    pattern = re.compile(r"(?:\(\s*" + year + r"\s*\)|\[\s*" + year
        + r"\s*\]|【\s*" + year + r"\s*】|(?:发行年份|首播年份|播出年份|年份)\s*[:：=]?\s*"
        + year + r"(?!\d))")
    return any(isinstance(text, str) and pattern.search(text) for text in texts)


def release_year(value, *, titles=()):
    """标题中出现同一数字时不将其作为年份证据。"""
    return value if plausible_release_year(value) and not year_in_titles(value, titles) else None
