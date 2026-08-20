from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any


DATE = re.compile(r"(?P<year>(?:18|19|20)\d{2})年(?:(?P<month>\d{1,2})月)?(?:(?P<day>\d{1,2})日)?")
SENTENCE = re.compile(r"[^。！？\n]{0,100}(?:18|19|20)\d{2}年[^。！？\n]{0,180}[。！？]?")
PRINCIPLE_KEYWORDS = (
    "系统工程", "系统科学", "工程控制论", "总体设计", "开放的复杂巨系统",
    "综合集成", "大成智慧", "人才培养", "科学精神", "工程实践",
)


def extract_cards(chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts: list[dict[str, Any]] = []
    principles: list[dict[str, Any]] = []
    seen_facts: set[str] = set()
    seen_principles: set[str] = set()
    for chunk in chunks:
        source_kind = str(chunk.get("source_kind") or "")
        direct_source = source_kind in {"authored", "letters"}
        for sentence in SENTENCE.findall(str(chunk["content"]))[:4]:
            match = DATE.search(sentence)
            if not match:
                continue
            value = sentence.strip()
            key = re.sub(r"\s+", "", value)
            if key in seen_facts:
                continue
            seen_facts.add(key)
            month, day = match.group("month"), match.group("day")
            event_date = None
            if month and day:
                try:
                    event_date = date(int(match.group("year")), int(month), int(day)).isoformat()
                except ValueError:
                    event_date = None
            facts.append({
                "id": stable_id("fact", chunk["id"], value), "predicate": "时间与事件",
                "value": value, "event_date": event_date, "event_year": int(match.group("year")),
                "chunk_id": chunk["id"], "page_start": chunk["page_start"], "page_end": chunk["page_end"],
                "source_count": 1, "confidence": 0.92 if direct_source else 0.72,
                "card_status": "auto_published" if direct_source else "candidate",
            })
        for keyword in PRINCIPLE_KEYWORDS:
            if keyword not in str(chunk["content"]):
                continue
            summary = str(chunk["content"]).strip()
            key = keyword + re.sub(r"\s+", "", summary[:120])
            if key in seen_principles:
                continue
            seen_principles.add(key)
            principles.append({
                "id": stable_id("principle", chunk["id"], key), "title": keyword,
                "summary": summary, "application": f"用于分析与{keyword}相关的目标、要素、关系和实施路径。",
                "constraints": "这是根据来源归纳的方法卡，不作为钱学森原话直接引用。",
                "chunk_id": chunk["id"], "page_start": chunk["page_start"], "page_end": chunk["page_end"],
                "confidence": 0.9 if direct_source else 0.78, "card_status": "auto_published",
            })
            break
    return facts, principles


def stable_id(kind: str, chunk_id: str, value: str) -> str:
    return f"qxs-{kind}-" + hashlib.sha256(f"{chunk_id}|{value}".encode()).hexdigest()[:24]
