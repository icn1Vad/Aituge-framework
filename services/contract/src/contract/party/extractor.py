from __future__ import annotations

from dataclasses import dataclass
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
        "托运人",
        "出租方",
        "出租人",
        "客户方",
        "个人信息处理者",
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
        "承运人",
        "承租方",
        "承租人",
        "服务方",
        "顾问方",
        "咨询方",
        "境外接收方",
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
        "收件人",
        "证件号码",
        "身份证件号码",
        "身份证号码",
        "身份证号",
        "法定代表人",
        "注册地址",
        "通讯地址",
        "联系人",
        "住所",
        "地址",
        "电话",
        "传真",
        "邮箱",
        "电子邮件",
        "电子邮箱",
        "邮政编码",
        "授权代表",
        "日期",
    )
)
_ENTITY_NAME_FIELD_PATTERN = "|".join(
    re.escape(value)
    for value in (
        "单位名称",
        "公司名称",
        "企业名称",
        "单位全称",
    )
)
_PARTY_QUALIFIER_PATTERN = r"(?:[（(][^）)]{0,24}[）)]|【[^】]{0,24}】|\[[^\]]{0,24}\])"
_LABEL_TOKEN_PATTERN = rf"(?:【\s*|\[\s*)?(?:{_LABEL_PATTERN})(?:\s*】|\s*\])?"
_NON_TARGET_LABEL_PATTERN = r"(?:丙方|丁方|戊方)"
_NON_TARGET_LABEL_TOKEN_PATTERN = (
    rf"(?:【\s*|\[\s*)?{_NON_TARGET_LABEL_PATTERN}(?:\s*】|\s*\])?"
)
_PARTY_DECLARATION = re.compile(
    rf"(?:【\s*|\[\s*)?(?P<label>{_LABEL_PATTERN})(?:\s*】|\s*\])?"
    rf"\s*(?:{_PARTY_QUALIFIER_PATTERN})?\s*(?:名称\s*)?[：:]\s*"
    rf"(?:(?:{_ENTITY_NAME_FIELD_PATTERN})\s*[：:]\s*)?"
    rf"(?P<name>.*?)"
    rf"(?=(?:\s*{_LABEL_TOKEN_PATTERN}\s*(?:{_PARTY_QUALIFIER_PATTERN})?\s*(?:名称\s*)?[：:])"
    rf"|(?:\s*{_NON_TARGET_LABEL_TOKEN_PATTERN}\s*(?:{_PARTY_QUALIFIER_PATTERN})?\s*(?:名称\s*)?[：:])"
    rf"|(?:\s*(?:{_FIELD_PATTERN})\s*[：:])|[；;]|$)",
    re.IGNORECASE,
)
_ALIASED_PARTY_DECLARATION = re.compile(
    rf"(?P<descriptor>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{{0,23}})\s*"
    rf"[（(]\s*(?:以下)?称\s*(?P<label>甲方|乙方)\s*[）)]\s*[：:]\s*"
    rf"(?P<name>.*?)"
    rf"(?=(?:\s*{_LABEL_TOKEN_PATTERN}\s*(?:{_PARTY_QUALIFIER_PATTERN})?\s*(?:名称\s*)?[：:])"
    rf"|(?:\s*{_NON_TARGET_LABEL_TOKEN_PATTERN}\s*(?:{_PARTY_QUALIFIER_PATTERN})?\s*(?:名称\s*)?[：:])"
    rf"|(?:\s*(?:{_FIELD_PATTERN})\s*[：:])|[；;]|$)",
    re.IGNORECASE,
)
_ROLE_FIELD_DECLARATION = re.compile(
    rf"(?P<label>{_LABEL_PATTERN})\s*/\s*"
    rf"(?:注册地址|地址|法定代表人|证件号码)(?:\s*/\s*(?:注册地址|地址|法定代表人|证件号码))*",
    re.IGNORECASE,
)
_ROLE_TABLE_DECLARATION = re.compile(
    rf"(?P<label>{_LABEL_PATTERN})\s*[|｜]\s*(?:全\s*称|名称(?:\s*[（(]或姓名[）)])?)",
    re.IGNORECASE,
)
_PARENTHETICAL_ROLE_MARKER = re.compile(
    r"[（(]\s*(?P<label>甲\s*方|乙\s*方)\s*[）)]",
    re.IGNORECASE,
)
_PAIRED_ROLE_DECLARATION = re.compile(
    rf"(?P<label_a>{_LABEL_PATTERN})"
    rf"\s*[（(][^）)]{{1,24}}[）)]\s*(?:[：:]\s*)?"
    rf"(?P<name_a>.*?)"
    rf"\s*[,，]\s*"
    rf"(?P<label_b>{_LABEL_PATTERN})"
    rf"\s*[（(][^）)]{{1,24}}[）)]\s*(?:[：:]\s*)?"
    rf"(?P<name_b>.*?)(?:[；;。])?\s*$",
    re.IGNORECASE,
)
_EDGE_CHARACTERS = frozenset(" \t\r\n\u3000,，。'\"“”‘’《》<>[]【】")
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
        "签字或公章",
        "章",
        "公章",
    }
)
_SEAL_PREFIX = re.compile(
    r"^\s*[（(【\[]\s*(?:公章|盖章|签章|签字或盖章)\s*[）)】\]]\s*",
    re.IGNORECASE,
)
_ORGANIZATION_SUFFIXES = (
    "公司",
    "企业",
    "医院",
    "学校",
    "大学",
    "中心",
    "委员会",
    "事务所",
    "研究院",
)


