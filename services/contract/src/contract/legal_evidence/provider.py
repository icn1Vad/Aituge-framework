from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Protocol

from contract.application.idempotency import canonical_json
from contract.config import Settings
from contract.legal_evidence.binding import LegalEvidenceCheckBinder
from contract.legal_evidence.embedding import OpenAICompatibleLegalEmbeddingProvider
from contract.legal_evidence.models import (
    FrozenLegalEvidencePlanningFailure,
    LegalEvidenceBundle,
    LegalEvidenceIssue,
    LegalEvidencePlanRequest,
    LegalEvidencePlanSnapshot,
    LegalEvidenceSnapshotCompatibilityError,
)
from contract.legal_evidence.planner import AdaptiveLegalEvidencePlanner
from contract.legal_evidence.postgres_repository import PostgresLegalEvidenceRepository
from contract.legal_evidence.reranking import OpenAICompatibleLegalReranker
from contract.risk.models import RiskReviewPlanInput
from contract.risk.playbooks import build_default_registry

_LEGAL_CONCEPTS = (
    "主体", "授权", "签署", "生效", "效力", "价款", "付款", "税费",
    "交付", "验收", "履行", "变更", "保密", "知识产权", "个人信息",
    "数据", "违约金", "赔偿", "解除", "终止", "争议", "仲裁", "管辖",
    "不可抗力", "担保", "转让", "分包", "期限", "通知",
)
_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*[年\-/\.]\s*(?P<month>0?[1-9]|1[0-2])"
    r"\s*[月\-/\.]\s*(?P<day>0?[1-9]|[12]\d|3[01])\s*日?"
)
_CONTRACT_DATE_PREDICATES = ("签订", "签署", "订立", "合同日期", "生效")
_MAX_QUERY_CHARACTERS = 8000
_MAX_FACT_CHARACTERS = 4200


class LegalEvidenceProvider(Protocol):
    """One fail-closed planning Interface behind a fail-open caller Seam."""

    def provide(self, value: RiskReviewPlanInput) -> LegalEvidenceBundle | None: ...


class DisabledLegalEvidenceProvider:
    def provide(self, _value: RiskReviewPlanInput) -> None:
        return None


