from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from contract.api.models import FindingCategory, Perspective
from contract.errors import ContractError
from contract.risk.models import (
    ApplicabilitySpec,
    CheckSpec,
    Criticality,
    ExecutionMode,
    IrField,
    LegacyFindingMapping,
    PlaybookManifest,
    RiskDomain,
)


BASE_UNIT_IDS: tuple[RiskDomain, ...] = (
    "formation_validity_authority",
    "commercial_financial",
    "performance_obligations",
    "ip_confidentiality_data",
    "liability_remedies_exit",
)
HORIZONTAL_UNIT_IDS: tuple[RiskDomain, ...] = (
    "cross_clause_consistency",
    "missing_ambiguity_completeness",
)
UNIT_ORDER = BASE_UNIT_IDS + HORIZONTAL_UNIT_IDS


@dataclass(frozen=True, slots=True)
class PlaybookRoute:
    selected: tuple[PlaybookManifest, ...]
    not_applicable: tuple[str, ...]

    @property
    def specialist_count(self) -> int:
        return sum(
            1
            for item in self.selected
            if item.execution_mode == ExecutionMode.SPECIALIST_REVIEWER
        )


class PlaybookRegistry:
    def __init__(
        self,
        *,
        manifests: Iterable[PlaybookManifest],
        checks: Iterable[CheckSpec],
    ) -> None:
        manifest_values = tuple(manifests)
        check_values = tuple(checks)
        self._manifests = self._unique_by_id(manifest_values, "playbook_id", "playbook")
        self._checks = self._unique_by_id(check_values, "check_code", "check")
        self._validate()

    @staticmethod
    def _unique_by_id(values, attribute: str, label: str):
        result = {}
        for value in values:
            key = getattr(value, attribute)
            if key in result:
                raise ValueError(f"Duplicate {label} identifier: {key}")
            result[key] = value
        return result

    @property
    def checks(self) -> tuple[CheckSpec, ...]:
        return tuple(self._checks[code] for code in sorted(self._checks))

    @property
    def manifests(self) -> tuple[PlaybookManifest, ...]:
        return tuple(self._manifests[key] for key in sorted(self._manifests))

    @property
    def mappings(self) -> tuple[LegacyFindingMapping, ...]:
        return tuple(
            LegacyFindingMapping(
                domain=item.domain,
                check_code=item.check_code,
                category=item.allowed_categories[0],
                legacy_artifact_type=item.legacy_artifact_type,
            )
            for item in self.checks
        )

    def manifest(self, playbook_id: str) -> PlaybookManifest:
        value = self._manifests.get(playbook_id)
        if value is None:
            raise ContractError(
                "RISK_PLAYBOOK_NOT_FOUND",
                f"Unknown risk review playbook: {playbook_id}",
                status_code=422,
            )
        return value

    def has_manifest(self, playbook_id: str) -> bool:
        return playbook_id in self._manifests

    def check(self, check_code: str) -> CheckSpec:
        value = self._checks.get(check_code)
        if value is None:
            raise ContractError(
                "RISK_CHECK_NOT_FOUND",
                f"Unknown risk review check: {check_code}",
                status_code=422,
            )
        return value

    def checks_for(self, manifests: Iterable[PlaybookManifest]) -> tuple[CheckSpec, ...]:
        codes: set[str] = set()
        for manifest in manifests:
            codes.update(manifest.check_codes)
        return tuple(
            sorted(
                (self.check(code) for code in codes if self.check(code).enabled),
                key=lambda item: (UNIT_ORDER.index(item.domain), item.priority, item.check_code),
            )
        )

    def _validate(self) -> None:
        if "base_neutral" not in self._manifests:
            raise ValueError("base_neutral playbook is required")
        base = self._manifests["base_neutral"]
        if base.execution_mode != ExecutionMode.DETERMINISTIC:
            raise ValueError("base_neutral must use DETERMINISTIC execution mode")
        frozen_base_codes = {item.check_code for item in _checks()}
        if set(base.check_codes) != frozen_base_codes or len(base.check_codes) != 45:
            raise ValueError("base_neutral must cover the frozen 45 checks exactly once")
        for manifest in self._manifests.values():
            for code in manifest.check_codes:
                check = self._checks.get(code)
                if check is None:
                    raise ValueError(f"Playbook {manifest.playbook_id} references unknown check {code}")
                if check.domain not in manifest.target_domains:
                    raise ValueError(
                        f"Playbook {manifest.playbook_id} does not target check domain {check.domain}"
                    )
            if manifest.execution_mode == ExecutionMode.EXTEND_DOMAIN:
                if not set(manifest.target_domains).issubset(UNIT_ORDER):
                    raise ValueError("EXTEND_DOMAIN cannot create a new review unit")
        mapping_keys = {
            (item.domain, item.check_code, item.allowed_categories[0])
            for item in self._checks.values()
        }
        if len(mapping_keys) != len(self._checks):
            raise ValueError("Every check must have one unique legacy mapping")
        for item in self._checks.values():
            if len(item.allowed_categories) != 1:
                raise ValueError(f"{item.check_code} must have exactly one frozen category in v1")
            is_other = item.allowed_categories[0] == FindingCategory.OTHER
            allowed_other = (
                item.check_code == "FVA-005"
                and item.domain == "formation_validity_authority"
                and item.allowed_risk_types == ["MANDATORY_RULE_OR_VALIDITY_RISK"]
            )
            if is_other != allowed_other:
                raise ValueError("Only FVA-005 may use the frozen OTHER compatibility mapping")


