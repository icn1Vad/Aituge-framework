from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from proof.config import Settings
from proof.errors import ProofError


logger = logging.getLogger(__name__)


class SemanticAuditService:
    """Dispatch semantic review batches and validate their trusted callback."""

    def __init__(self, settings: Settings, repository) -> None:
        self.settings = settings
        self.repository = repository

    def ensure_dispatched(self, document_id: str) -> dict[str, Any]:
        if not self.settings.semantic_audit_enabled:
            return self.get_state(document_id)

        run = self.repository.get_audit_run_for_document(document_id)
        if run is None:
            run = self.repository.create_audit_run(
                audit_run_id=uuid.uuid4().hex,
                document_id=document_id,
            )
        elif run["status"] in {"running", "completed"}:
            return self._state(run)
        elif run["status"] == "failed":
            if not self.repository.reset_failed_audit_run(run["id"]):
                return self.get_state(document_id)
            run = self.repository.get_audit_run(run["id"])

        try:
            self._dispatch(run)
        except Exception as exc:
            message = self._error_message(exc)
            logger.exception("Unable to dispatch semantic audit %s", run["id"])
            self.repository.mark_audit_failed(run["id"], message)
        return self.get_state(document_id)

    def get_state(self, document_id: str, *, reconcile: bool = True) -> dict[str, Any]:
        run = self.repository.get_audit_run_for_document(document_id)
        if run is None:
            return {
                "status": "disabled" if not self.settings.semantic_audit_enabled else "pending",
                "error_message": None,
            }
        if reconcile and run["status"] == "running":
            self._reconcile_framework_status(run)
            run = self.repository.get_audit_run(run["id"]) or run
        return self._state(run)

    def findings(self, document_id: str) -> list[dict[str, Any]]:
        run = self.repository.get_audit_run_for_document(document_id)
        if run is None or run["status"] != "completed":
            return []
        return self.repository.list_audit_findings(run["id"])

    def accept_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        audit_id = str(payload.get("audit_id") or "").strip()
        task_id = str(payload.get("task_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        output = payload.get("output")
        run = self.repository.get_audit_run(audit_id)
        if run is None:
            raise ProofError("audit_run_not_found", "Audit run not found.", status_code=404)

        try:
            if payload.get("task_type") not in {None, "proof.audit.run"}:
                raise ValueError("Framework task type does not match semantic audit.")
            if not task_id or task_id != run.get("framework_task_id"):
                raise ValueError("Framework task ID does not match the audit run.")
            if run.get("framework_run_id") and run_id != run["framework_run_id"]:
                raise ValueError("Framework run ID does not match the audit run.")
            findings = self._validate_output(run, output)
            self.repository.complete_audit(audit_id, findings)
        except Exception as exc:
            message = self._error_message(exc)
            self.repository.mark_audit_failed(audit_id, message)
            if isinstance(exc, ProofError):
                raise
            raise ProofError("invalid_audit_result", message, status_code=422) from exc
        return {"audit_id": audit_id, "status": "completed", "finding_count": len(findings)}

    def _dispatch(self, run: dict[str, Any]) -> None:
        base_url = self.settings.framework_base_url.strip().rstrip("/")
        if not base_url:
            raise ValueError("PROOF_FRAMEWORK_BASE_URL is not configured.")

        units = self.repository.get_document_units(run["document_id"])
        if not units:
            raise ValueError("The document has no chunks to audit.")
        too_long = [item["id"] for item in units if len(item["text"]) > self.settings.audit_max_chunk_chars]
        if too_long:
            raise ValueError(f"Chunks exceed PROOF_AUDIT_MAX_CHUNK_CHARS: {', '.join(too_long)}")
        items = self._build_batches(run["id"], units)
        headers = self._headers()
        dispatch_key = uuid.uuid4().hex
        create_payload = {
            "task_type": "proof.audit.run",
            "title": "Proof semantic policy audit",
            "input_payload": {
                "audit_id": run["id"],
                "document_id": run["document_id"],
                "items": items,
                "max_concurrency": self.settings.audit_max_concurrency,
                "failure_policy": "fail_fast",
                "retry_per_item": 1,
            },
            "stream": False,
        }
        with httpx.Client(base_url=base_url, timeout=20) as client:
            response = client.post(
                "/task-manager/tasks",
                json=create_payload,
                headers={**headers, "Idempotency-Key": f"proof-audit-task:{dispatch_key}"},
            )
            response.raise_for_status()
            task_id = str(response.json()["task"]["id"])
            self.repository.mark_audit_task_created(run["id"], task_id)
            response = client.post(
                f"/task-manager/tasks/{task_id}/runs",
                json={"stream": False},
                headers={**headers, "Idempotency-Key": f"proof-audit-run:{dispatch_key}"},
            )
            response.raise_for_status()
            framework_run_id = str(response.json()["run_id"])
        self.repository.mark_audit_running(
            run["id"],
            framework_task_id=task_id,
            framework_run_id=framework_run_id,
        )

    def _build_batches(self, audit_id: str, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for unit in units:
            text_chars = len(unit["text"])
            full = len(current) >= self.settings.audit_batch_max_chunks
            over_budget = bool(current) and current_chars + text_chars > self.settings.audit_batch_max_chars
            if full or over_budget:
                batches.append(current)
                current = []
                current_chars = 0
            current.append(unit)
            current_chars += text_chars
        if current:
            batches.append(current)

        return [
            {
                "id": f"{audit_id}:semantic:{index:04d}",
                "audit_id": audit_id,
                "check": "semantic",
                "targets": [
                    {
                        "id": unit["id"],
                        "text": unit["text"],
                        "clause_no": unit.get("clause_no_raw") or "",
                        "clause_ordinal": unit.get("clause_ordinal"),
                        "heading_path": unit.get("heading_path") or [],
                    }
                    for unit in batch
                ],
            }
            for index, batch in enumerate(batches, start=1)
        ]

    def _validate_output(self, run: dict[str, Any], output: Any) -> list[dict[str, str]]:
        if not isinstance(output, dict):
            raise ValueError("Framework callback output must be an object.")
        summary = output.get("summary")
        items = output.get("items")
        if not isinstance(summary, dict) or not isinstance(items, list):
            raise ValueError("Framework callback is missing batch summary or items.")
        if int(summary.get("failed") or 0) or any(item.get("status") != "succeeded" for item in items):
            raise ValueError("At least one semantic audit batch failed.")

        units = self.repository.get_document_units(run["document_id"])
        unit_by_id = {item["id"]: item for item in units}
        expected_ids = set(unit_by_id)
        covered_ids: list[str] = []
        normalized: list[dict[str, str]] = []
        finding_ids: set[str] = set()

        for item in items:
            item_input = item.get("input") or {}
            targets = item_input.get("targets") if isinstance(item_input, dict) else None
            if not isinstance(targets, list):
                raise ValueError("A batch callback is missing its target chunks.")
            target_ids = [str(target.get("id") or "") for target in targets if isinstance(target, dict)]
            covered_ids.extend(target_ids)

            result_wrapper = item.get("result") or {}
            result = result_wrapper.get("result") if isinstance(result_wrapper, dict) else None
            findings = result.get("findings") if isinstance(result, dict) else None
            if not isinstance(findings, list):
                raise ValueError("A batch result must contain a findings array.")
            for finding in findings:
                if not isinstance(finding, dict):
                    raise ValueError("Each semantic finding must be an object.")
                finding_id = str(finding.get("id") or "").strip()
                quote = str(finding.get("quote") or "").strip()
                problem = str(finding.get("problem") or "").strip()
                suggestion = str(finding.get("suggestion") or "").strip()
                if not all((finding_id, quote, problem, suggestion)):
                    raise ValueError("Semantic finding fields must not be blank.")
                if finding_id not in target_ids:
                    raise ValueError(f"Finding targets a chunk outside its batch: {finding_id}")
                if finding_id in finding_ids:
                    raise ValueError(f"A chunk returned more than one finding: {finding_id}")
                resolved_quote = self._resolve_quote(
                    unit_by_id[finding_id]["text"],
                    quote,
                )
                if resolved_quote is None:
                    raise ValueError(f"Finding quote cannot be located in chunk: {finding_id}")
                finding_ids.add(finding_id)
                normalized.append(
                    {
                        "id": finding_id,
                        "quote": resolved_quote,
                        "problem": problem,
                        "suggestion": suggestion,
                    }
                )

        if len(covered_ids) != len(set(covered_ids)) or set(covered_ids) != expected_ids:
            raise ValueError("Semantic audit batches did not cover every chunk exactly once.")
        return normalized

    @staticmethod
    def _resolve_quote(source: str, quote: str) -> str | None:
        """Locate a model quote and return the exact source-backed substring.

        PDF extraction commonly inserts line breaks or layout spaces that a model
        removes when quoting. Exact quotes retain the previous behavior. Otherwise,
        match after removing Unicode whitespace, require a unique occurrence, and
        map that occurrence back to the original source span.
        """
        if quote in source:
            return quote

        source_chars: list[str] = []
        source_positions: list[int] = []
        for index, character in enumerate(source):
            if character.isspace():
                continue
            source_chars.append(character)
            source_positions.append(index)

        normalized_quote = "".join(character for character in quote if not character.isspace())
        if not normalized_quote:
            return None
        normalized_source = "".join(source_chars)

        first = normalized_source.find(normalized_quote)
        if first < 0:
            return None
        if normalized_source.find(normalized_quote, first + 1) >= 0:
            return None

        start = source_positions[first]
        end = source_positions[first + len(normalized_quote) - 1] + 1
        return source[start:end]

    def _reconcile_framework_status(self, run: dict[str, Any]) -> None:
        task_id = str(run.get("framework_task_id") or "").strip()
        base_url = self.settings.framework_base_url.strip().rstrip("/")
        if not task_id or not base_url:
            return
        try:
            with httpx.Client(base_url=base_url, timeout=5) as client:
                response = client.get(f"/task-manager/tasks/{task_id}", headers=self._headers())
                response.raise_for_status()
                status = str(response.json()["task"]["status"])
        except Exception:
            return
        if status in {"failed", "cancelled", "succeeded"}:
            self.repository.mark_audit_failed(
                run["id"],
                "Framework task ended without delivering a valid semantic audit result.",
            )

    def _headers(self) -> dict[str, str]:
        return {
            "X-User-ID": self.settings.framework_user_id,
            "X-Tenant-ID": self.settings.framework_tenant_id,
        }

    @staticmethod
    def _state(run: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": run["id"],
            "status": run["status"],
            "error_message": run.get("error_message"),
            "framework_task_id": run.get("framework_task_id"),
            "framework_run_id": run.get("framework_run_id"),
        }

    @staticmethod
    def _error_message(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            detail = exc.response.text.strip()[:1000]
            return f"Framework returned HTTP {exc.response.status_code}: {detail}"
        return str(exc).strip()[:2000] or exc.__class__.__name__
