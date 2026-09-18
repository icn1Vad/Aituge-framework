"""Deterministic reimbursement checks over server-bound attachment facts.

This is a service-side tool core, not an agent-callable endpoint. The caller
must resolve document ownership, versions and extraction provenance first.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import re
from typing import Any, Mapping

from .audit import AuditContext, AuditRecord, AuditResult, AuditRunner


_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_MD5 = re.compile(r"[0-9a-fA-F]{32}\Z")


def _documents(context: AuditContext) -> list[Mapping[str, Any]]:
    documents = context.facts.get("documents")
    if not isinstance(documents, list) or any(not isinstance(item, Mapping) for item in documents):
        raise ValueError("documents must be a list of server-bound facts")
    return documents


def check_duplicate_invoices(context: AuditContext) -> list[AuditRecord]:
    """Check this submission, and explicitly leave external history unresolved."""
    documents = _documents(context)
    if not documents:
        return [AuditRecord("DUPLICATE_INVOICE", "PENDING", "尚无可核对的票据")]

    identifiers: dict[tuple[str, str], list[str]] = defaultdict(list)
    invalid: set[str] = set()
    seen_ids: set[str] = set()
    for document in documents:
        document_id = str(document.get("document_id") or "").strip()
        sha256 = str(document.get("file_sha256") or "").strip().lower()
        md5 = str(document.get("file_md5") or "").strip().lower()
        number = str(document.get("invoice_number") or "").strip().upper()
        if (not document_id or document_id in seen_ids
                or (sha256 and not _SHA256.fullmatch(sha256))
                or (md5 and not _MD5.fullmatch(md5))):
            invalid.add(document_id)
        seen_ids.add(document_id)
        if sha256 and _SHA256.fullmatch(sha256):
            identifiers[("sha256", sha256)].append(document_id)
        if md5 and _MD5.fullmatch(md5):
            identifiers[("md5", md5)].append(document_id)
        if number:
            identifiers[("invoice", number)].append(document_id)
        if not sha256 and not md5 and not number:
            invalid.add(document_id)

    duplicates = {document_id for ids in identifiers.values() if len(set(ids)) > 1 for document_id in ids}
    history_complete = context.facts.get("invoice_history_complete") is True
    rows: list[AuditRecord] = []
    for document in documents:
        document_id = str(document.get("document_id") or "").strip()
        if document_id in invalid:
            rows.append(AuditRecord("DUPLICATE_INVOICE", "PENDING", "票据编号、文件版本或文件身份不完整，需核实", document_id))
        elif document_id in duplicates:
            rows.append(AuditRecord("DUPLICATE_INVOICE", "RISK", "本单内发现相同票据编号或相同文件，需人工核实", document_id))
        elif not history_complete:
            rows.append(AuditRecord("DUPLICATE_INVOICE", "PENDING", "本单内未发现重复；历史票据台账尚未核对", document_id))
        else:
            rows.append(AuditRecord("DUPLICATE_INVOICE", "PASS", "本单及已提供历史台账未发现相同编号或文件", document_id))
    return rows


def check_same_day_lodging(context: AuditContext) -> list[AuditRecord]:
    documents = [item for item in _documents(context) if item.get("category") == "lodging"]
    if not documents:
        return [AuditRecord("SAME_DAY_LODGING", "PENDING", "尚无住宿票据可核对")]

    by_day: dict[tuple[str, str], set[str]] = defaultdict(set)
    invalid: set[str] = set()
    for document in documents:
        document_id = str(document.get("document_id") or "").strip()
        traveler = str(document.get("traveler") or "").strip()
        day = str(document.get("occurrence_date") or "").strip()
        try:
            date.fromisoformat(day)
        except ValueError:
            invalid.add(document_id)
        if not document_id or not traveler or document_id in invalid:
            invalid.add(document_id)
            continue
        by_day[(traveler, day)].add(document_id)

    rows: list[AuditRecord] = []
    for document in documents:
        document_id = str(document.get("document_id") or "").strip()
        traveler = str(document.get("traveler") or "").strip()
        day = str(document.get("occurrence_date") or "").strip()
        if document_id in invalid:
            rows.append(AuditRecord("SAME_DAY_LODGING", "PENDING", "住宿日期或出差人未核实", document_id))
        elif len(by_day[(traveler, day)]) > 1:
            rows.append(AuditRecord("SAME_DAY_LODGING", "RISK", "同一出差人同日有多张住宿票据，请核实是否合理", document_id))
        else:
            rows.append(AuditRecord("SAME_DAY_LODGING", "PASS", "本单内未发现该出差人同日多张住宿票据", document_id))
    return rows


def review_reimbursement_facts(facts: Mapping[str, Any]) -> AuditResult:
    """Run independent checks; a failed or incomplete one never becomes PASS."""
    runner = AuditRunner()
    runner.register("DUPLICATE_INVOICE", check_duplicate_invoices)
    runner.register("SAME_DAY_LODGING", check_same_day_lodging)
    return runner.run(AuditContext(facts))