class PlaybookRouter:
    def __init__(self, registry: PlaybookRegistry) -> None:
        self.registry = registry

    def route(
        self,
        playbook_ids: list[str],
        *,
        contract_type: str,
        perspective: Perspective,
        review_attitude: str,
    ) -> PlaybookRoute:
        if len(playbook_ids) != len(set(playbook_ids)):
            raise ContractError(
                "INVALID_RISK_PLAYBOOK_SELECTION",
                "Risk review playbook IDs must be unique",
                status_code=422,
            )
        if "base_neutral" not in playbook_ids:
            raise ContractError(
                "INVALID_RISK_PLAYBOOK_SELECTION",
                "base_neutral is required for schema_version=1.0",
                status_code=422,
            )
        selected: list[PlaybookManifest] = []
        not_applicable: list[str] = []
        for playbook_id in playbook_ids:
            manifest = self.registry.manifest(playbook_id)
            if not manifest.enabled:
                raise ContractError(
                    "RISK_PLAYBOOK_DISABLED",
                    f"Risk review playbook is disabled: {playbook_id}",
                    status_code=422,
                )
            if manifest.applicability.matches(
                contract_type=contract_type,
                perspective=perspective,
                review_attitude=review_attitude,
            ):
                selected.append(manifest)
            elif playbook_id == "base_neutral":
                raise ContractError(
                    "RISK_PLAYBOOK_NOT_APPLICABLE",
                    "base_neutral is not applicable to the review context",
                    status_code=422,
                )
            else:
                not_applicable.append(playbook_id)
        specialist_count = sum(
            1
            for item in selected
            if item.execution_mode == ExecutionMode.SPECIALIST_REVIEWER
        )
        if specialist_count > 2:
            raise ContractError(
                "RISK_SPECIALIST_LIMIT_EXCEEDED",
                "At most two specialist reviewers may be selected",
                status_code=422,
            )
        return PlaybookRoute(tuple(selected), tuple(not_applicable))


def _check(
    domain: RiskDomain,
    code: str,
    title: str,
    category: FindingCategory,
    artifact: str,
    ir_types: tuple[IrField, ...],
    risk_type: str,
    priority: int,
) -> CheckSpec:
    return CheckSpec(
        check_code=code,
        title=title,
        description=title,
        domain=domain,
        criticality=Criticality.REQUIRED,
        applicability=ApplicabilitySpec(contract_types=["*"]),
        required_ir_types=list(ir_types),
        review_question=f"检查{title}是否对我方形成实质风险。",
        allowed_categories=[category],
        allowed_risk_types=[risk_type],
        risk_level_rule_id="neutral-materiality-v1",
        deterministic_validator_ids=["source-anchor-v1", "perspective-v1"],
        execution_mode=ExecutionMode.DETERMINISTIC,
        priority=priority,
        enabled=True,
        legacy_artifact_type=artifact,
    )


