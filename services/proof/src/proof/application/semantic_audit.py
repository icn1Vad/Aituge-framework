from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from proof.application.short_refs import short_ref
from proof.config import Settings
from proof.errors import ProofError
from proof.model_pack import MODEL_PACK_ID_HEADER, current_model_pack_id
from proof.tenant import current_tenant_id


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FindingValidationResult:
    findings: list[dict[str, Any]]
    warning_count: int = 0
    warning_label: str = "模型结果无法解析"

    @property
    def warning_message(self) -> str | None:
        if not self.warning_count:
            return None
        return f"{self.warning_count} 条{self.warning_label}"

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self):
        return iter(self.findings)

    def __getitem__(self, index):
        return self.findings[index]


@dataclass(frozen=True, slots=True)
class IntraConflictValidationResult:
    findings: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    @property
    def warning_message(self) -> str | None:
        if not self.warnings:
            return None
        return f"{self.warning_count} 条模型引用无法解析"


class PolicyAuditService:
    """Coordinate policy summary, semantic review, and conflict review stages."""

    def __init__(
        self,
        settings: Settings,
        repository,
        *,
        intra_conflict_retrieval_service=None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.intra_conflict_retrieval_service = intra_conflict_retrieval_service

    def ensure_dispatched(self, document_id: str) -> dict[str, Any]:
        if not self.settings.semantic_audit_enabled:
            return self.get_state(document_id)

        run = self.repository.get_audit_run_for_document(document_id)
        if run is None:
            run = self.repository.create_audit_run(
                audit_run_id=uuid.uuid4().hex,
                document_id=document_id,
            )
        elif self._has_running_stage(run):
            return self._state(run)
        elif self._all_stages_completed(run):
            return self._state(run)
        else:
            prepared = self.repository.prepare_audit_run_for_dispatch(run["id"])
            if not prepared:
                return self.get_state(document_id)
            run = self.repository.get_audit_run(run["id"])

        try:
            self._dispatch(run)
        except Exception as exc:
            message = self._error_message(exc)
            logger.exception("Unable to dispatch semantic audit %s", run["id"])
            self.repository.mark_audit_failed(run["id"], message)
            self.repository.mark_audit_summary_failed(run["id"], message)
            self._mark_conflict_failed(run["id"], message)
            self._mark_intra_conflict_failed(run["id"], message)
        return self.get_state(document_id)

    @staticmethod
    def _has_running_stage(run: dict[str, Any]) -> bool:
        return any(
            status == "running"
            for status in (
                run.get("status"),
                run.get("summary_status"),
                run.get("conflict_status"),
                run.get("intra_conflict_status"),
            )
        )

    @staticmethod
    def _all_stages_completed(run: dict[str, Any]) -> bool:
        return all(
            status == "completed"
            for status in (
                run.get("status"),
                run.get("summary_status"),
                run.get("conflict_status"),
                run.get("intra_conflict_status"),
            )
        )

    def get_state(self, document_id: str, *, reconcile: bool = True) -> dict[str, Any]:
        run = self.repository.get_audit_run_for_document(document_id)
        if run is None:
            return {
                "status": "disabled" if not self.settings.semantic_audit_enabled else "pending",
                "error_message": None,
            }
        if reconcile and (
            run["status"] == "running"
            or run.get("summary_status") in {"pending", "running"}
            or run.get("conflict_status") in {"pending", "running"}
            or run.get("intra_conflict_status") in {"pending", "running"}
        ):
            self._reconcile_framework_status(run)
            run = self.repository.get_audit_run(run["id"]) or run
        return self._state(run)

    def findings(self, document_id: str) -> list[dict[str, Any]]:
        run = self.repository.get_audit_run_for_document(document_id)
        if run is None or run["status"] != "completed":
            return []
        return self.repository.list_audit_findings(run["id"])

    def summary_state(self, document_id: str) -> dict[str, Any]:
        run = self.repository.get_audit_run_for_document(document_id)
        if run is None:
            status = "disabled" if not self.settings.semantic_audit_enabled else "pending"
            return {"status": status, "error_message": None, "content": None}
        return {
            "status": run.get("summary_status") or "pending",
            "error_message": run.get("summary_error_message"),
            "content": run.get("summary_content"),
        }

    def conflict_state(self, document_id: str) -> dict[str, Any]:
        run = self.repository.get_audit_run_for_document(document_id)
        if run is None:
            status = "disabled" if not self.settings.semantic_audit_enabled else "pending"
            return {"status": status, "error_message": None}
        return {
            "status": run.get("conflict_status") or "pending",
            "error_message": run.get("conflict_error_message"),
        }

    def conflict_findings(self, document_id: str) -> list[dict[str, Any]]:
        run = self.repository.get_audit_run_for_document(document_id)
        if run is None or run.get("conflict_status") != "completed":
            return []
        return self.repository.list_conflict_audit_findings(run["id"])

    def intra_conflict_state(self, document_id: str) -> dict[str, Any]:
        run = self.repository.get_audit_run_for_document(document_id)
        if run is None:
            status = "disabled" if not self.settings.semantic_audit_enabled else "pending"
            return {"status": status, "error_message": None}
        return {
            "status": run.get("intra_conflict_status") or "pending",
            "error_message": run.get("intra_conflict_error_message"),
        }

    def intra_conflict_findings(self, document_id: str) -> list[dict[str, Any]]:
        run = self.repository.get_audit_run_for_document(document_id)
        if run is None or run.get("intra_conflict_status") != "completed":
            return []
        return self.repository.list_intra_conflict_audit_findings(run["id"])

    def accept_result(
        self,
        payload: dict[str, Any],
        *,
        conflict_output_validator: Callable[[dict[str, Any]], FindingValidationResult] | None = None,
        intra_conflict_output_validator: (
            Callable[[dict[str, Any]], IntraConflictValidationResult] | None
        ) = None,
    ) -> dict[str, Any]:
        audit_id = str(payload.get("audit_id") or "").strip()
        task_id = str(payload.get("task_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        stage_id = str(payload.get("stage_id") or "").strip()
        callback_status = str(payload.get("status") or "completed").strip()
        output = payload.get("output")
        run = self.repository.get_audit_run(audit_id)
        if run is None:
            raise ProofError("audit_run_not_found", "Audit run not found.", status_code=404)

        if payload.get("task_type") not in {None, "proof.audit.run"}:
            raise ProofError(
                "invalid_audit_result",
                "Framework task type does not match semantic audit.",
                status_code=422,
            )
        if not task_id or task_id != run.get("framework_task_id"):
            raise ProofError(
                "invalid_audit_result",
                "Framework task ID does not match the audit run.",
                status_code=422,
            )
        if run.get("framework_run_id") and run_id != run["framework_run_id"]:
            raise ProofError(
                "invalid_audit_result",
                "Framework run ID does not match the audit run.",
                status_code=422,
            )
        if stage_id not in {
            "policy_summary", "semantic_audit", "conflict_audit",
            "intra_conflict_audit", "finalize_report", "",
        }:
            raise ProofError(
                "invalid_audit_result",
                f"Unknown Framework pipeline stage: {stage_id}",
                status_code=422,
            )

        try:
            if stage_id == "policy_summary":
                result = self._accept_summary_callback(run, callback_status, output, payload)
            elif stage_id == "semantic_audit":
                result = self._accept_semantic_callback(run, callback_status, output, payload)
            elif stage_id == "conflict_audit":
                result = self._accept_conflict_callback(
                    run,
                    callback_status,
                    output,
                    payload,
                    conflict_output_validator,
                )
            elif stage_id == "intra_conflict_audit":
                result = self._accept_intra_conflict_callback(
                    run,
                    callback_status,
                    output,
                    payload,
                    intra_conflict_output_validator,
                )
            elif stage_id in {"", "finalize_report"}:
                result = self._accept_pipeline_callback(
                    run,
                    callback_status,
                    output,
                    payload,
                    conflict_output_validator,
                    intra_conflict_output_validator,
                )
        except Exception as exc:
            message = self._error_message(exc)
            if stage_id == "policy_summary":
                self.repository.mark_audit_summary_failed(audit_id, message)
            elif stage_id == "conflict_audit":
                self._mark_conflict_failed(audit_id, message)
            elif stage_id == "intra_conflict_audit":
                self._mark_intra_conflict_failed(audit_id, message)
            else:
                self.repository.mark_audit_failed(audit_id, message)
            if isinstance(exc, ProofError):
                raise
            raise ProofError("invalid_audit_result", message, status_code=422) from exc
        return {"audit_id": audit_id, **result}

    def _accept_summary_callback(self, run, status, output, payload) -> dict[str, Any]:
        if status == "failed":
            message = str(payload.get("error_message") or "Policy summary stage failed.")
            self.repository.complete_audit_summary(
                run["id"], self._fallback_summary(), warning_message=message
            )
            return {"stage_id": "policy_summary", "status": "completed"}
        try:
            summary = self._validate_summary(run, output)
            warning_message = None
        except ValueError:
            summary = self._fallback_summary()
            warning_message = "模型摘要无法解析"
        self.repository.complete_audit_summary(
            run["id"], summary, warning_message=warning_message
        )
        return {"stage_id": "policy_summary", "status": "completed"}

    def _accept_semantic_callback(self, run, status, output, payload) -> dict[str, Any]:
        if status == "failed":
            message = str(payload.get("error_message") or "Semantic audit stage failed.")
            self.repository.complete_audit(run["id"], [], warning_message=message)
            return {
                "stage_id": "semantic_audit",
                "status": "completed",
                "finding_count": 0,
            }
        validation = self._validate_output(run, output)
        self.repository.complete_audit(
            run["id"], validation.findings, warning_message=validation.warning_message
        )
        return {
            "stage_id": "semantic_audit",
            "status": "completed",
            "finding_count": len(validation.findings),
        }

    def _accept_conflict_callback(
        self,
        run,
        status,
        output,
        payload,
        validator,
    ) -> dict[str, Any]:
        if status == "failed":
            message = str(payload.get("error_message") or "Conflict audit stage failed.")
            self.repository.complete_conflict_audit(
                run["id"], [], warning_message=message
            )
            return {
                "stage_id": "conflict_audit",
                "status": "completed",
                "finding_count": 0,
            }
        if validator is None:
            raise ValueError("Conflict result validator is not configured.")
        validation = validator({**payload, "output": output})
        self.repository.complete_conflict_audit(
            run["id"], validation.findings, warning_message=validation.warning_message
        )
        return {
            "stage_id": "conflict_audit",
            "status": "completed",
            "finding_count": len(validation.findings),
        }

    def _accept_intra_conflict_callback(
        self,
        run,
        status,
        output,
        payload,
        validator,
    ) -> dict[str, Any]:
        if status == "failed":
            message = str(payload.get("error_message") or "Intra-policy conflict audit stage failed.")
            self.repository.complete_intra_conflict_audit(
                run["id"], [], warning_message=message
            )
            return {
                "stage_id": "intra_conflict_audit",
                "status": "completed",
                "finding_count": 0,
                "warning_count": 0,
                "warning_message": message,
            }
        if validator is None:
            raise ValueError("Intra-policy conflict result validator is not configured.")
        validation = validator({**payload, "output": output})
        self.repository.complete_intra_conflict_audit(
            run["id"],
            validation.findings,
            warnings=validation.warnings,
            warning_message=validation.warning_message,
        )
        return {
            "stage_id": "intra_conflict_audit",
            "status": "completed",
            "finding_count": len(validation.findings),
            "warning_count": validation.warning_count,
            "warning_message": validation.warning_message,
        }

    @staticmethod
    def _fallback_summary() -> dict[str, Any]:
        return {
            "plain_summary": "模型摘要无法解析。",
            "purpose": None,
            "scope": [],
            "concerned_roles": [],
            "key_rules": [],
        }

    def _accept_pipeline_callback(
        self, run, status, output, payload, conflict_validator, intra_conflict_validator
    ) -> dict[str, Any]:
        if status == "failed":
            message = str(payload.get("error_message") or "Policy review pipeline failed.")
            self.repository.mark_audit_failed(run["id"], message)
            self.repository.mark_audit_summary_failed(run["id"], message)
            self._mark_conflict_failed(run["id"], message)
            self._mark_intra_conflict_failed(run["id"], message)
            return {"status": "failed"}
        if not isinstance(output, dict):
            raise ValueError("Framework final callback output must be an object.")
        artifacts = output.get("artifacts")
        stages = output.get("stages")
        if not isinstance(artifacts, dict) or not isinstance(stages, dict):
            raise ValueError("Framework final callback is missing artifacts or stages.")

        current = self.repository.get_audit_run(run["id"]) or run
        summary_output = artifacts.get("policy_summary")
        summary_stage = stages.get("policy_summary") or {}
        if current.get("summary_status") != "completed":
            if summary_output is not None:
                try:
                    summary = self._validate_summary(run, summary_output)
                    warning_message = None
                except ValueError:
                    summary = self._fallback_summary()
                    warning_message = "模型摘要无法解析"
                self.repository.complete_audit_summary(
                    run["id"], summary, warning_message=warning_message
                )
            elif summary_stage.get("status") in {"failed", "cancelled"}:
                self.repository.complete_audit_summary(
                    run["id"],
                    self._fallback_summary(),
                    warning_message=str(
                        summary_stage.get("error_message") or "Policy summary stage failed."
                    ),
                )
            else:
                raise ValueError("Framework final callback is missing the policy summary artifact.")

        current = self.repository.get_audit_run(run["id"]) or current
        semantic_output = artifacts.get("semantic_audit")
        semantic_stage = stages.get("semantic_audit") or {}
        if current.get("status") != "completed":
            if semantic_output is not None:
                validation = self._validate_output(run, semantic_output)
                self.repository.complete_audit(
                    run["id"],
                    validation.findings,
                    warning_message=validation.warning_message,
                )
            elif semantic_stage.get("status") in {"failed", "cancelled"}:
                self.repository.complete_audit(
                    run["id"],
                    [],
                    warning_message=str(
                        semantic_stage.get("error_message") or "Semantic audit stage failed."
                    ),
                )
            else:
                raise ValueError("Framework final callback is missing the semantic audit artifact.")

        current = self.repository.get_audit_run(run["id"]) or current
        conflict_output = artifacts.get("conflict_audit")
        conflict_stage = stages.get("conflict_audit") or {}
        if current.get("conflict_status") != "completed":
            if conflict_output is not None:
                if conflict_validator is None:
                    raise ValueError("Conflict result validator is not configured.")
                validation = conflict_validator(
                    {
                        "task_type": "proof.audit.run",
                        "audit_id": run["id"],
                        "output": conflict_output,
                    }
                )
                self.repository.complete_conflict_audit(
                    run["id"],
                    validation.findings,
                    warning_message=validation.warning_message,
                )
            elif conflict_stage.get("status") in {"failed", "cancelled"}:
                self.repository.complete_conflict_audit(
                    run["id"],
                    [],
                    warning_message=str(
                        conflict_stage.get("error_message") or "Conflict audit stage failed."
                    ),
                )
            else:
                raise ValueError("Framework final callback is missing the conflict audit artifact.")

        current = self.repository.get_audit_run(run["id"]) or current
        intra_output = artifacts.get("intra_conflict_audit")
        intra_stage = stages.get("intra_conflict_audit") or {}
        if current.get("intra_conflict_status") != "completed":
            if intra_output is not None:
                if intra_conflict_validator is None:
                    raise ValueError("Intra-policy conflict result validator is not configured.")
                validation = intra_conflict_validator(
                    {
                        "task_type": "proof.audit.run",
                        "audit_id": run["id"],
                        "output": intra_output,
                    }
                )
                self.repository.complete_intra_conflict_audit(
                    run["id"],
                    validation.findings,
                    warnings=validation.warnings,
                    warning_message=validation.warning_message,
                )
            elif intra_stage.get("status") in {"failed", "cancelled"}:
                self.repository.complete_intra_conflict_audit(
                    run["id"],
                    [],
                    warning_message=str(
                        intra_stage.get("error_message")
                        or "Intra-policy conflict audit stage failed."
                    ),
                )
            else:
                raise ValueError(
                    "Framework final callback is missing the intra-policy conflict artifact."
                )
        final = self.repository.get_audit_run(run["id"]) or current
        return {"status": final["status"]}

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
        if self.intra_conflict_retrieval_service is not None:
            self.intra_conflict_retrieval_service.prepare(
                run["id"], run["document_id"], units
            )
        semantic_items = self._build_semantic_items(run["id"], units)
        conflict_items = self._build_conflict_items(run["id"], units)
        intra_conflict_items = self._build_intra_conflict_items(run["id"], units)
        headers = self._headers()
        dispatch_key = uuid.uuid4().hex
        create_payload = {
            "task_type": "proof.audit.run",
            "title": "Proof policy review",
            "model_pack_id": current_model_pack_id() or None,
            "input_payload": {
                "audit_id": run["id"],
                "document_id": run["document_id"],
                "summary_chunks": [self._target(unit) for unit in units],
                "summary_max_chars": self.settings.summary_max_chars,
                "semantic_items": semantic_items,
                "conflict_items": conflict_items,
                "intra_conflict_items": intra_conflict_items,
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

    def _build_semantic_items(
        self,
        audit_id: str,
        units: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for unit in units:
            text_chars = len(unit["text"])
            full = len(current) >= self.settings.audit_batch_max_chunks
            over_budget = (
                bool(current)
                and current_chars + text_chars > self.settings.audit_batch_max_chars
            )
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
                    {**self._target(unit), "ref": short_ref("T", target_index)}
                    for target_index, unit in enumerate(batch, start=1)
                ],
            }
            for index, batch in enumerate(batches, start=1)
        ]

    def _build_conflict_items(
        self,
        audit_id: str,
        units: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": f"{audit_id}:conflict:{index:04d}",
                "audit_id": audit_id,
                "check": "conflict",
                "targets": [
                    {
                        "id": unit["id"],
                        "unit_id": unit["id"],
                        "text": unit["text"],
                        "clause_no": unit.get("clause_no_raw") or "",
                        "heading_path": unit.get("heading_path") or [],
                    }
                ],
            }
            for index, unit in enumerate(units, start=1)
        ]

    def _build_intra_conflict_items(
        self,
        audit_id: str,
        units: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": f"{audit_id}:intra-conflict:{index:04d}",
                "audit_id": audit_id,
                "check": "conflict",
                "targets": [
                    {
                        "id": unit["id"],
                        "unit_id": unit["id"],
                        "text": unit["text"],
                        "clause_no": unit.get("clause_no_raw") or "",
                        "heading_path": unit.get("heading_path") or [],
                    }
                ],
            }
            for index, unit in enumerate(units, start=1)
        ]

    @staticmethod
    def _target(unit: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": unit["id"],
            "text": unit["text"],
            "clause_no": unit.get("clause_no_raw") or "",
            "clause_ordinal": unit.get("clause_ordinal"),
            "heading_path": unit.get("heading_path") or [],
        }

    def _validate_output(
        self, run: dict[str, Any], output: Any
    ) -> FindingValidationResult:
        if not isinstance(output, dict):
            raise ValueError("Framework callback output must be an object.")
        summary = output.get("summary")
        items = output.get("items")
        if not isinstance(summary, dict) or not isinstance(items, list):
            raise ValueError("Framework callback is missing batch summary or items.")

        units = self.repository.get_document_units(run["document_id"])
        expected_ids = {str(item["id"]) for item in units}
        covered_ids: list[str] = []
        normalized: list[dict[str, str]] = []
        warning_count = 0

        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Each semantic audit item must be an object.")
            item_input = item.get("input") or {}
            targets = item_input.get("targets") if isinstance(item_input, dict) else None
            if (
                not isinstance(targets, list)
                or not 1 <= len(targets) <= 8
                or any(not isinstance(target, dict) for target in targets)
            ):
                raise ValueError("Each semantic audit item must contain one to eight target chunks.")

            ref_to_id: dict[str, str] = {}
            for index, target in enumerate(targets, start=1):
                target_ref = str(target.get("ref") or "").strip().upper()
                target_id = str(target.get("id") or "").strip()
                if target_ref != short_ref("T", index):
                    raise ValueError("Semantic target refs must be sequential T01 through T08.")
                if not target_id or target_id not in expected_ids:
                    raise ValueError("A semantic audit item targets an unknown document chunk.")
                ref_to_id[target_ref] = target_id
                covered_ids.append(target_id)

            if item.get("status") != "succeeded":
                warning_count += 1
                continue
            result_wrapper = item.get("result") or {}
            result = result_wrapper.get("result") if isinstance(result_wrapper, dict) else None
            findings = result.get("findings") if isinstance(result, dict) else None
            if not isinstance(findings, list):
                warning_count += 1
                continue

            finding_refs: set[str] = set()
            for finding in findings:
                try:
                    expected_fields = {"target_ref", "category", "problem", "suggestion"}
                    if not isinstance(finding, dict) or set(finding) != expected_fields:
                        raise ValueError("A semantic finding has an invalid structure.")
                    target_ref = str(finding.get("target_ref") or "").strip().upper()
                    category = str(finding.get("category") or "").strip()
                    problem = str(finding.get("problem") or "").strip()
                    suggestion = str(finding.get("suggestion") or "").strip()
                    if target_ref not in ref_to_id:
                        raise ValueError("Semantic finding targets a ref outside its item.")
                    if target_ref in finding_refs:
                        raise ValueError("A target returned more than one semantic finding.")
                    if category not in {"semantic_ambiguity", "executability_gap"}:
                        raise ValueError("Semantic finding has an invalid category.")
                    if not all((problem, suggestion)):
                        raise ValueError("Semantic finding fields must not be blank.")
                except ValueError:
                    warning_count += 1
                    continue
                finding_refs.add(target_ref)
                normalized.append(
                    {
                        "id": ref_to_id[target_ref],
                        "category": category,
                        "problem": problem,
                        "suggestion": suggestion,
                    }
                )

        if len(covered_ids) != len(set(covered_ids)) or set(covered_ids) != expected_ids:
            raise ValueError("Semantic audit items did not cover every chunk exactly once.")
        return FindingValidationResult(
            findings=normalized,
            warning_count=warning_count,
            warning_label="模型引用无法解析",
        )

    def _validate_summary(self, run: dict[str, Any], output: Any) -> dict[str, Any]:
        if not isinstance(output, dict):
            raise ValueError("Policy summary output must be an object.")
        allowed = {
            "plain_summary", "purpose", "scope", "concerned_roles",
            "key_rules",
        }
        if set(output) - allowed:
            raise ValueError(f"Policy summary contains unknown fields: {sorted(set(output) - allowed)}")
        if not isinstance(output.get("plain_summary"), str) or not output["plain_summary"].strip():
            raise ValueError("Policy summary plain_summary must not be blank.")
        for field in ("scope", "concerned_roles", "key_rules"):
            if not isinstance(output.get(field), list):
                raise ValueError(f"Policy summary {field} must be an array.")
        if output.get("purpose") is not None and (
            not isinstance(output["purpose"], str) or not output["purpose"].strip()
        ):
            raise ValueError("Policy summary purpose must be null or a non-blank string.")
        for field in ("scope", "key_rules"):
            for index, item in enumerate(output[field]):
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(f"Policy summary {field}[{index}] must be a non-blank string.")
        for index, role in enumerate(output["concerned_roles"]):
            path = f"summary.concerned_roles[{index}]"
            expected = {"role", "summary"}
            if not isinstance(role, dict) or set(role) != expected:
                raise ValueError(f"{path} has an invalid structure.")
            if not isinstance(role["role"], str) or not role["role"].strip():
                raise ValueError(f"{path}.role must not be blank.")
            if not isinstance(role["summary"], str) or not role["summary"].strip():
                raise ValueError(f"{path}.summary must not be blank.")
        return output

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
            if run["status"] == "running":
                self.repository.mark_audit_failed(
                    run["id"], "Framework task ended without delivering a valid semantic audit result."
                )
            if run.get("summary_status") in {"pending", "running"}:
                self.repository.mark_audit_summary_failed(
                    run["id"], "Framework task ended without delivering a valid policy summary result."
                )
            if run.get("conflict_status") in {"pending", "running"}:
                self._mark_conflict_failed(
                    run["id"], "Framework task ended without delivering a valid conflict audit result."
                )
            if run.get("intra_conflict_status") in {"pending", "running"}:
                self._mark_intra_conflict_failed(
                    run["id"], "Framework task ended without delivering a valid intra-policy conflict audit result."
                )

    def _headers(self) -> dict[str, str]:
        headers = {
            "X-User-ID": self.settings.framework_user_id,
            "X-Tenant-ID": current_tenant_id(),
        }
        model_pack_id = current_model_pack_id()
        if model_pack_id:
            headers[MODEL_PACK_ID_HEADER] = model_pack_id
        return headers

    def _mark_conflict_failed(self, audit_id: str, message: str) -> None:
        handler = getattr(self.repository, "mark_conflict_audit_failed", None)
        if handler is not None:
            handler(audit_id, message)

    def _mark_intra_conflict_failed(self, audit_id: str, message: str) -> None:
        handler = getattr(self.repository, "mark_intra_conflict_audit_failed", None)
        if handler is not None:
            handler(audit_id, message)

    def _state(self, run: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": run["id"],
            "status": run["status"],
            "error_message": run.get("error_message"),
            "framework_task_id": run.get("framework_task_id"),
            "framework_run_id": run.get("framework_run_id"),
            "policy_summary": {
                "status": run.get("summary_status") or "pending",
                "error_message": run.get("summary_error_message"),
                "content": run.get("summary_content"),
            },
            "conflict_audit": {
                "status": run.get("conflict_status") or "pending",
                "error_message": run.get("conflict_error_message"),
            },
            "intra_conflict_audit": {
                "status": run.get("intra_conflict_status") or "pending",
                "error_message": run.get("intra_conflict_error_message"),
            },
        }

    @staticmethod
    def _error_message(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            detail = exc.response.text.strip()[:1000]
            return f"Framework returned HTTP {exc.response.status_code}: {detail}"
        return str(exc).strip()[:2000] or exc.__class__.__name__