@dataclass(frozen=True)
class PartyExtractionEvidence:
    candidates: list[IRParty]
    declared_roles: frozenset[str]


def extract_party_evidence(blocks: list[ParsedContractBlock]) -> PartyExtractionEvidence:
    """Extract candidates and explicit role-label evidence from stable source blocks."""
    candidates: dict[tuple[str, str], tuple[str, list[SourceAnchor]]] = {}
    declared_roles: set[str] = set()
    for block in blocks:
        if block.block_type == "footer":
            continue
        for matched in _PARENTHETICAL_ROLE_MARKER.finditer(block.text):
            declared_roles.add(
                "PARTY_A" if "甲" in matched.group("label") else "PARTY_B"
            )
        for matched in _ROLE_TABLE_DECLARATION.finditer(block.text):
            declared_roles.add(_LABEL_TO_ROLE[_normalized_label(matched.group("label"))])
        for matched in _ROLE_FIELD_DECLARATION.finditer(block.text):
            declared_roles.add(_LABEL_TO_ROLE[_normalized_label(matched.group("label"))])
        for matched in _PAIRED_ROLE_DECLARATION.finditer(block.text):
            paired_roles = (
                _LABEL_TO_ROLE[_normalized_label(matched.group("label_a"))],
                _LABEL_TO_ROLE[_normalized_label(matched.group("label_b"))],
            )
            if paired_roles[0] == paired_roles[1]:
                continue
            declared_roles.update(paired_roles)
            for role, group_name in zip(paired_roles, ("name_a", "name_b"), strict=True):
                start, end = _trim_name_span(block.text, *matched.span(group_name))
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
        declarations = [
            *_ALIASED_PARTY_DECLARATION.finditer(block.text),
            *_PARTY_DECLARATION.finditer(block.text),
        ]
        for matched in sorted(declarations, key=lambda item: item.start()):
            role = _LABEL_TO_ROLE[_normalized_label(matched.group("label"))]
            declared_roles.add(role)
            start, end = _trim_name_span(block.text, *matched.span("name"))
            if start >= end:
                continue
            name = block.text[start:end]
            if (
                _is_placeholder(name)
                or _is_signature_ocr_artifact(name)
                or len(name) > 500
            ):
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
    return PartyExtractionEvidence(
        candidates=[
            IRParty(role=role, name=name, source_anchors=anchors)
            for (role, _normalized), (name, anchors) in candidates.items()
        ],
        declared_roles=frozenset(declared_roles),
    )


def extract_party_candidates(blocks: list[ParsedContractBlock]) -> list[IRParty]:
    """Backward-compatible candidate-only view."""
    return extract_party_evidence(blocks).candidates


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


def _is_signature_ocr_artifact(value: str) -> bool:
    """Reject seal/stamp OCR codes while preserving real signed entity names."""
    matched = _SEAL_PREFIX.match(value)
    if matched is None:
        return False
    candidate = _normalized_name(value[matched.end() :]).rstrip("|｜")
    return (
        any(character.isdigit() for character in candidate)
        and not candidate.endswith(_ORGANIZATION_SUFFIXES)
    )


def _anchor_id(block_id: str, start: int, end: int) -> str:
    digest = hashlib.sha256(f"{block_id}\0{start}\0{end}".encode("utf-8")).hexdigest()[:32]
    return f"party-anchor-{digest}"
