from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    filename: str
    page_count: int
    category: str
    source_kind: str
    author: str | None = None
    publication_year: int | None = None

    @property
    def id(self) -> str:
        return "qxs-" + hashlib.sha256(self.filename.encode("utf-8")).hexdigest()[:20]

    @property
    def title(self) -> str:
        value = self.filename.removesuffix(".pdf")
        return value.replace("_--_", " ").replace("__", " ").replace("_", " ").strip()


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry("[钱学森论第六次产业革命通信集].刘恕.文字版.pdf", 283, "书信与通信", "letters", "钱学森、刘恕"),
    CatalogEntry("人民科学家钱学森.pdf", 257, "人物传记", "biography"),
    CatalogEntry("剑指苍穹-钱学森的航天传奇.pdf", 888, "人物传记", "biography"),
    CatalogEntry("工程控制论_(上册)_--_钱学森.pdf", 543, "工程控制论", "authored", "钱学森"),
    CatalogEntry("工程控制论__下册_--_钱学森.pdf", 525, "工程控制论", "authored", "钱学森"),
    CatalogEntry("材料三：钱学森系统工程理论辅助阅读教材.pdf", 219, "系统工程", "research"),
    CatalogEntry("组织管理的技术—系统工程.pdf", 8, "系统工程", "authored", "钱学森"),
    CatalogEntry("航天系统工程与总体设计部——钱学森同志在发展我国系统工程理论与实践中的贡献.pdf", 4, "系统工程", "research"),
    CatalogEntry("钱学森《论系统工程(新世纪版)》.pdf", 405, "系统工程", "authored", "钱学森"),
    CatalogEntry("钱学森与系统工程.pdf", 5, "系统工程", "research"),
    CatalogEntry("钱学森书信选（上册）.pdf", 631, "书信与通信", "letters", "钱学森"),
    CatalogEntry("钱学森书信选（下册）.pdf", 612, "书信与通信", "letters", "钱学森"),
    CatalogEntry("钱学森文集·卷1_1_--_钱学森.pdf", 434, "钱学森文集", "authored", "钱学森"),
    CatalogEntry("钱学森文集·卷2_2_--_钱学森.pdf", 412, "钱学森文集", "authored", "钱学森"),
    CatalogEntry("钱学森文集·卷3_3_--_钱学森.pdf", 376, "钱学森文集", "authored", "钱学森"),
    CatalogEntry("钱学森文集·卷4_4_--_钱学森.pdf", 419, "钱学森文集", "authored", "钱学森"),
    CatalogEntry("钱学森文集·卷5_5_--_钱学森.pdf", 406, "钱学森文集", "authored", "钱学森"),
    CatalogEntry("钱学森文集·卷6_6_--_钱学森.pdf", 444, "钱学森文集", "authored", "钱学森"),
    CatalogEntry("附件4：钱学森战略科学思想研究综述与文选1107(1).pdf", 518, "战略科学思想", "research"),
    CatalogEntry("附件5：钱学森战略科学思想研究报告2022.1.14(V1).pdf", 103, "战略科学思想", "research", publication_year=2022),
)

EXPECTED_DOCUMENTS = 20
EXPECTED_PAGES = 7492


def validate_catalog(root: Path) -> list[str]:
    errors: list[str] = []
    if len(CATALOG) != EXPECTED_DOCUMENTS:
        errors.append(f"catalog has {len(CATALOG)} documents, expected {EXPECTED_DOCUMENTS}")
    if sum(item.page_count for item in CATALOG) != EXPECTED_PAGES:
        errors.append("catalog page total does not equal 7492")
    for entry in CATALOG:
        path = root / entry.filename
        if not path.is_file():
            errors.append(f"missing: {entry.filename}")
    return errors
