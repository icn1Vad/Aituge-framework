from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


CHINESE_DIGITS = "〇零一二三四五六七八九十百千万两"
NUMBER_TOKEN = rf"[{CHINESE_DIGITS}0-9０-９]+"

# Recognition and boundary ownership are intentionally separate. These
# expressions say what a prefix looks like; the structure detector decides
# whether it owns a retrieval unit from sequence, hierarchy and context.
ARTICLE_START_RE = re.compile(rf"^[\s\u3000]*第[\s\u3000]*({NUMBER_TOKEN})[\s\u3000]*条")
CHAPTER_RE = re.compile(
    rf"^[\s\u3000]*(第[\s\u3000]*{NUMBER_TOKEN}[\s\u3000]*(?:编|章|篇))[\s\u3000]*(.*)$"
)
SECTION_RE = re.compile(rf"^[\s\u3000]*(第[\s\u3000]*{NUMBER_TOKEN}[\s\u3000]*节)[\s\u3000]*(.*)$")
DECIMAL_START_RE = re.compile(
    r"^[\s\u3000]*([0-9０-９]+(?:[\s\u3000]*[.．][\s\u3000]*[0-9０-９]+){1,5})"
    r"(?:[.．])?(?=\s|[\u3000、:：《【（(]|[\u4e00-\u9fff])"
)
ARABIC_HEADING_RE = re.compile(
    r"^[\s\u3000]*([0-9０-９]+)(?:[.．、][\s\u3000]+|[\s\u3000]+)(?=\S)"
)
ARABIC_ITEM_RE = re.compile(r"^[\s\u3000]*([0-9０-９]+)[.．、](?=\S)")
CHINESE_HEADING_RE = re.compile(rf"^[\s\u3000]*([{CHINESE_DIGITS}]+)[、.．][\s\u3000]*")
PAREN_CHINESE_RE = re.compile(rf"^[\s\u3000]*[（(]([{CHINESE_DIGITS}]+)[）)][\s\u3000]*")
PAREN_ARABIC_RE = re.compile(r"^[\s\u3000]*[（(]([0-9０-９]+)[）)][\s\u3000]*")
ALPHA_ITEM_RE = re.compile(r"^[\s\u3000]*([A-Za-z])[)）.．][\s\u3000]*")
CIRCLED_ITEM_RE = re.compile(r"^[\s\u3000]*([①②③④⑤⑥⑦⑧⑨⑩])[\s\u3000]*")
APPENDIX_HEADING_RE = re.compile(
    r"^[\s\u3000]*(附件|附录)(?:[\s\u3000]*[A-Za-zＡ-Ｚａ-ｚ一二三四五六七八九十0-9０-９]+)?(?:[：:]|\s|$)"
)
PAGE_FOOTER_RE = re.compile(r"^第?\s*\d+\s*页(?:\s*共\s*\d+\s*页)?$")
TOC_LEADER_RE = re.compile(r"(?:\.{3,}|…{2,}|·{3,}|_{3,})\s*\d*\s*$")


class MarkerKind(StrEnum):
    ARTICLE = "article"
    CHAPTER = "chapter"
    SECTION = "section"
    DECIMAL = "decimal"
    ARABIC_HEADING = "arabic_heading"
    ARABIC_ITEM = "arabic_item"
    CHINESE_HEADING = "chinese_heading"
    PAREN_CHINESE = "paren_chinese"
    PAREN_ARABIC = "paren_arabic"
    ALPHA_ITEM = "alpha_item"
    CIRCLED_ITEM = "circled_item"
    APPENDIX = "appendix"


@dataclass(frozen=True, slots=True)
class NumberingMarker:
    kind: MarkerKind
    raw: str
    normalized: str
    depth: int
    path: tuple[int, ...] = ()


