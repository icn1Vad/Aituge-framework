from __future__ import annotations

import hashlib
import re
import unicodedata

from contract.ir.models import IRParty, SourceAnchor
from contract.parser.models import ParsedContractBlock


_ROLE_LABELS = {
    "PARTY_A": (
        "甲方",
        "买方",
        "采购方",
        "采购人",
        "需方",
        "委托方",
        "发包方",
        "出租方",
        "客户方",
        "party a",
        "buyer",
        "purchaser",
        "principal",
        "client",
    ),
    "PARTY_B": (
        "乙方",
        "卖方",
        "销售方",
        "供应方",
        "供应商",
        "供方",
        "受托方",
        "承包方",
        "承租方",
        "服务方",
        "party b",
        "seller",
        "supplier",
        "contractor",
        "service provider",
    ),
}
_LABEL_TO_ROLE = {
    unicodedata.normalize("NFKC", label).casefold(): role
    for role, labels in _ROLE_LABELS.items()
    for label in labels
}
_LABEL_PATTERN = "|".join(
    re.escape(label)
    for label in sorted(
        (label for labels in _ROLE_LABELS.values() for label in labels),
        key=len,
        reverse=True,
    )
)
_FIELD_PATTERN = "|".join(
    re.escape(value)
    for value in (
        "统一社会信用代码",
        "法定代表人",
        "注册地址",
        "通讯地址",
        "联系人",
        "住所",
        "地址",
        "电话",
        "传真",
        "邮箱",
    )
)
_PARTY_DECLARATION = re.compile(
    rf"(?P<label>{_LABEL_PATTERN})"
    rf"\s*(?:[（(][^）)]{{0,24}}[）)])?\s*(?:名称\s*)?[：:]\s*"
    rf"(?P<name>.*?)"
    rf"(?=(?:\s*(?:{_LABEL_PATTERN})\s*(?:[（(][^）)]{{0,24}}[）)])?\s*(?:名称\s*)?[：:])"
    rf"|(?:\s+(?:{_FIELD_PATTERN})\s*[：:])|[；;]|$)",
    re.IGNORECASE,
)
_EDGE_CHARACTERS = frozenset(" \t\r\n\u3000,，。'\"“”‘’《》<>")
_PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "/",
        "名称",
        "签字",
        "签章",
        "盖章",
        "签字盖章",
        "签字或盖章",
    }
)


def extract_party_candidates(blocks: list[ParsedContractBlock]) -> list[IRParty]:
    """Extract only explicitly labelled party candidates from stable source blocks."""
    candidates: dict[tuple[str, str], tuple[str, list[SourceAnchor]]] = {}
    for block in blocks:
        if block.block_type == "footer":
            continue
        for matched in _PARTY_DECLARATION.finditer(block.text):
            role = _LABEL_TO_ROLE[_normalized_label(matched.group("label"))]
            start, end = _trim_name_span(block.text, *matched.span("name"))
            if start >= end:
                continue
            name = block.text[start:end]
            if _is_placeholder(name) or len(name) > 500:
                continue
            key = role, _normalized_name(name)
            anchor = SourceAnchor(
                anchor_id=_anchor_id(block.block_id, start, end),
                block_id=block.block_id,
                page_number=block.page_number,
                char_start=start,
                char_end=end,
            )
            existing = candidates.get(key)
            if existing is None:
                candidates[key] = (name, [anchor])
            elif anchor.anchor_id not in {item.anchor_id for item in existing[1]}:
                existing[1].append(anchor)
    return [
        IRParty(role=role, name=name, source_anchors=anchors)
        for (role, _normalized), (name, anchors) in candidates.items()
    ]


def _trim_name_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start] in _EDGE_CHARACTERS:
        start += 1
    while end > start and text[end - 1] in _EDGE_CHARACTERS:
        end -= 1
    return start, end


def _normalized_label(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _is_placeholder(value: str) -> bool:
    normalized = _normalized_name(value)
    unwrapped = normalized.strip("()（）[]【】")
    return (
        normalized in _PLACEHOLDERS
        or unwrapped in _PLACEHOLDERS
        or not any(character.isalnum() for character in normalized)
    )


def _anchor_id(block_id: str, start: int, end: int) -> str:
    digest = hashlib.sha256(f"{block_id}\0{start}\0{end}".encode("utf-8")).hexdigest()[:32]
    return f"party-anchor-{digest}"
