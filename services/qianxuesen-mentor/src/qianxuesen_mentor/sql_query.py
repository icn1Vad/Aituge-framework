from __future__ import annotations

import json
import re
from typing import Any

from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.errors import QianXuesenError
from qianxuesen_mentor.repository import Repository


READ_START = re.compile(r"^(?:select|with)\b", re.I)
MULTI = re.compile(r";")
MUTATION = re.compile(r"\b(insert|update|delete|drop|alter|create|grant|revoke|copy|call|do|truncate)\b", re.I)
RELATION = re.compile(r"\b(qxs_[a-z0-9_]+)\b", re.I)
ALLOWED = {"qxs_sql_document_v", "qxs_sql_fact_v", "qxs_sql_principle_v"}


class SqlQueryService:
    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository

    def execute(self, question: str, sql: str) -> dict[str, Any]:
        normalized = normalize_sql(sql)
        result = self.repository.execute_read_query(
            normalized, timeout_ms=int(self.settings.sql_statement_timeout_seconds * 1000),
            row_limit=self.settings.sql_max_rows,
        )
        rows = result["rows"]
        truncated = len(rows) > self.settings.sql_max_rows
        rows = rows[:self.settings.sql_max_rows]
        while rows and len(json.dumps(rows, ensure_ascii=False, default=str)) > self.settings.sql_max_response_chars:
            rows.pop()
            truncated = True
        return {"question": question.strip(), "sql": normalized, "columns": result["columns"],
                "rows": rows, "row_count": len(rows), "truncated": truncated,
                "execution_ms": result["execution_ms"]}


def normalize_sql(sql: str) -> str:
    value = sql.strip().removesuffix(";").strip()
    if not value or not READ_START.match(value):
        raise QianXuesenError("sql_read_only_required", "SQL 必须是 SELECT 或 WITH 查询", status_code=422)
    if MULTI.search(value) or MUTATION.search(value):
        raise QianXuesenError("sql_read_only_required", "只允许执行一条只读查询", status_code=422)
    relations = {match.lower() for match in RELATION.findall(value)}
    forbidden = sorted(relations - ALLOWED)
    if forbidden or not relations:
        raise QianXuesenError("sql_relation_not_allowed", "只能查询钱学森知识库公开语义视图",
                              status_code=422, details={"forbidden_relations": forbidden})
    return value