def _checks() -> tuple[CheckSpec, ...]:
    rights = "rights_obligations_review_result"
    commercial = "commercial_terms_review_result"
    liability = "liability_termination_review_result"
    missing = "missing_ambiguous_clauses_result"
    relation = "relation_extraction_result"
    rows = (
        ("formation_validity_authority", "FVA-001", "主体名称、身份和对应关系", FindingCategory.PARTY_IDENTIFICATION, rights, ("definitions",), "PARTY_IDENTITY_RISK"),
        ("formation_validity_authority", "FVA-002", "主体资格、代表权和授权", FindingCategory.PARTY_IDENTIFICATION, rights, ("rights", "obligations"), "AUTHORITY_OR_CAPACITY_RISK"),
        ("formation_validity_authority", "FVA-003", "签字、盖章及必要签署形式缺失", FindingCategory.MISSING_CLAUSE, missing, ("obligations",), "EXECUTION_FORM_MISSING"),
        ("formation_validity_authority", "FVA-004", "生效条件、时间和前置条件不清", FindingCategory.AMBIGUITY, missing, ("dates", "obligations"), "EFFECTIVENESS_CONDITION_AMBIGUITY"),
        ("formation_validity_authority", "FVA-005", "强制性规范、禁止性安排或基础效力风险", FindingCategory.OTHER, rights, ("prohibitions", "obligations"), "MANDATORY_RULE_OR_VALIDITY_RISK"),
        ("commercial_financial", "CF-001", "价款、计价口径和总额", FindingCategory.PAYMENT, commercial, ("payment_terms", "amounts"), "PRICE_CALCULATION_RISK"),
        ("commercial_financial", "CF-002", "付款节点、条件和期限", FindingCategory.PAYMENT, commercial, ("payment_terms", "dates"), "PAYMENT_TIMING_RISK"),
        ("commercial_financial", "CF-003", "发票、税费和开票前置条件", FindingCategory.PAYMENT, commercial, ("payment_terms", "obligations"), "INVOICE_TAX_CONDITION_RISK"),
        ("commercial_financial", "CF-004", "调价、扣款、抵销和结算调整", FindingCategory.PAYMENT, commercial, ("payment_terms", "amounts"), "PAYMENT_ADJUSTMENT_RISK"),
        ("commercial_financial", "CF-005", "预付款、保证金和履约保障", FindingCategory.PAYMENT, commercial, ("payment_terms", "amounts", "obligations"), "ADVANCE_PAYMENT_SECURITY_RISK"),
        ("commercial_financial", "CF-006", "币种、账户和支付路径", FindingCategory.PAYMENT, commercial, ("payment_terms", "amounts"), "PAYMENT_PATH_RISK"),
        ("commercial_financial", "CF-007", "交付物、时间和交付方式", FindingCategory.DELIVERY, commercial, ("delivery_terms", "dates", "obligations"), "DELIVERY_RISK"),
        ("commercial_financial", "CF-008", "验收标准、程序、期限和后果", FindingCategory.ACCEPTANCE, commercial, ("acceptance_terms", "dates", "obligations"), "ACCEPTANCE_RISK"),
        ("performance_obligations", "PO-001", "双方核心义务范围", FindingCategory.RIGHTS_OBLIGATIONS_IMBALANCE, rights, ("rights", "obligations", "delivery_terms"), "CORE_OBLIGATION_SCOPE_RISK"),
        ("performance_obligations", "PO-002", "权利义务和单方控制权平衡", FindingCategory.RIGHTS_OBLIGATIONS_IMBALANCE, rights, ("rights", "obligations", "prohibitions"), "UNILATERAL_CONTROL_RISK"),
        ("performance_obligations", "PO-003", "配合义务、前置依赖和边界", FindingCategory.RIGHTS_OBLIGATIONS_IMBALANCE, rights, ("obligations",), "COOPERATION_DEPENDENCY_RISK"),
        ("performance_obligations", "PO-004", "服务标准、响应时限、质量和验收机制", FindingCategory.RIGHTS_OBLIGATIONS_IMBALANCE, rights, ("obligations", "dates", "delivery_terms", "acceptance_terms"), "SERVICE_LEVEL_RISK"),
        ("performance_obligations", "PO-005", "转委托、分包和权利义务转让", FindingCategory.RIGHTS_OBLIGATIONS_IMBALANCE, rights, ("rights", "obligations", "prohibitions"), "ASSIGNMENT_SUBCONTRACT_RISK"),
        ("performance_obligations", "PO-006", "需求、范围和价格变更控制", FindingCategory.RIGHTS_OBLIGATIONS_IMBALANCE, rights, ("rights", "obligations", "payment_terms"), "CHANGE_CONTROL_RISK"),
        ("performance_obligations", "PO-007", "质保、维护、整改、复验和支持", FindingCategory.RIGHTS_OBLIGATIONS_IMBALANCE, rights, ("obligations", "dates", "acceptance_terms"), "WARRANTY_SUPPORT_RISK"),
        ("ip_confidentiality_data", "ICD-001", "项目成果和新增知识产权归属", FindingCategory.INTELLECTUAL_PROPERTY, liability, ("intellectual_property_terms", "rights", "obligations"), "FOREGROUND_IP_OWNERSHIP_RISK"),
        ("ip_confidentiality_data", "ICD-002", "背景知识产权、许可范围和限制", FindingCategory.INTELLECTUAL_PROPERTY, liability, ("intellectual_property_terms", "rights"), "BACKGROUND_IP_LICENSE_RISK"),
        ("ip_confidentiality_data", "ICD-003", "第三方权利和不侵权救济", FindingCategory.INTELLECTUAL_PROPERTY, liability, ("intellectual_property_terms", "liabilities"), "THIRD_PARTY_IP_RISK"),
        ("ip_confidentiality_data", "ICD-004", "保密范围、例外、期限和披露", FindingCategory.CONFIDENTIALITY, liability, ("confidentiality_terms", "dates", "obligations"), "CONFIDENTIALITY_SCOPE_RISK"),
        ("ip_confidentiality_data", "ICD-005", "数据使用、处理、安全和访问", FindingCategory.CONFIDENTIALITY, liability, ("confidentiality_terms", "obligations"), "DATA_PROCESSING_RISK"),
        ("ip_confidentiality_data", "ICD-006", "数据权属、返还、删除和留存", FindingCategory.CONFIDENTIALITY, liability, ("confidentiality_terms", "rights", "obligations"), "DATA_RETURN_RETENTION_RISK"),
        ("liability_remedies_exit", "LRE-001", "违约触发和责任成立条件", FindingCategory.BREACH, liability, ("liabilities", "obligations"), "BREACH_TRIGGER_RISK"),
        ("liability_remedies_exit", "LRE-002", "违约金、损失计算和调整", FindingCategory.BREACH, liability, ("liabilities", "amounts"), "LIQUIDATED_DAMAGES_RISK"),
        ("liability_remedies_exit", "LRE-003", "责任上限、免责和间接损失", FindingCategory.LIABILITY, liability, ("liabilities", "amounts"), "LIABILITY_CAP_RISK"),
        ("liability_remedies_exit", "LRE-004", "赔偿、补偿和第三方索赔", FindingCategory.LIABILITY, liability, ("liabilities", "obligations"), "INDEMNITY_RISK"),
        ("liability_remedies_exit", "LRE-005", "解除、终止和单方退出", FindingCategory.TERMINATION, liability, ("termination_terms", "rights", "dates"), "TERMINATION_RIGHT_RISK"),
        ("liability_remedies_exit", "LRE-006", "终止后的结算、返还和继续义务", FindingCategory.TERMINATION, liability, ("termination_terms", "payment_terms", "obligations"), "POST_TERMINATION_RISK"),
        ("liability_remedies_exit", "LRE-007", "不可抗力、情势变化和风险分配", FindingCategory.LIABILITY, liability, ("liabilities", "termination_terms"), "FORCE_MAJEURE_RISK"),
        ("liability_remedies_exit", "LRE-008", "适用法律、争议方式和管辖", FindingCategory.DISPUTE_RESOLUTION, liability, ("dispute_resolution",), "DISPUTE_FORUM_RISK"),
        ("cross_clause_consistency", "CCC-001", "日期、期限、金额和比例冲突", FindingCategory.INTERNAL_CONFLICT, relation, ("dates", "amounts"), "VALUE_CONFLICT_RISK"),
        ("cross_clause_consistency", "CCC-002", "权利、义务和条件前后冲突", FindingCategory.INTERNAL_CONFLICT, relation, ("rights", "obligations", "prohibitions"), "OBLIGATION_CONFLICT_RISK"),
        ("cross_clause_consistency", "CCC-003", "条款、附件引用和优先级冲突", FindingCategory.INTERNAL_CONFLICT, relation, ("definitions",), "REFERENCE_PRIORITY_RISK"),
        ("cross_clause_consistency", "CCC-004", "付款、验收、违约、解除联动冲突", FindingCategory.INTERNAL_CONFLICT, relation, ("payment_terms", "acceptance_terms", "liabilities", "termination_terms"), "CLAUSE_INTERACTION_RISK"),
        ("cross_clause_consistency", "CCC-005", "主体、定义和术语不一致", FindingCategory.INTERNAL_CONFLICT, relation, ("definitions",), "TERM_IDENTITY_CONFLICT_RISK"),
        ("missing_ambiguity_completeness", "MAC-001", "Playbook要求的必要条款缺失", FindingCategory.MISSING_CLAUSE, missing, ("acceptance_terms", "dispute_resolution"), "REQUIRED_CLAUSE_MISSING"),
        ("missing_ambiguity_completeness", "MAC-002", "关键商务要素或执行机制缺失", FindingCategory.MISSING_CLAUSE, missing, ("payment_terms", "delivery_terms", "acceptance_terms"), "BUSINESS_MECHANISM_MISSING"),
        ("missing_ambiguity_completeness", "MAC-003", "未定义术语、多义和指代不清", FindingCategory.AMBIGUITY, missing, ("definitions",), "UNDEFINED_TERM_RISK"),
        ("missing_ambiguity_completeness", "MAC-004", "条件、期限、标准或后果不完整", FindingCategory.AMBIGUITY, missing, ("dates", "obligations"), "INCOMPLETE_CONDITION_RISK"),
        ("missing_ambiguity_completeness", "MAC-005", "空白、待定、占位符和断裂引用", FindingCategory.AMBIGUITY, missing, ("definitions",), "PLACEHOLDER_OR_BROKEN_REFERENCE_RISK"),
        ("missing_ambiguity_completeness", "MAC-006", "必要救济、退出或争议机制缺失", FindingCategory.MISSING_CLAUSE, missing, ("liabilities", "termination_terms", "dispute_resolution"), "REMEDY_EXIT_MISSING"),
    )
    return tuple(
        _check(*row, priority=index)
        for index, row in enumerate(rows, start=1)
    )


def build_default_registry() -> PlaybookRegistry:
    checks = _checks()
    manifest = PlaybookManifest(
        playbook_id="base_neutral",
        version="1.0",
        name="合同中立审查基础检查包",
        description="schema_version=1.0下所有正式启用的45项必要检查。",
        enabled=True,
        execution_mode=ExecutionMode.DETERMINISTIC,
        criticality=Criticality.REQUIRED,
        applicability=ApplicabilitySpec(contract_types=["*"]),
        required_ir_types=list(
            dict.fromkeys(ir_type for item in checks for ir_type in item.required_ir_types)
        ),
        target_domains=list(UNIT_ORDER),
        check_codes=[item.check_code for item in checks],
        risk_level_rule_ids=["neutral-materiality-v1"],
        evidence_policy_id="contract-evidence-v1",
        deterministic_validator_ids=[
            "source-anchor-v1",
            "perspective-v1",
            "check-coverage-v1",
            "fva-other-compatibility-v1",
        ],
        test_case_ids=["service-outsourcing-0829-v1"],
    )
    return PlaybookRegistry(manifests=[manifest], checks=checks)
