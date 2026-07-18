from __future__ import annotations

import re
import unicodedata


_VERSION_PAREN_RE = re.compile(
    r"[（(][^）)]*(?:试行|暂行|修订|修正|版本|第?\s*\d+\s*版|20\d{2}\s*年)[^）)]*[）)]",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}\s*年?")
_VERSION_RE = re.compile(r"(?:第\s*[一二三四五六七八九十\d]+\s*版|V\s*\d+(?:\.\d+)*)", re.IGNORECASE)
_TRAILING_VERSION_RE = re.compile(r"(?:试行|暂行|修订版?|修正版?|测试修改版)$")
_DATASET_PREFIX_RE = re.compile(r"^(?:L[123]|\d{1,3})[_\-]+", re.IGNORECASE)
_DATASET_SUFFIX_RE = re.compile(r"[_\-]+(?:二级公司|三级子公司).*$")
_SEPARATORS_RE = re.compile(r"[\s_\-—·•:：,，.。/\\]+")
_MANAGEMENT_SUFFIX_RE = re.compile(r"管理(?:办法|制度)$")


def normalize_policy_title(title: str) -> str:
    """Build a conservative deterministic key for same-name policy recall."""
    value = unicodedata.normalize("NFKC", str(title or "")).strip()
    if value.startswith("《") and value.endswith("》"):
        value = value[1:-1]
    value = value.strip("《》[]【】")
    value = _DATASET_PREFIX_RE.sub("", value)
    value = _DATASET_SUFFIX_RE.sub("", value)
    value = _VERSION_PAREN_RE.sub("", value)
    value = _YEAR_RE.sub("", value)
    value = _VERSION_RE.sub("", value)
    value = _TRAILING_VERSION_RE.sub("", value)
    value = _SEPARATORS_RE.sub("", value)
    value = _MANAGEMENT_SUFFIX_RE.sub("管理", value)
    return value.strip("()（）[]【】《》")