class PlannerLegalEvidenceProvider:
    def __init__(
        self,
        planner: AdaptiveLegalEvidencePlanner,
        repository: PostgresLegalEvidenceRepository,
        *,
        default_jurisdiction: str = "CN",
        binder: LegalEvidenceCheckBinder | None = None,
    ) -> None:
        self.planner = planner
        self.repository = repository
        self.default_jurisdiction = default_jurisdiction
        self.binder = binder or LegalEvidenceCheckBinder()
        self.registry = build_default_registry()

    def provide(self, value: RiskReviewPlanInput) -> LegalEvidenceBundle:
        request = self._request(value)
        frozen = self.repository.load_plan_snapshot(
            review_id=value.review_id,
            generation_id=value.generation_id,
            attempt_no=value.attempt_no,
            expected_request_hash=request.stable_hash,
        )
        if frozen is not None:
            return self._resolve_snapshot(frozen)
        try:
            bundle = self.binder.bind(self.planner.plan(request))
        except Exception as exc:  # noqa: BLE001
            # Every planner/binder failure must freeze the same fail-open
            # decision; limiting this to known exception types would allow an
            # unclassified failure to be retried into a different result.
            frozen = self.repository.save_failed_plan_snapshot(
                request=request,
                attempt_no=value.attempt_no,
                error_type=type(exc).__name__,
            )
            # A concurrent success may have won before this failed planner.
            # Always resolve the database winner so every caller of the same
            # attempt observes one result.
            return self._resolve_snapshot(frozen)
        frozen = self.repository.save_plan_snapshot(
            request=request,
            bundle=bundle,
            attempt_no=value.attempt_no,
        )
        return self._resolve_snapshot(frozen)

    def _resolve_snapshot(
        self,
        snapshot: LegalEvidencePlanSnapshot,
    ) -> LegalEvidenceBundle:
        if snapshot.status == "PLANNER_FAILED":
            raise FrozenLegalEvidencePlanningFailure(
                snapshot.error_type or "UnknownPlannerError"
            )
        bundle = snapshot.bundle
        if bundle is None:  # guarded by LegalEvidencePlanSnapshot validation
            raise RuntimeError("Legal evidence success snapshot has no bundle")
        if bundle.binding_profile_version != self.binder.profile_version:
            # Rebinding would change a frozen attempt.  A new attempt may use
            # the new profile; this attempt deterministically follows the
            # caller's fail-open legacy path.
            raise LegalEvidenceSnapshotCompatibilityError(
                "Frozen legal evidence binding profile is incompatible"
            )
        return bundle

    def _request(self, value: RiskReviewPlanInput) -> LegalEvidencePlanRequest:
        contract_text = "\n".join(block.text for block in value.source_blocks)
        contract_date = self._verified_contract_date(value)
        review_as_of_date = datetime.now(UTC).date()
        issues: list[LegalEvidenceIssue] = []
        checks_by_domain: dict[str, list] = defaultdict(list)
        for check in self.registry.checks:
            checks_by_domain[str(check.domain)].append(check)
        for domain, checks in checks_by_domain.items():
            check_text = "\n".join(
                f"[{check.check_code}] {check.title}：{check.review_question}"
                for check in checks
            )
            domain_concepts = [
                concept for concept in _LEGAL_CONCEPTS if concept in check_text
            ]
            fact_text = self._domain_contract_facts(
                value,
                concepts=domain_concepts,
            )
            # Put the legal questions before excerpts.  The reranker has a
            # stricter query-size limit than PostgreSQL FTS; placing a long
            # clause excerpt first could otherwise truncate every check that
            # explains what the evidence is being retrieved for.
            query = (
                f"合同类型：{value.contract_type}\n"
                f"我方：{value.our_party}\n相对方：{value.counterparty}\n"
                f"审查域：{domain}\n"
                f"合同日期：{contract_date.isoformat() if contract_date else '未核实'}\n"
                f"审查基准日期：{review_as_of_date.isoformat()}\n"
                f"法律问题：\n{check_text}\n"
                f"合同事实：\n{fact_text}"
            )[:_MAX_QUERY_CHARACTERS]
            required = [
                concept
                for concept in _LEGAL_CONCEPTS
                if concept in contract_text and concept in check_text
            ]
            check_codes = [check.check_code for check in checks]
            identity = {
                "review_id": value.review_id,
                "generation_id": value.generation_id,
                "domain": domain,
                "check_codes": check_codes,
            }
            issues.append(
                LegalEvidenceIssue(
                    issue_id="legal-issue-"
                    + hashlib.sha256(
                        canonical_json(identity).encode("utf-8")
                    ).hexdigest()[:32],
                    domain=domain,
                    query=query,
                    check_codes=check_codes,
                    required_concepts=required,
                )
            )
        return LegalEvidencePlanRequest(
            review_id=value.review_id,
            generation_id=value.generation_id,
            contract_type=value.contract_type,
            # Until a verified place extractor exists, CN means national law
            # only. It is safer than silently applying every locality.
            jurisdiction=self.default_jurisdiction or None,
            contract_date=contract_date,
            review_as_of_date=review_as_of_date,
            issues=issues,
        )

    @staticmethod
    def _domain_contract_facts(
        value: RiskReviewPlanInput,
        *,
        concepts: list[str],
    ) -> str:
        selected: list[str] = []
        used = 0
        for block in value.source_blocks:
            searchable = " ".join((*block.heading_path, block.text))
            if concepts and not any(concept in searchable for concept in concepts):
                continue
            fragment = block.text.strip()
            if not fragment:
                continue
            remaining = _MAX_FACT_CHARACTERS - used
            if remaining <= 0:
                break
            fragment = fragment[:remaining]
            selected.append(fragment)
            used += len(fragment)
        if selected:
            return "\n".join(selected)
        # No relevant clause is itself a useful fact for missing-term checks;
        # do not fill the query with unrelated contract prose.
        return "未在合同原文中抽取到与本审查域直接相关的条款。"

    @staticmethod
    def _verified_contract_date(value: RiskReviewPlanInput) -> date | None:
        candidates: set[date] = set()
        for item in value.stage_result.semantic_ir.dates:
            predicate = item.predicate or ""
            if not any(marker in predicate for marker in _CONTRACT_DATE_PREDICATES):
                continue
            raw = " ".join(part for part in (item.subject, item.object) if part)
            for match in _DATE_PATTERN.finditer(raw):
                try:
                    candidates.add(
                        date(
                            int(match.group("year")),
                            int(match.group("month")),
                            int(match.group("day")),
                        )
                    )
                except ValueError:
                    continue
        # Ambiguous dates are not guessed. The review still uses the current
        # as-of date and records source validity as unverified where necessary.
        return next(iter(candidates)) if len(candidates) == 1 else None


def build_legal_evidence_provider(settings: Settings) -> LegalEvidenceProvider:
    if not settings.legal_evidence_enabled:
        return DisabledLegalEvidenceProvider()
    repository = PostgresLegalEvidenceRepository(settings)
    embedding = None
    reranker = None
    if (
        settings.legal_embedding_base_url
        and settings.legal_embedding_registration_id
        and settings.legal_embedding_model
    ):
        embedding = OpenAICompatibleLegalEmbeddingProvider(
            base_url=settings.legal_embedding_base_url,
            api_key=settings.legal_embedding_api_key,
            registration_id=settings.legal_embedding_registration_id,
            model=settings.legal_embedding_model,
        )
    if (
        settings.legal_reranker_base_url
        and settings.legal_reranker_registration_id
        and settings.legal_reranker_model
    ):
        reranker = OpenAICompatibleLegalReranker(
            base_url=settings.legal_reranker_base_url,
            api_key=settings.legal_reranker_api_key,
            registration_id=settings.legal_reranker_registration_id,
            model=settings.legal_reranker_model,
        )
    return PlannerLegalEvidenceProvider(
        AdaptiveLegalEvidencePlanner(
            repository,
            embedding_provider=embedding,
            reranker=reranker,
        ),
        repository,
        default_jurisdiction=settings.legal_evidence_default_jurisdiction,
    )
