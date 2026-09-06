from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, date, datetime
from typing import Protocol

from contract.application.idempotency import canonical_json
from contract.config import Settings
from contract.legal_evidence.binding import LegalEvidenceCheckBinder
from contract.evidence_planning import EvidencePlanningEngine, EvidencePlanningProfile
from contract.legal_evidence.embedding import OpenAICompatibleLegalEmbeddingProvider
from contract.legal_evidence.models import (
    FrozenLegalEvidencePlanningFailure,
    LegalEvidenceBundle,
    LegalEvidenceIssue,
    LegalEvidencePlanRequest,
    LegalEvidencePlanSnapshot,
    LegalEvidenceSnapshotCompatibilityError,
)
from contract.legal_evidence.planner import (
    PLANNER_VERSION,
    AdaptiveLegalEvidencePlanner,
    LegalEvidencePlannerSafety,
)
from contract.legal_evidence.postgres_repository import PostgresLegalEvidenceRepository
from contract.legal_evidence.reranking import OpenAICompatibleLegalReranker
from contract.risk.models import CheckSpec, RiskReviewPlanInput
from contract.risk.playbooks import build_default_registry

logger = logging.getLogger(__name__)

_LEGAL_CONCEPTS = (
    "主体", "授权", "签署", "生效", "效力", "价款", "付款", "税费",
    "交付", "验收", "履行", "变更", "保密", "知识产权", "个人信息",
    "数据", "违约金", "赔偿", "解除", "终止", "争议", "仲裁", "管辖",
    "不可抗力", "担保", "转让", "分包", "期限", "通知",
)
_LEGAL_CONCEPT_ALIASES = {
    "主体": ("主体", "甲方", "乙方", "当事人"),
    "授权": ("授权", "代理", "代表"),
    "签署": ("签署", "签订", "盖章", "签字"),
    "生效": ("生效", "成立"),
    "效力": ("效力", "无效", "可撤销"),
    "价款": ("价款", "价格", "合同价", "总价", "费用", "金额"),
    "付款": ("付款", "支付", "结算"),
    "税费": ("税费", "税率", "发票", "开票"),
    "交付": ("交付", "交货", "移交"),
    "验收": ("验收", "检验", "确认合格"),
    "履行": ("履行", "义务", "服务"),
    "变更": ("变更", "调整", "修改"),
    "保密": ("保密", "秘密信息"),
    "知识产权": ("知识产权", "著作权", "专利", "商标"),
    "个人信息": ("个人信息", "个人数据"),
    "数据": ("数据", "信息安全", "网络安全"),
    "违约金": ("违约金", "逾期金"),
    "赔偿": ("赔偿", "补偿", "损失"),
    "解除": ("解除", "解约"),
    "终止": ("终止", "停止"),
    "争议": ("争议", "纠纷"),
    "仲裁": ("仲裁",),
    "管辖": ("管辖", "法院"),
    "不可抗力": ("不可抗力", "情势变更"),
    "担保": ("担保", "保证金", "保函"),
    "转让": ("转让", "转委托"),
    "分包": ("分包",),
    "期限": ("期限", "日期", "时间", "日内"),
    "通知": ("通知", "送达"),
}
_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*[年\-/\.]\s*(?P<month>0?[1-9]|1[0-2])"
    r"\s*[月\-/\.]\s*(?P<day>0?[1-9]|[12]\d|3[01])\s*日?"
)
_CONTRACT_DATE_PREDICATES = ("签订", "签署", "订立", "合同日期", "生效")
_MAX_QUERY_CHARACTERS = 8000
_MAX_FACT_CHARACTERS = 4200
_ISSUE_TERM_SPLIT = re.compile(r"[、，,；;和及与或/（）()：:\s]+")
_NON_EVIDENCE_TERMS = {
    "检查",
    "风险",
    "实质风险",
    "我方",
    "合同",
    "机制",
    "条件",
    "后果",
}
# One legal issue must be narrow enough to retrieve a coherent body of law,
# while issuing one query for every one of the 45 review checks is needlessly
# expensive and loses cross-check reuse. These stable topic groups preserve all
# seven review domains and every check exactly once, but split legally distinct
# questions such as termination, force majeure and dispute resolution.
_LEGAL_ISSUE_CHECK_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("主体资格、代表权与签署形式", ("FVA-001", "FVA-002", "FVA-003")),
    ("合同成立、生效与强制性效力", ("FVA-004", "FVA-005")),
    (
        "价款、支付、税费与履约保障",
        ("CF-001", "CF-002", "CF-003", "CF-004", "CF-005", "CF-006"),
    ),
    ("交付与验收", ("CF-007", "CF-008")),
    ("核心履约义务、协作边界与控制权", ("PO-001", "PO-002", "PO-003")),
    ("服务标准、质保、整改与复验", ("PO-004", "PO-007")),
    ("转委托、分包与权利义务转让", ("PO-005",)),
    ("需求、范围、价款与工期变更", ("PO-006",)),
    ("知识产权归属、许可与第三方侵权", ("ICD-001", "ICD-002", "ICD-003")),
    ("保密义务、例外、披露与期限", ("ICD-004",)),
    ("数据处理、个人信息、返还与删除", ("ICD-005", "ICD-006")),
    ("违约责任、损害赔偿与责任限制", ("LRE-001", "LRE-002", "LRE-003", "LRE-004")),
    ("解除、终止、结算与退出", ("LRE-005", "LRE-006")),
    ("不可抗力、情势变更与风险分配", ("LRE-007",)),
    ("适用法律、争议方式与管辖", ("LRE-008",)),
    (
        "条款冲突与合同解释",
        ("CCC-001", "CCC-002", "CCC-003", "CCC-004", "CCC-005"),
    ),
    (
        "必要条款、歧义、待定项与救济机制缺失",
        ("MAC-001", "MAC-002", "MAC-003", "MAC-004", "MAC-005", "MAC-006"),
    ),
)


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
        engine: EvidencePlanningEngine | None = None,
    ) -> None:
        self.planner = planner
        self.repository = repository
        self.default_jurisdiction = default_jurisdiction
        self.binder = binder or LegalEvidenceCheckBinder()
        self.engine = engine or EvidencePlanningEngine()
        if "legal" not in self.engine.profile_ids(include_disabled=True):
            self.engine.register(
                EvidencePlanningProfile(
                    profile_id="legal",
                    planner=self.planner,
                    binder=self.binder,
                )
            )
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
            bundle = self.engine.execute("legal", request)
            if bundle.planning_metrics is not None:
                logger.info(
                    "Legal evidence plan review_id=%s generation_id=%s metrics=%s",
                    value.review_id,
                    value.generation_id,
                    bundle.planning_metrics.model_dump_json(),
                )
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
        contract_date = self._verified_contract_date(value)
        review_as_of_date = datetime.now(UTC).date()
        issues: list[LegalEvidenceIssue] = []
        checks_by_code = {check.check_code: check for check in self.registry.checks}
        grouped_codes = [
            code for _topic, codes in _LEGAL_ISSUE_CHECK_GROUPS for code in codes
        ]
        if (
            len(grouped_codes) != len(set(grouped_codes))
            or set(grouped_codes) != set(checks_by_code)
        ):
            raise RuntimeError(
                "Legal issue groups must cover every registered check exactly once"
            )
        for topic, check_codes_tuple in _LEGAL_ISSUE_CHECK_GROUPS:
            checks = [checks_by_code[code] for code in check_codes_tuple]
            domains = {str(check.domain) for check in checks}
            if len(domains) != 1:
                raise RuntimeError("One legal issue group cannot span review domains")
            domain = domains.pop()
            check_texts = [
                f"[{check.check_code}] {check.title}：{check.review_question}"
                for check in checks
            ]
            combined_check_text = "\n".join(check_texts)
            domain_concepts = [
                concept for concept in _LEGAL_CONCEPTS if concept in combined_check_text
            ]
            evidence_need = list(
                dict.fromkeys(
                    term
                    for check in checks
                    for term in self._evidence_need(check.title, domain_concepts)
                )
            )[:100]
            required_ir_types = list(
                dict.fromkeys(
                    ir_type for check in checks for ir_type in check.required_ir_types
                )
            )
            facts = self._issue_contract_facts(
                value,
                concepts=evidence_need,
                ir_types=required_ir_types,
            )
            fact_text = "\n".join(facts) if facts else "未抽取到直接相关合同条款。"
            required = [
                concept
                for concept in domain_concepts
                if self._concept_present(concept, fact_text)
            ]
            if not required:
                required = domain_concepts[:2] or evidence_need[:2]
            question = self._domain_legal_question(
                value,
                topic=topic,
                check_titles=[check.title for check in checks],
                facts=facts,
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
                f"法律议题主题：{topic}\n"
                f"审查目标：\n{combined_check_text}\n"
                f"法律议题：\n{question}\n"
                f"合同事实：\n{fact_text}"
            )[:_MAX_QUERY_CHARACTERS]
            check_codes = [check.check_code for check in checks]
            identity = {
                "review_id": value.review_id,
                "generation_id": value.generation_id,
                "domain": domain,
                "topic": topic,
                "check_codes": check_codes,
                "facts": facts,
            }
            issues.append(
                LegalEvidenceIssue(
                    issue_id="legal-issue-"
                    + hashlib.sha256(
                        canonical_json(identity).encode("utf-8")
                    ).hexdigest()[:32],
                    domain=domain,
                    query=query,
                    question=question,
                    facts=facts,
                    parties=[value.our_party, value.counterparty],
                    contract_object=value.contract_type,
                    source_domain=domain,
                    evidence_need=evidence_need,
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
            planner_version=PLANNER_VERSION,
            reranker_version=self._reranker_version(),
            issues=issues,
        )

    @staticmethod
    def _evidence_need(title: str, concepts: list[str]) -> list[str]:
        terms = [
            item.strip()
            for item in _ISSUE_TERM_SPLIT.split(title)
            if len(item.strip()) >= 2 and item.strip() not in _NON_EVIDENCE_TERMS
        ]
        return list(dict.fromkeys([*concepts, *terms]))[:12]

    @staticmethod
    def _domain_legal_question(
        value: RiskReviewPlanInput,
        *,
        topic: str,
        check_titles: list[str],
        facts: list[str],
    ) -> str:
        focus = f"{topic}（{'、'.join(check_titles)}）"
        if facts:
            fact_summary = "；".join(facts)[:1200]
            return (
                f"在{value.our_party}与{value.counterparty}的{value.contract_type}合同中，"
                f"基于“{fact_summary}”，围绕{focus}，应适用哪些权利义务、"
                "强制性规则、例外和法律后果？"
            )
        return (
            f"在{value.our_party}与{value.counterparty}的{value.contract_type}合同中，"
            f"当前事实抽取未定位到与“{focus}”直接相关的明确条款；"
            "应适用哪些法定默认规则、强制性要求或举证规则？"
        )

    @staticmethod
    def _concept_present(concept: str, text: str) -> bool:
        return any(
            alias in text
            for alias in _LEGAL_CONCEPT_ALIASES.get(concept, (concept,))
        )

    @staticmethod
    def _issue_contract_facts(
        value: RiskReviewPlanInput,
        *,
        concepts: list[str],
        ir_types: list[str],
    ) -> list[str]:
        anchored_block_ids = {
            anchor.block_id
            for ir_type in ir_types
            for item in getattr(value.stage_result.semantic_ir, ir_type, ())
            for anchor in item.source_anchors
        }
        selected: list[str] = []
        used = 0
        for block in sorted(
            value.source_blocks,
            key=lambda item: (item.block_no, item.block_id),
        ):
            searchable = " ".join((*block.heading_path, block.text))
            anchored = block.block_id in anchored_block_ids
            lexical = bool(concepts) and any(concept in searchable for concept in concepts)
            if not anchored and not lexical:
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
            if len(selected) >= 8:
                break
        return selected

    def _reranker_version(self) -> str | None:
        reranker = getattr(self.planner, "reranker", None)
        if reranker is None:
            return None
        registration = str(getattr(reranker, "registration_id", "") or "")
        model = str(getattr(reranker, "model", "") or "")
        return ":".join(item for item in (registration, model) if item) or type(
            reranker
        ).__name__

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
            safety=LegalEvidencePlannerSafety(
                maximum_wall_time_seconds=(
                    settings.legal_evidence_issue_wall_time_seconds
                ),
                maximum_total_wall_time_seconds=(
                    settings.legal_evidence_total_wall_time_seconds
                ),
            ),
        ),
        repository,
        default_jurisdiction=settings.legal_evidence_default_jurisdiction,
    )