def detect_numbering_marker(text: str) -> NumberingMarker | None:
    """Recognize a leading numbering marker without deciding chunk ownership."""
    article = ARTICLE_START_RE.match(text)
    if article:
        raw = article.group(0).strip()
        return NumberingMarker(MarkerKind.ARTICLE, raw, _compact_spaces(raw), 1)

    chapter = CHAPTER_RE.match(text)
    if chapter:
        raw = chapter.group(1).strip()
        return NumberingMarker(MarkerKind.CHAPTER, raw, _compact_spaces(raw), 0)
    section = SECTION_RE.match(text)
    if section:
        raw = section.group(1).strip()
        return NumberingMarker(MarkerKind.SECTION, raw, _compact_spaces(raw), 0)

    appendix = APPENDIX_HEADING_RE.match(text)
    if appendix:
        raw = appendix.group(0).strip().rstrip("：:")
        return NumberingMarker(MarkerKind.APPENDIX, raw, _compact_spaces(raw), 0)

    decimal = DECIMAL_START_RE.match(text)
    if decimal:
        raw = decimal.group(1).strip()
        normalized = re.sub(r"\s+", "", _ascii_digits(raw).replace("．", "."))
        path = tuple(int(item) for item in normalized.split("."))
        return NumberingMarker(MarkerKind.DECIMAL, raw, normalized, len(path), path)

    arabic = ARABIC_HEADING_RE.match(text)
    if arabic:
        raw = arabic.group(1).strip()
        normalized = _ascii_digits(raw)
        if len(normalized) >= 4:
            return None
        return NumberingMarker(MarkerKind.ARABIC_HEADING, raw, normalized, 1, (int(normalized),))

    arabic_item = ARABIC_ITEM_RE.match(text)
    if arabic_item:
        raw = arabic_item.group(1).strip()
        normalized = _ascii_digits(raw)
        return NumberingMarker(MarkerKind.ARABIC_ITEM, raw, normalized, 1, (int(normalized),))

    chinese = CHINESE_HEADING_RE.match(text)
    if chinese:
        raw = chinese.group(0).strip()
        value = chinese_number_to_int(chinese.group(1))
        return NumberingMarker(MarkerKind.CHINESE_HEADING, raw, _compact_spaces(raw), 1, (value,))

    paren_chinese = PAREN_CHINESE_RE.match(text)
    if paren_chinese:
        raw = paren_chinese.group(0).strip()
        value = chinese_number_to_int(paren_chinese.group(1))
        return NumberingMarker(MarkerKind.PAREN_CHINESE, raw, _compact_spaces(raw), 2, (value,))

    paren_arabic = PAREN_ARABIC_RE.match(text)
    if paren_arabic:
        raw = paren_arabic.group(0).strip()
        normalized = _ascii_digits(paren_arabic.group(1))
        return NumberingMarker(MarkerKind.PAREN_ARABIC, raw, normalized, 3, (int(normalized),))

    alpha = ALPHA_ITEM_RE.match(text)
    if alpha:
        raw = alpha.group(0).strip()
        return NumberingMarker(MarkerKind.ALPHA_ITEM, raw, raw.lower(), 3)
    circled = CIRCLED_ITEM_RE.match(text)
    if circled:
        raw = circled.group(0).strip()
        return NumberingMarker(MarkerKind.CIRCLED_ITEM, raw, raw, 4)
    return None


def is_toc_entry(text: str) -> bool:
    return bool(TOC_LEADER_RE.search(text.strip()))


def chinese_number_to_int(value: str) -> int:
    value = _ascii_digits(re.sub(r"\s+", "", value))
    if value.isdigit():
        return int(value)
    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
        "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = section = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
            continue
        unit = units.get(char)
        if unit is None:
            return 0
        if unit == 10000:
            total += (section + number) * unit
            section = 0
        else:
            section += (number or 1) * unit
        number = 0
    return total + section + number


def _compact_spaces(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", value)


def _ascii_digits(value: str) -> str:
    return value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
