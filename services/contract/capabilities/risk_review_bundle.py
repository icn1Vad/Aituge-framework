"""Stage 6.3 five-domain Direct Review and deterministic base Bundle execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError, ValidationInfo, model_validator

from contract.risk.models import (
    RiskAbsenceEvidenceSource,
    RiskCheckEvidencePolicy,
    RiskEvidenceSource,
    RiskReviewPlan,
)
from services.contract.capabilities.prompt_budget import (
    PROMPT_BUDGET_POLICY_VERSION,
    PromptBudgetResult,
    evaluate_prompt_budget,
    summarize_prompt_budgets,
)
from services.contract.capabilities.party_roles import (
    contract_party_roles,
    text_names_role,
)
from contract.risk.icd_source_policy import ICD_SOURCE_PATTERN_RULES
from contract.risk.lre_source_policy import (
    LRE_ABSENCE_POLICIES,
    LRE_BROAD_BREACH_TRIGGER_PATTERN,
    LRE_SOURCE_PATTERN_RULES,
)
from contract.risk.po_source_policy import PO_SOURCE_PATTERN_RULES
from service.conversation.llm_runner import LlmCompletionResult, LlmRuntime
from services.contract.capabilities.model_observation import (
    deferred_completion_kwargs,
    finalize_completion_success,
    finalize_completion_validation_failed,
)
from services.contract.capabilities.risk_review import (
    BASE_CHECK_CODE_PATTERN,
    BASE_UNIT_ID_PATTERN,
    CheckCoverageResult,
    CommercialFinancialDirectReviewer,
    CommercialIrItem,
    CommercialReviewRequest,
    CommercialSourceExcerpt,
    DirectReviewError,
    EvidenceCandidate,
    FindingDraft,
    LlmAttemptDiagnostic,
    LlmCallMetric,
    ReviewUnitResult,
    SchemaNormalizationRecord,
    StrictModel,
    _attempt_diagnostic,
    _stable_id,
    _sum_optional,
    commercial_request_from_context,
    enforce_provider_prompt_budget,
)
from task_manager.output_parser import parse_json_output


BASE_UNIT_IDS = (
    "formation_validity_authority",
    "commercial_financial",
    "performance_obligations",
    "ip_confidentiality_data",
    "liability_remedies_exit",
)
GENERIC_UNIT_IDS = tuple(item for item in BASE_UNIT_IDS if item != "commercial_financial")
EXPECTED_BASE_CHECK_CODES = tuple(
    [
        *(f"FVA-{index:03d}" for index in range(1, 6)),
        *(f"CF-{index:03d}" for index in range(1, 9)),
        *(f"PO-{index:03d}" for index in range(1, 8)),
        *(f"ICD-{index:03d}" for index in range(1, 7)),
        *(f"LRE-{index:03d}" for index in range(1, 9)),
    ]
)

UNIT_CHECK_CODES = {
    "formation_validity_authority": tuple(
        f"FVA-{index:03d}" for index in range(1, 6)
    ),
    "commercial_financial": tuple(f"CF-{index:03d}" for index in range(1, 9)),
    "performance_obligations": tuple(f"PO-{index:03d}" for index in range(1, 8)),
    "ip_confidentiality_data": tuple(f"ICD-{index:03d}" for index in range(1, 7)),
    "liability_remedies_exit": tuple(f"LRE-{index:03d}" for index in range(1, 9)),
}

UNIT_NAMES = {
    "formation_validity_authority": "成立、生效与授权",
    "performance_obligations": "履行义务与控制",
    "ip_confidentiality_data": "知识产权、保密与数据",
    "liability_remedies_exit": "责任、救济与退出",
}

UNIT_BOUNDARIES = {
    "formation_validity_authority": (
        "只审合同文本中的主体称谓一致性、签署形式、生效条件和可由文本支持的基础效力风险。"
        "合同未附证照、授权书或外部登记信息，只能提示外部核验或补充证明，"
        "不得断言现实中的主体无资格、签署人无授权或合同当然无效。"
        "FVA-005只能使用category=OTHER和risk_type=MANDATORY_RULE_OR_VALIDITY_RISK。"
    ),
    "performance_obligations": (
        "只审标的和履行范围、交付进度、质量与服务标准、验收机制、双方配合义务、"
        "变更流程、质保整改及权利义务和程序控制平衡。"
        "不得审主体授权和签署效力、员工劳动合同/社保/外部履约资质、知识产权/保密/数据、"
        "赔偿上限/终止救济/争议解决或纯付款/发票/税费问题。"
        "PO-006可以审范围变更与费用、工期的联动，但不得重新审基础付款条件。"
    ),
    "ip_confidentiality_data": (
        "只审项目成果和背景知识产权、第三方权利、保密、数据处理及返还删除留存。"
        "文本未涉及个人信息时不得虚构具体数据种类或安全事件。"
    ),
    "liability_remedies_exit": (
        "只审违约触发、违约金和损失、责任限制、赔偿、解除终止、终止后义务、"
        "不可抗力及争议解决。不得把同一条款下不同法律根因强行合并。"
    ),
}

CHECK_DECISION_RULES = {
    "FVA-001": "核对首部、正文、签署处的主体名称和角色；只有实质不一致或指向不明才成立风险。",
    "FVA-002": (
        "四步判断：1.只找合同文本中关于签约主体、签署人、代表权、授权、"
        "签字盖章或审批条件的明确表述；2.如文本明确出现主体/签署人矛盾、"
        "无权或越权、授权缺失被合同约定为成立或生效条件、签章主体冲突，"
        "判TEXTUAL_AUTHORITY_RISK并仅用原文Evidence生成Finding；"
        "3.如文本没有冲突，但仅凭合同无法确认现实中的法定代表人、授权委托、"
        "董事会/股东会批准、营业执照或内部审批，判EXTERNAL_VERIFICATION_REQUIRED，"
        "不生成Finding，只说明需要核验的外部材料；"
        "4.如文本一致且没有特别的外部核验线索，判NO_VISIBLE_ISSUE且不生成Finding。"
        "不得使用source_policy禁止的非授权领域材料，也不得把缺少附件本身当作代表权风险。"
    ),
    "FVA-003": "检查合同约定或文本结构所需的签字、盖章及签署形式是否明确完整。",
    "FVA-004": "检查生效时间、条件、追溯安排和前置条件是否明确且不冲突。",
    "FVA-005": "只处理合同文本直接呈现的强制规范、禁止性安排或基础效力疑点；普通不利条款不属于本检查。",
    "PO-001": (
        "对象是标的、服务/交付范围和基本进度边界。开放式增加要求、单方扩大范围，"
        "或新增工作没有明确边界时才形成候选；范围、交付内容和期限均明确且变更另有书面机制时不构成风险。"
        "付款归CF，变更程序归PO-006。"
    ),
    "PO-002": (
        "对象是权利义务和程序控制平衡。相对方享有单方审批、解释、暂停、拒绝或检查决定权，"
        "而我方承担对应义务且缺少对等程序保护时触发；合理监督权并有客观条件、通知和救济时不构成风险。"
        "解除终止和责任救济归LRE。"
    ),
    "PO-003": (
        "对象是配合义务、前置依赖和延迟归责。相对方配合义务模糊或缺失，"
        "其延迟仍由我方承担工期或履约后果时触发；双方资料、接口、时限和顺延后果明确时不构成风险。"
        "员工劳动合同、社保和外部履约资质不属于本检查。"
    ),
    "PO-004": (
        "对象是质量、服务标准、响应时限和验收机制。仅写满足要求、达到目的、专业水准等不可衡量标准，"
        "缺少验收标准/程序，期限明显过短、默示验收或单方判断结果时触发；指标、期限、程序和复验机制明确时不构成风险。"
        "交付物本身归PO-001，付款与验收挂钩归CF。"
    ),
    "PO-005": (
        "对象是转委托、分包及权利义务转让。未经我方同意即可转委托/分包/转让，"
        "或未明确原责任主体继续负责时触发；事先书面同意、受托方要求和持续责任明确时不构成风险。"
    ),
    "PO-006": (
        "对象是需求、范围、工期和价款的变更控制。相对方可单方变更，或变更没有书面确认、"
        "费用和工期联动及拒绝机制时触发；双方书面确认且范围、费用、工期同步调整时不构成风险。"
        "基础付款、发票和税费归CF。"
    ),
    "PO-007": (
        "对象是质保、维护、整改、修复、复验和持续支持。范围、期限、响应、整改后复验或责任不明确时触发；"
        "支持范围、期限、响应等级、整改和复验后果完整时不构成风险。赔偿上限和解除救济归LRE。"
    ),
    "ICD-001": "检查项目成果和新增知识产权的权属、交付和使用范围。",
    "ICD-002": "检查背景知识产权归属、许可范围、期限、地域、转授权和限制。",
    "ICD-003": "检查第三方权利保证、不侵权承诺、索赔处理和替换/赔偿救济。",
    "ICD-004": "检查保密信息范围、例外、披露对象、期限和法定披露程序。",
    "ICD-005": "检查数据使用目的、处理范围、访问、安全措施和事件责任。",
    "ICD-006": "检查数据及载体的权属、返还、删除、备份和法定留存。",
    "LRE-001": "检查违约责任成立的触发、归责、通知和补救条件。",
    "LRE-002": "检查违约金、损失计算、累计适用和调整机制是否合理明确。",
    "LRE-003": "检查责任上限、免责、间接损失和例外是否对我方造成重大暴露。",
    "LRE-004": "检查赔偿范围、第三方索赔、抗辩控制、通知和费用承担。",
    "LRE-005": "检查解除、终止、单方退出、宽限期和持续违约条件是否平衡可执行。",
    "LRE-006": "检查终止后的结算、返还、移交、保密和继续有效义务。",
    "LRE-007": "检查不可抗力通知、减损、期限、终止权和风险分配。",
    "LRE-008": "检查适用法律、协商/诉讼/仲裁路径和管辖是否明确且不明显不利于我方。",
}

_GENERIC_SYSTEM_PROMPT = """你是合同风险Direct Reviewer。
你只审查当前输入分配的check_code，不调用工具，不审查其他领域。
严格站在our_party立场并使用NEUTRAL标准；Finding只表示对our_party有原文依据的实质风险。
逐项执行decision_rule和unit_boundary；有利、中性、普通说明、纯格式问题或外部事实猜测不得生成Finding。
必须使用当前输入明确给出的Evidence选择形式；PO只能选择不可拆分的source_id，
其他已实现领域只能使用输入给出的IR引用和Evidence引用；缺失风险才可用ABSENCE。
现实世界的主体资格、法定代表人身份、授权委托、董事会/股东会批准、营业执照和内部审批属于外部事实；
除非合同文本明确记载冲突、无权、越权或把缺失授权约定为成立/生效条件，否则不得生成其实质风险Finding。
不得生成Python负责的ID、Hash、Block、字符位置或reason_code。
只输出一个JSON对象，第一字符必须是{，最后字符必须是}；禁止Markdown和过程说明。"""

_PO_CANDIDATE_SYSTEM_PROMPT = """你是合同风险候选裁决器。
Python已经确定性生成全部RiskCandidate、证据边界和技术映射；你不能创建、删除、合并或跳过Candidate。
你必须站在our_party和perspective指定的我方立场，以NEUTRAL标准逐项裁决。
每个candidate_id必须返回一次且仅一次RISK、NO_RISK或INSUFFICIENT_EVIDENCE。
decision_summary只说明裁决依据，使用“合同文本”“该安排”“相关条款”等中性称谓；
禁止在输出中判断或书写甲方、乙方、我方、相对方及主体名称。
RISK必须选择至少一个当前Candidate允许的recommended_control_code；NO_RISK必须给出具体反向理由；
HARD_RULE或STRONG_SIGNAL的NO_RISK必须引用允许的Counter Evidence，不能只写“未发现风险”。
Primary Evidence由Python固定，不能删除、替换或在输出中声明；Supporting和Counter只能从当前Candidate允许列表选择。
Severity只能从当前Candidate的allowed_severity_factors选择，并且必须由当前Candidate Evidence直接支持；
它只是非权威语义提议，Python会逐项执行证据门并根据severity_rule_id计算最终风险等级。
禁止输出Check、Category、Risk Type、风险等级、正式Finding文案、主体、立场、
Anchor、Block、字符位置、Hash或其他技术ID。
只输出一个JSON对象，顶层只能是candidate_decisions；第一字符必须是{，最后字符必须是}；
禁止Markdown、过程说明和未列字段。"""

FVA002_ASSESSMENT_TYPES = (
    "TEXTUAL_AUTHORITY_RISK",
    "EXTERNAL_VERIFICATION_REQUIRED",
    "NO_VISIBLE_ISSUE",
)
FVA002_EXTERNAL_MATERIAL_WORDS = (
    "法定代表人",
    "授权委托书",
    "董事会",
    "股东会",
    "营业执照",
    "内部审批",
)
FVA002_OUT_OF_SCOPE_WORDS = (
    "员工",
    "上岗人员",
    "劳动合同",
    "社会保险",
    "社保",
    "履约能力",
    "服务能力",
)


class GenericCheckSpec(StrictModel):
    check_code: str = Field(pattern=BASE_CHECK_CODE_PATTERN)
    review_question: str = Field(min_length=1, max_length=2000)
    allowed_categories: list[str] = Field(min_length=1, max_length=1)
    allowed_risk_types: list[str] = Field(min_length=1, max_length=10)
    required_ir_types: list[str] = Field(default_factory=list, max_length=14)
    criticality: Literal["REQUIRED"]


class GenericReviewRequest(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    attempt_no: int = Field(ge=1, le=2)
    plan_id: str = Field(pattern=r"^risk-plan-[0-9a-f]{32}$")
    context_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party: str = Field(min_length=1, max_length=500)
    counterparty: str = Field(min_length=1, max_length=500)
    contract_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=80)
    review_attitude: Literal["NEUTRAL"]
    assigned_check_specs: list[GenericCheckSpec] = Field(min_length=1, max_length=8)
    definitions: list[CommercialIrItem] = Field(default_factory=list)
    projected_ir_items: list[CommercialIrItem] = Field(default_factory=list)
    source_excerpts: list[CommercialSourceExcerpt] = Field(min_length=1)
    evidence_sources: list[RiskEvidenceSource] = Field(default_factory=list)
    absence_evidence_sources: list[RiskAbsenceEvidenceSource] = Field(
        default_factory=list
    )
    check_evidence_policies: list[RiskCheckEvidencePolicy] = Field(
        default_factory=list
    )
    present_ir_types: list[str] = Field(default_factory=list)
    missing_ir_types: list[str] = Field(default_factory=list)
    estimated_input_tokens: int = Field(ge=1, le=6000)

    @model_validator(mode="after")
    def validate_identity_and_coverage(
        self, info: ValidationInfo
    ) -> "GenericReviewRequest":
        if self.unit_id not in GENERIC_UNIT_IDS:
            raise ValueError("Generic Direct Reviewer supports only the four new base units")
        expected = set(UNIT_CHECK_CODES[self.unit_id])
        codes = [item.check_code for item in self.assigned_check_specs]
        if len(codes) != len(set(codes)) or not set(codes).issubset(expected):
            raise ValueError("Batch Check codes must be unique and belong to its Unit")
        item_ids = [item.item_id for item in [*self.definitions, *self.projected_ir_items]]
        anchor_ids = [item.anchor_id for item in self.source_excerpts]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Projected IR item IDs must be unique")
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("Source Anchor IDs must be unique")
        known_anchors = set(anchor_ids)
        if any(
            anchor.anchor_id not in known_anchors
            for item in [*self.definitions, *self.projected_ir_items]
            for anchor in item.source_anchors
        ):
            raise ValueError("Projected IR references an unknown source Anchor")
        if (
            self.unit_id == "formation_validity_authority"
            and self.check_evidence_policies
        ) or self.unit_id in {
            "performance_obligations",
            "ip_confidentiality_data",
            "liability_remedies_exit",
        }:
            self._validate_evidence_source_catalog(
                allow_absence_only=bool(
                    info.context
                    and info.context.get("allow_absence_only_evidence_catalog")
                )
            )
        return self

    def _validate_evidence_source_catalog(
        self, *, allow_absence_only: bool = False
    ) -> None:
        items = {
            item.item_id: item
            for item in [*self.definitions, *self.projected_ir_items]
        }
        excerpts = {item.anchor_id: item for item in self.source_excerpts}
        source_ids = [item.source_id for item in self.evidence_sources]
        absence_ids = [item.source_id for item in self.absence_evidence_sources]
        if (
            (not source_ids and not (allow_absence_only and absence_ids))
            or len(source_ids) != len(set(source_ids))
            or len(absence_ids) != len(set(absence_ids))
            or set(source_ids) & set(absence_ids)
        ):
            raise ValueError("Evidence Source IDs must be present and unique")
        for source in self.evidence_sources:
            item = items.get(source.ir_item_id)
            excerpt = excerpts.get(source.anchor_id)
            if (
                source.generation_id != self.generation_id
                or item is None
                or excerpt is None
                or source.ir_type != item.ir_type
                or source.anchor_id
                not in {anchor.anchor_id for anchor in item.source_anchors}
                or source.block_id != excerpt.block_id
                or source.page_number != excerpt.page_number
                or source.char_start != excerpt.char_start
                or source.char_end != excerpt.char_end
                or source.quoted_text != excerpt.quoted_text
                or source.quoted_text_hash != excerpt.quoted_text_hash
            ):
                raise ValueError(
                    "Evidence Source must preserve its Generation, IR and Anchor binding"
                )
        assigned_codes = [item.check_code for item in self.assigned_check_specs]
        assigned_code_set = set(assigned_codes)
        if any(
            not set(source.allowed_check_codes).issubset(assigned_code_set)
            for source in self.evidence_sources
        ):
            raise ValueError(
                "Evidence Source allowed_check_codes must belong to the current Batch"
            )
        policies = {item.check_code: item for item in self.check_evidence_policies}
        if set(policies) != set(assigned_codes) or len(policies) != len(
            self.check_evidence_policies
        ):
            raise ValueError("Every assigned Check requires one Evidence policy")
        known_source_ids = set(source_ids)
        known_absence_ids = set(absence_ids)
        absence_by_id = {
            item.source_id: item for item in self.absence_evidence_sources
        }
        if any(
            item.generation_id != self.generation_id
            for item in self.absence_evidence_sources
        ):
            raise ValueError("Absence Evidence Source belongs to another Generation")
        for check_code, policy in policies.items():
            expected_source_ids = {
                source.source_id
                for source in self.evidence_sources
                if check_code in source.allowed_check_codes
            }
            if (
                set(policy.allowed_evidence_source_ids) - known_source_ids
                or set(policy.allowed_evidence_source_ids)
                != expected_source_ids
                or set(policy.allowed_absence_source_ids) - known_absence_ids
                or any(
                    absence_by_id[source_id].check_code != check_code
                    for source_id in policy.allowed_absence_source_ids
                )
            ):
                raise ValueError(
                    "Evidence policy references an unknown or cross-Check Source"
                )


class GenericModelEvidenceDraft(StrictModel):
    evidence_type: Literal["TEXT_QUOTE", "CONTEXT", "ABSENCE"]
    ir_ref: str | None = Field(default=None, pattern=r"^I[0-9]{3}$")
    evidence_ref: str | None = Field(default=None, pattern=r"^A[0-9]{3}$")
    checked_scope: str | None = Field(default=None, min_length=1, max_length=500)
    verification_note: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_shape(self) -> "GenericModelEvidenceDraft":
        if self.evidence_type == "ABSENCE":
            if self.ir_ref is not None or self.evidence_ref is not None:
                raise ValueError("ABSENCE cannot reference source text")
            if self.checked_scope is None or self.verification_note is None:
                raise ValueError("ABSENCE requires checked_scope and verification_note")
        elif self.ir_ref is None or self.evidence_ref is None:
            raise ValueError("Text Evidence requires ir_ref and evidence_ref")
        return self


class GenericModelFindingDraft(StrictModel):
    # These three fields are compatibility inputs only. The parent Check,
    # Registry, and deterministic candidate rules remain authoritative.
    check_code: str | None = Field(default=None, pattern=BASE_CHECK_CODE_PATTERN)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    risk_type: str | None = Field(default=None, min_length=1, max_length=160)
    risk_level: Literal["HIGH", "MEDIUM", "LOW", "INFO"]
    title: str = Field(min_length=1, max_length=300)
    issue: str = Field(min_length=1, max_length=2000)
    impact_to_our_party: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(min_length=1, max_length=2000)
    evidence: list[GenericModelEvidenceDraft] = Field(default_factory=list, max_length=20)
    evidence_source_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_candidate_ids(self) -> "GenericModelFindingDraft":
        if any(
            re.fullmatch(r"risk-candidate-[0-9a-f]{32}", item) is None
            for item in self.candidate_ids
        ):
            raise ValueError("candidate_ids contain an invalid deterministic ID")
        return self


class GenericModelCheckResultRaw(StrictModel):
    check_code: str = Field(pattern=BASE_CHECK_CODE_PATTERN)
    status: Literal["REVIEWED", "NOT_APPLICABLE", "FAILED"]
    reason_code: str | None = None
    decision_note: str = Field(min_length=1, max_length=1000)
    findings: list[GenericModelFindingDraft] = Field(default_factory=list)
    assessment_type: Literal[
        "TEXTUAL_AUTHORITY_RISK",
        "EXTERNAL_VERIFICATION_REQUIRED",
        "NO_VISIBLE_ISSUE",
    ] | None = None
    external_verification_required: bool | None = None

    @model_validator(mode="after")
    def validate_fva002_fields(self) -> "GenericModelCheckResultRaw":
        if self.check_code == "FVA-002":
            if (
                self.assessment_type is None
                or self.external_verification_required is None
            ):
                raise ValueError(
                    "FVA-002 requires assessment_type and "
                    "external_verification_required"
                )
        elif (
            self.assessment_type is not None
            or self.external_verification_required is not None
        ):
            raise ValueError("FVA-002 assessment fields belong only to FVA-002")
        return self


class GenericModelResponseRaw(StrictModel):
    check_results: list[GenericModelCheckResultRaw] = Field(min_length=1, max_length=8)


class GenericModelCheckResult(StrictModel):
    check_code: str = Field(pattern=BASE_CHECK_CODE_PATTERN)
    status: Literal["REVIEWED", "NOT_APPLICABLE", "FAILED"]
    reason_code: Literal[
        "RISK_IDENTIFIED",
        "NO_RISK_IDENTIFIED",
        "NOT_APPLICABLE",
        "CHECK_FAILED",
        "INSUFFICIENT_EVIDENCE",
    ]
    decision_note: str = Field(min_length=1, max_length=1000)
    findings: list[GenericModelFindingDraft] = Field(default_factory=list)
    assessment_type: Literal[
        "TEXTUAL_AUTHORITY_RISK",
        "EXTERNAL_VERIFICATION_REQUIRED",
        "NO_VISIBLE_ISSUE",
    ] | None = None
    external_verification_required: bool | None = None


class FvaAssessmentResult(StrictModel):
    check_code: Literal["FVA-002"]
    assessment_type: Literal[
        "TEXTUAL_AUTHORITY_RISK",
        "EXTERNAL_VERIFICATION_REQUIRED",
        "NO_VISIBLE_ISSUE",
    ]
    external_verification_required: bool


SeverityFactorCode = Literal[
    "UNILATERAL_CONTROL",
    "NO_EFFECTIVE_REMEDY",
    "BROAD_SCOPE",
    "FINANCIAL_IMPACT",
    "SCHEDULE_IMPACT",
    "OPERATIONAL_IMPACT",
    "MISSING_CORE_MECHANISM",
    "OWNERSHIP_AMBIGUITY",
    "OVERBROAD_TRANSFER",
    "EXCLUSIVE_OR_IRREVOCABLE",
    "UNLIMITED_SCOPE",
    "POST_TERMINATION_EFFECT",
    "ONE_SIDED_PROTECTION",
    "THIRD_PARTY_EXPOSURE",
    "NO_RETURN_OR_DELETION",
    "NO_SECURITY_STANDARD",
    "MISSING_INCIDENT_NOTICE",
    "UNLIMITED_LIABILITY",
    "ONE_SIDED_LIABILITY",
    "OVERBROAD_INDEMNITY",
    "INDIRECT_LOSS_EXPOSURE",
    "LIABILITY_CAP_BYPASSED",
    "CUMULATIVE_REMEDIES",
    "UNILATERAL_TERMINATION",
    "NO_CURE_PERIOD",
    "NO_TERMINATION_SETTLEMENT",
    "POST_TERMINATION_EXPOSURE",
    "AUTOMATIC_RENEWAL",
    "RESTRICTED_EXIT_WINDOW",
    "DISPUTE_CLAUSE_CONFLICT",
    "FOREIGN_OR_BURDENSOME_FORUM",
]


class Po003CandidatePrecondition(StrictModel):
    counterparty_cooperation_required: bool
    performance_depends_on_cooperation: bool
    adverse_consequence_to_our_party: bool
    relief_or_adjustment_missing: bool
    model_review_required: bool
    supporting_source_ids: list[str] = Field(default_factory=list, max_length=20)
    unmet_conditions: list[str] = Field(default_factory=list, max_length=4)


class IcdPerspectivePrecondition(StrictModel):
    counterparty_protects_our_party: bool
    adverse_burden_on_our_party: bool
    model_review_required: bool
    supporting_source_ids: list[str] = Field(default_factory=list, max_length=20)
    decision_reason: str = Field(min_length=1, max_length=500)


class SeverityFactorPolicy(StrictModel):
    factor_code: SeverityFactorCode
    allowed_check_codes: list[str] = Field(min_length=1, max_length=8)
    allowed_candidate_types: list[str] = Field(min_length=1, max_length=15)
    required_evidence_types: list[Literal["TEXT_QUOTE", "ABSENCE"]] = Field(
        min_length=1,
        max_length=2,
    )
    required_text_signals: list[str] = Field(default_factory=list, max_length=20)
    required_absence_source_types: list[str] = Field(
        default_factory=list,
        max_length=10,
    )
    conflicting_or_disqualifying_signals: list[str] = Field(
        default_factory=list,
        max_length=20,
    )


class RejectedSeverityFactor(StrictModel):
    factor_code: SeverityFactorCode
    reason_code: Literal[
        "FACTOR_NOT_ALLOWED_FOR_CANDIDATE",
        "DIRECT_CAUSAL_EVIDENCE_MISSING",
        "REQUIRED_TEXT_SIGNAL_MISSING",
        "VALID_ABSENCE_SOURCE_MISSING",
    ]
    evidence_source_ids: list[str] = Field(default_factory=list, max_length=40)


class DeterministicRiskCandidate(StrictModel):
    candidate_id: str = Field(pattern=r"^risk-candidate-[0-9a-f]{32}$")
    check_code: str = Field(pattern=BASE_CHECK_CODE_PATTERN)
    candidate_type: Literal[
        "PROJECTED_IR_REVIEW",
        "MISSING_EXPECTED_IR",
        "SCOPE_EXPANSION",
        "DELIVERY_SCHEDULE_REVIEW",
        "RIGHTS_OBLIGATIONS_IMBALANCE",
        "COOPERATION_DEPENDENCY",
        "COOPERATION_OBLIGATION_ABSENT",
        "QUALITY_STANDARD_UNMEASURABLE",
        "ACCEPTANCE_MECHANISM_REVIEW",
        "ACCEPTANCE_MECHANISM_ABSENT",
        "ASSIGNMENT_SUBCONTRACT_REVIEW",
        "CHANGE_CONTROL_REVIEW",
        "CHANGE_CONTROL_ABSENT",
        "WARRANTY_SUPPORT_REVIEW",
        "WARRANTY_SUPPORT_ABSENT",
        "FOREGROUND_IP_OWNERSHIP_REVIEW",
        "FOREGROUND_IP_OWNERSHIP_ABSENT",
        "BACKGROUND_IP_LICENSE_REVIEW",
        "BACKGROUND_IP_LICENSE_ABSENT",
        "THIRD_PARTY_IP_PROTECTION_REVIEW",
        "THIRD_PARTY_IP_PROTECTION_ABSENT",
        "CONFIDENTIALITY_PROTECTION_REVIEW",
        "CONFIDENTIALITY_COMPLETENESS_ABSENT",
        "DATA_PROCESSING_SECURITY_REVIEW",
        "DATA_PROCESSING_SECURITY_ABSENT",
        "DATA_RETURN_DELETION_REVIEW",
        "DATA_RETURN_DELETION_ABSENT",
        "BROAD_BREACH_TRIGGER_REVIEW",
        "OVERBROAD_LOSS_SCOPE_REVIEW",
        "CUMULATIVE_REMEDIES_REVIEW",
        "LIABILITY_CAP_ABSENT",
        "LIABILITY_CAP_BYPASS_REVIEW",
        "OVERBROAD_INDEMNITY_REVIEW",
        "TERMINATION_RIGHTS_REVIEW",
        "TERMINATION_SETTLEMENT_REVIEW",
        "TERMINATION_SETTLEMENT_ABSENT",
        "FORCE_MAJEURE_MECHANISM_ABSENT",
        "DISPUTE_RESOLUTION_ABSENT",
    ]
    trigger_reason: str = Field(min_length=1, max_length=500)
    required_ir_types: list[str]
    candidate_ir_refs: list[str]
    candidate_evidence_refs: list[str]
    requires_model_decision: bool = True
    candidate_strength: Literal[
        "HARD_RULE",
        "STRONG_SIGNAL",
        "SEMANTIC_REVIEW",
    ] = "SEMANTIC_REVIEW"
    criticality: Literal["REQUIRED"] = "REQUIRED"
    facts: list[str] = Field(default_factory=list, max_length=20)
    trigger_conditions: list[str] = Field(default_factory=list, max_length=20)
    mitigating_conditions: list[str] = Field(default_factory=list, max_length=20)
    disqualifying_conditions: list[str] = Field(
        default_factory=list,
        max_length=20,
    )
    primary_evidence_requirements: list[str] = Field(
        default_factory=list,
        max_length=20,
    )
    absence_evidence_requirements: list[str] = Field(
        default_factory=list,
        max_length=20,
    )
    primary_evidence_source_ids: list[str] = Field(default_factory=list, max_length=20)
    allowed_supporting_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=40,
    )
    allowed_counter_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=40,
    )
    severity_rule_id: str = Field(
        default="GENERIC_SEMANTIC_V1",
        pattern=r"^[A-Z][A-Z0-9_]*_V[0-9]+$",
    )
    canonical_root_type: str = Field(
        default="GENERIC_CANDIDATE_ROOT",
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    root_severity_rule_id: str = Field(
        default="GENERIC_ROOT_V1",
        pattern=r"^[A-Z][A-Z0-9_]*_V[0-9]+$",
    )
    merge_group_id: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    merge_compatible_candidate_types: list[str] = Field(
        default_factory=list,
        max_length=10,
    )
    core_primary_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
    )
    context_primary_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
    )
    deterministic_severity_factors: list[SeverityFactorCode] = Field(
        default_factory=list,
        max_length=7,
    )
    allowed_severity_factors: list[SeverityFactorCode] = Field(
        default_factory=list,
        max_length=20,
    )
    po003_precondition: Po003CandidatePrecondition | None = None
    icd_perspective_precondition: IcdPerspectivePrecondition | None = None

    @model_validator(mode="after")
    def validate_po_candidate_contract(self) -> "DeterministicRiskCandidate":
        if self.check_code.startswith(("PO-", "ICD-", "LRE-")):
            if (
                not self.facts
                or not self.trigger_conditions
                or not self.primary_evidence_source_ids
                or self.severity_rule_id == "GENERIC_SEMANTIC_V1"
            ):
                raise ValueError(
                    "Candidate requires facts, trigger conditions, "
                    "Primary Evidence and a frozen severity rule"
                )
            source_ids = [
                self.primary_evidence_source_ids,
                self.allowed_supporting_evidence_source_ids,
                self.allowed_counter_evidence_source_ids,
                self.core_primary_evidence_source_ids,
                self.context_primary_evidence_source_ids,
            ]
            if any(len(items) != len(set(items)) for items in source_ids):
                raise ValueError(
                    "Candidate Evidence role lists must be unique"
                )
            if (
                set(self.core_primary_evidence_source_ids)
                & set(self.context_primary_evidence_source_ids)
                or set(
                    [
                        *self.core_primary_evidence_source_ids,
                        *self.context_primary_evidence_source_ids,
                    ]
                )
                != set(self.primary_evidence_source_ids)
            ):
                raise ValueError(
                    "Candidate core/context Primary Evidence must be "
                    "disjoint and cover all Primary Evidence"
                )
            if (
                self.merge_group_id is None
                and self.merge_compatible_candidate_types
            ):
                raise ValueError(
                    "Candidate merge compatibility requires a merge group"
                )
            if len(self.deterministic_severity_factors) != len(
                set(self.deterministic_severity_factors)
            ):
                raise ValueError(
                    "Deterministic severity factors must be unique"
                )
            if len(self.allowed_severity_factors) != len(
                set(self.allowed_severity_factors)
            ):
                raise ValueError("Allowed severity factors must be unique")
            if self.check_code == "PO-003":
                if self.po003_precondition is None:
                    raise ValueError("PO-003 requires a deterministic precondition")
                if (
                    self.requires_model_decision
                    != self.po003_precondition.model_review_required
                ):
                    raise ValueError(
                        "PO-003 model decision must follow its precondition"
                    )
            elif self.po003_precondition is not None:
                raise ValueError("Only PO-003 may contain a PO-003 precondition")
            if self.icd_perspective_precondition is not None:
                if self.check_code != "ICD-004":
                    raise ValueError(
                        "Only ICD-004 may contain an ICD perspective precondition"
                    )
                if (
                    self.requires_model_decision
                    != self.icd_perspective_precondition.model_review_required
                ):
                    raise ValueError(
                        "ICD model decision must follow its perspective precondition"
                    )
        return self


class CandidateSeverityFactors(StrictModel):
    unilateral_control: bool = False
    no_effective_remedy: bool = False
    broad_scope: bool = False
    financial_impact: bool = False
    schedule_impact: bool = False
    operational_impact: bool = False
    missing_core_mechanism: bool = False
    ownership_ambiguity: bool = False
    overbroad_transfer: bool = False
    exclusive_or_irrevocable: bool = False
    unlimited_scope: bool = False
    post_termination_effect: bool = False
    one_sided_protection: bool = False
    third_party_exposure: bool = False
    no_return_or_deletion: bool = False
    no_security_standard: bool = False
    missing_incident_notice: bool = False
    unlimited_liability: bool = False
    one_sided_liability: bool = False
    overbroad_indemnity: bool = False
    indirect_loss_exposure: bool = False
    liability_cap_bypassed: bool = False
    cumulative_remedies: bool = False
    unilateral_termination: bool = False
    no_cure_period: bool = False
    no_termination_settlement: bool = False
    post_termination_exposure: bool = False
    automatic_renewal: bool = False
    restricted_exit_window: bool = False
    dispute_clause_conflict: bool = False
    foreign_or_burdensome_forum: bool = False


class CandidateDecisionRaw(StrictModel):
    candidate_id: str = Field(pattern=r"^risk-candidate-[0-9a-f]{32}$")
    verdict: Literal["RISK", "NO_RISK", "INSUFFICIENT_EVIDENCE"]
    decision_summary: str = Field(min_length=1, max_length=1200)
    severity_factors: list[SeverityFactorCode] = Field(default_factory=list, max_length=20)
    supporting_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=40,
    )
    counter_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=40,
    )
    recommended_control_codes: list[
        Literal[
            "DEFINE_SCOPE_BOUNDARY",
            "ADD_WRITTEN_CHANGE_PROCEDURE",
            "LINK_CHANGE_TO_FEE_AND_SCHEDULE",
            "DEFINE_DELIVERY_DEADLINE",
            "DEFINE_MEASURABLE_STANDARD",
            "ADD_FORMAL_ACCEPTANCE_PROCEDURE",
            "DEFINE_ACCEPTANCE_PERIOD",
            "ADD_RECTIFICATION_AND_RETEST",
            "ADD_COUNTERPARTY_COOPERATION_DUTY",
            "EXTEND_SCHEDULE_FOR_COUNTERPARTY_DELAY",
            "LIMIT_UNILATERAL_CONTROL",
            "ADD_NOTICE_REQUIREMENT",
            "ADD_CONFIDENTIALITY_GUARD",
            "DEFINE_FOREGROUND_IP_OWNERSHIP",
            "PRESERVE_BACKGROUND_IP",
            "LIMIT_IP_LICENSE_SCOPE",
            "ADD_THIRD_PARTY_IP_WARRANTY",
            "ADD_IP_INFRINGEMENT_REMEDIES",
            "DEFINE_CONFIDENTIAL_INFORMATION",
            "ADD_CONFIDENTIALITY_EXCEPTIONS",
            "DEFINE_CONFIDENTIALITY_TERM",
            "LIMIT_DATA_PROCESSING_PURPOSE",
            "ADD_DATA_SECURITY_STANDARD",
            "ADD_SECURITY_INCIDENT_NOTICE",
            "ADD_DATA_RETURN_DELETION",
            "LIMIT_DATA_RETENTION",
            "RESERVE_BACKGROUND_IP",
            "SEPARATE_BACKGROUND_AND_FOREGROUND_IP",
            "LIMIT_LICENSE_TO_CONTRACT_PURPOSE",
            "DEFINE_LICENSE_TERM_AND_SCOPE",
            "RESTRICT_SUBLICENSING",
            "DEFINE_POST_TERMINATION_USE",
            "RESTRICT_THIRD_PARTY_DISCLOSURE",
            "ADD_RETURN_AND_DESTRUCTION",
            "DEFINE_DATA_OWNERSHIP",
            "RESTRICT_DATA_SHARING",
            "ADD_DATA_EXPORT_AND_RETURN",
            "ADD_DATA_DELETION",
            "FLOW_DOWN_SECURITY_OBLIGATIONS",
            "DEFINE_SECURITY_EXIT_PROCEDURE",
            "TIE_LIABILITY_TO_SPECIFIC_OBLIGATION",
            "LIMIT_LOSS_TO_DIRECT_AND_FORESEEABLE",
            "EXCLUDE_INDIRECT_AND_CONSEQUENTIAL_LOSS",
            "ADD_AGGREGATE_LIABILITY_CAP",
            "DEFINE_LIABILITY_CAP_BASE",
            "ALIGN_CAP_EXCEPTIONS",
            "PREVENT_DOUBLE_RECOVERY",
            "DEFINE_THIRD_PARTY_CLAIM_PROCEDURE",
            "ADD_NOTICE_AND_MITIGATION_DUTY",
            "ADD_MUTUAL_REMEDIES",
            "ADD_CURE_PERIOD",
            "LIMIT_TERMINATION_TRIGGERS",
            "ADD_TERMINATION_SETTLEMENT",
            "DEFINE_POST_TERMINATION_OBLIGATIONS",
            "ADD_TRANSITION_ASSISTANCE",
            "REMOVE_AUTOMATIC_RENEWAL",
            "ADD_RENEWAL_NOTICE",
            "ADD_REASONABLE_EXIT_WINDOW",
            "CLARIFY_FORCE_MAJEURE",
            "DEFINE_FORCE_MAJEURE_NOTICE",
            "DEFINE_INSURANCE_REQUIREMENTS",
            "CHOOSE_SINGLE_DISPUTE_FORUM",
            "CLARIFY_GOVERNING_LAW",
            "SELECT_REASONABLE_FORUM",
            "ADD_DISPUTE_ESCALATION_DEADLINE",
        ]
    ] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_verdict_shape(self) -> "CandidateDecisionRaw":
        if self.verdict == "RISK":
            if not self.recommended_control_codes:
                raise ValueError(
                    "RISK requires at least one recommended_control_code"
                )
        elif self.recommended_control_codes:
            raise ValueError(
                "Only a RISK decision may contain recommended_control_codes"
            )
        if len(self.severity_factors) != len(set(self.severity_factors)):
            raise ValueError("severity_factors must be unique")
        if len(self.recommended_control_codes) != len(
            set(self.recommended_control_codes)
        ):
            raise ValueError("recommended_control_codes must be unique")
        return self


class CandidateDecisionResponseRaw(StrictModel):
    candidate_decisions: list[CandidateDecisionRaw] = Field(min_length=1, max_length=20)


class CandidateDecision(StrictModel):
    candidate_id: str = Field(pattern=r"^risk-candidate-[0-9a-f]{32}$")
    check_code: str = Field(pattern=r"^(PO|ICD|LRE)-[0-9]{3}$")
    candidate_type: str = Field(min_length=1, max_length=160)
    verdict: Literal["RISK", "NO_RISK", "INSUFFICIENT_EVIDENCE"]
    reason_code: Literal[
        "RISK_IDENTIFIED",
        "NO_RISK_SUPPORTED",
        "INSUFFICIENT_EVIDENCE",
    ]
    risk_level: Literal["HIGH", "MEDIUM", "LOW", "INFO"] | None = None
    primary_evidence_source_ids: list[str] = Field(min_length=1, max_length=20)
    supporting_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=40,
    )
    counter_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=40,
    )
    decision_summary: str = Field(min_length=1, max_length=1200)
    severity_factors: CandidateSeverityFactors
    proposed_semantic_severity_factors: list[SeverityFactorCode] = Field(
        default_factory=list,
        max_length=20,
    )
    validated_semantic_severity_factors: list[SeverityFactorCode] = Field(
        default_factory=list,
        max_length=20,
    )
    rejected_severity_factors: list[RejectedSeverityFactor] = Field(
        default_factory=list,
        max_length=20,
    )
    decision_source: Literal[
        "MODEL",
        "DETERMINISTIC_PRECONDITION",
    ] = "MODEL"
    po003_precondition: Po003CandidatePrecondition | None = None
    icd_perspective_precondition: IcdPerspectivePrecondition | None = None
    recommended_control_codes: list[str] = Field(default_factory=list, max_length=30)


class CanonicalRiskRoot(StrictModel):
    root_id: str = Field(pattern=r"^risk-root-[0-9a-f]{32}$")
    domain: Literal[
        "performance_obligations",
        "ip_confidentiality_data",
        "liability_remedies_exit",
    ]
    check_code: str = Field(pattern=r"^(PO|ICD|LRE)-[0-9]{3}$")
    risk_type: str = Field(min_length=1, max_length=160)
    root_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    source_candidate_ids: list[str] = Field(min_length=1, max_length=20)
    primary_evidence_source_ids: list[str] = Field(min_length=1, max_length=40)
    core_primary_evidence_source_ids: list[str] = Field(min_length=1, max_length=40)
    context_primary_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=40,
    )
    supporting_evidence_source_ids: list[str] = Field(
        default_factory=list,
        max_length=80,
    )
    severity_factors: CandidateSeverityFactors
    recommended_control_codes: list[str] = Field(default_factory=list, max_length=30)
    root_severity_rule_id: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]*_V[0-9]+$",
    )
    risk_level: Literal["HIGH", "MEDIUM", "LOW", "INFO"]
    finding_local_id: str = Field(pattern=r"^finding-[0-9a-f]{32}$")

    @model_validator(mode="after")
    def validate_root_sets(self) -> "CanonicalRiskRoot":
        lists = (
            self.source_candidate_ids,
            self.primary_evidence_source_ids,
            self.core_primary_evidence_source_ids,
            self.context_primary_evidence_source_ids,
            self.supporting_evidence_source_ids,
            self.recommended_control_codes,
        )
        if any(len(values) != len(set(values)) for values in lists):
            raise ValueError("Canonical Risk Root lists must be unique")
        if (
            set(self.core_primary_evidence_source_ids)
            & set(self.context_primary_evidence_source_ids)
        ):
            raise ValueError(
                "Canonical Risk Root core/context Primary Evidence must be disjoint"
            )
        if set(
            [
                *self.core_primary_evidence_source_ids,
                *self.context_primary_evidence_source_ids,
            ]
        ) != set(self.primary_evidence_source_ids):
            raise ValueError(
                "Canonical Risk Root core/context evidence must cover Primary Evidence"
            )
        return self


class CheckDecisionResult(StrictModel):
    check_code: str = Field(pattern=r"^(PO|ICD|LRE)-[0-9]{3}$")
    status: Literal["REVIEWED"]
    reason_code: Literal[
        "RISK_IDENTIFIED",
        "NO_RISK_IDENTIFIED",
        "INSUFFICIENT_EVIDENCE",
    ]
    candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    finding_local_ids: list[str] = Field(default_factory=list, max_length=20)


class DeterministicFindingEnrichment(StrictModel):
    finding_local_id: str = Field(pattern=r"^finding-[0-9a-f]{32}$")
    check_code: str = Field(pattern=BASE_CHECK_CODE_PATTERN)
    candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    check_code_source: Literal["PARENT_CHECK"]
    category_source: Literal["REGISTRY"]
    risk_type_source: Literal["MODEL", "CANDIDATE", "UNIQUE_ALLOWED_TYPE"]
    check_code_enriched: bool
    category_enriched: bool
    risk_type_enriched: bool
    ignored_model_check_code: bool
    ignored_model_category: bool


class ReviewBatchResult(StrictModel):
    unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    domain: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")
    status: Literal["COMPLETED", "PARTIAL_FAILED", "FAILED"]
    check_results: list[CheckCoverageResult] = Field(min_length=1, max_length=8)
    findings: list[FindingDraft] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    model_call_count: int = Field(ge=0, le=2)
    repair_count: int = Field(ge=0, le=1)
    schema_repair_count: int = Field(default=0, ge=0, le=1)
    evidence_selection_repair_count: int = Field(default=0, ge=0, le=1)
    evidence_binding_normalization_count: int = Field(default=0, ge=0)
    ignored_model_link_fields_count: int = Field(default=0, ge=0)
    selected_evidence_source_ids: list[str] = Field(default_factory=list)
    tool_call_count: Literal[0] = 0
    prompt_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    prompt_budget: PromptBudgetResult | None = None
    duration_ms: int = Field(ge=0)
    trace_ids: list[str] = Field(default_factory=list, max_length=2)
    call_metrics: list[LlmCallMetric] = Field(default_factory=list, max_length=2)
    repair_reasons: list[str] = Field(default_factory=list, max_length=1)
    schema_normalization_applied: bool = False
    schema_normalization_type: Literal["TOP_LEVEL_CHECKS_TO_CHECK_RESULTS"] | None = None
    attempt_diagnostics: list[LlmAttemptDiagnostic] = Field(
        default_factory=list,
        max_length=2,
    )
    reason_code_enrichment_count: int = Field(ge=0, le=8)
    reason_code_rule_version: Literal["1.0"]
    ignored_model_reason_code_count: int = Field(ge=0, le=8)
    deterministic_enrichment_count: int = Field(default=0, ge=0)
    check_code_enrichment_count: int = Field(default=0, ge=0)
    category_enrichment_count: int = Field(default=0, ge=0)
    risk_type_enrichment_count: int = Field(default=0, ge=0)
    ignored_model_check_code_count: int = Field(default=0, ge=0)
    ignored_model_category_count: int = Field(default=0, ge=0)
    category_conflict_count: Literal[0] = 0
    deterministic_enrichments: list[DeterministicFindingEnrichment] = Field(
        default_factory=list
    )
    candidate_decisions: list[CandidateDecision] = Field(default_factory=list)
    canonical_risk_roots: list[CanonicalRiskRoot] = Field(default_factory=list)
    check_decisions: list[CheckDecisionResult] = Field(default_factory=list)
    supporting_primary_overlap_count: int = Field(default=0, ge=0)
    decision_summary_perspective_warning_count: int = Field(default=0, ge=0)
    proposed_severity_factor_count: int = Field(default=0, ge=0)
    accepted_severity_factor_count: int = Field(default=0, ge=0)
    rejected_severity_factor_count: int = Field(default=0, ge=0)
    fva_assessments: list[FvaAssessmentResult] = Field(
        default_factory=list,
        max_length=1,
    )


class BaseReviewUnitResult(StrictModel):
    unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    domain: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    status: Literal["COMPLETED", "PARTIAL_FAILED", "FAILED"]
    batch_ids: list[str] = Field(min_length=1, max_length=2)
    check_results: list[CheckCoverageResult] = Field(min_length=5, max_length=8)
    findings: list[FindingDraft] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    model_call_count: int = Field(ge=0, le=4)
    repair_count: int = Field(ge=0, le=2)
    schema_repair_count: int = Field(default=0, ge=0, le=2)
    evidence_selection_repair_count: int = Field(default=0, ge=0, le=2)
    evidence_binding_normalization_count: int = Field(default=0, ge=0)
    ignored_model_link_fields_count: int = Field(default=0, ge=0)
    deterministic_enrichment_count: int = Field(default=0, ge=0)
    check_code_enrichment_count: int = Field(default=0, ge=0)
    category_enrichment_count: int = Field(default=0, ge=0)
    risk_type_enrichment_count: int = Field(default=0, ge=0)
    ignored_model_check_code_count: int = Field(default=0, ge=0)
    ignored_model_category_count: int = Field(default=0, ge=0)
    category_conflict_count: Literal[0] = 0
    deterministic_enrichments: list[DeterministicFindingEnrichment] = Field(
        default_factory=list
    )
    candidate_decisions: list[CandidateDecision] = Field(default_factory=list)
    canonical_risk_roots: list[CanonicalRiskRoot] = Field(default_factory=list)
    check_decisions: list[CheckDecisionResult] = Field(default_factory=list)
    supporting_primary_overlap_count: int = Field(default=0, ge=0)
    decision_summary_perspective_warning_count: int = Field(default=0, ge=0)
    proposed_severity_factor_count: int = Field(default=0, ge=0)
    accepted_severity_factor_count: int = Field(default=0, ge=0)
    rejected_severity_factor_count: int = Field(default=0, ge=0)
    selected_evidence_source_ids: list[str] = Field(default_factory=list)
    tool_call_count: Literal[0] = 0
    prompt_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(ge=0)
    trace_ids: list[str] = Field(default_factory=list, max_length=4)
    call_metrics: list[LlmCallMetric] = Field(default_factory=list, max_length=4)
    fva_assessments: list[FvaAssessmentResult] = Field(
        default_factory=list,
        max_length=1,
    )


class BaseBundleIdentity(StrictModel):
    review_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    generation_id: str = Field(min_length=1, max_length=160)
    attempt_no: int = Field(ge=1, le=2)
    contract_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    schema_version: Literal["1.0"] = "1.0"
    perspective: Literal["PARTY_A", "PARTY_B"]
    our_party: str = Field(min_length=1, max_length=500)
    counterparty: str = Field(min_length=1, max_length=500)
    review_attitude: Literal["NEUTRAL"] = "NEUTRAL"
    fixture_id: str = Field(min_length=1, max_length=160)
    plan_id: str = Field(pattern=r"^risk-plan-[0-9a-f]{32}$")
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class BaseBundleBatchMetric(StrictModel):
    unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")
    status: Literal["COMPLETED", "PARTIAL_FAILED", "FAILED"]
    start_offset_ms: int = Field(ge=0)
    wall_duration_ms: int = Field(ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    prompt_budget: PromptBudgetResult | None = None
    model_call_count: int = Field(ge=0, le=2)
    repair_count: int = Field(ge=0, le=1)
    tool_call_count: Literal[0] = 0


class BaseBundleUnitMetric(StrictModel):
    unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    status: Literal["COMPLETED", "PARTIAL_FAILED", "FAILED"]
    check_count: int = Field(ge=1, le=8)
    candidate_count: int = Field(ge=0)
    root_count: int = Field(ge=0)
    finding_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    wall_duration_ms: int = Field(ge=0)


class CrossUnitOverlapCandidate(StrictModel):
    left_unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    left_finding_id: str = Field(pattern=r"^finding-[0-9a-f]{32}$")
    left_check_code: str = Field(pattern=BASE_CHECK_CODE_PATTERN)
    right_unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    right_finding_id: str = Field(pattern=r"^finding-[0-9a-f]{32}$")
    right_check_code: str = Field(pattern=BASE_CHECK_CODE_PATTERN)
    shared_evidence_keys: list[str] = Field(min_length=1)
    action: Literal["RECORDED_NOT_MERGED"] = "RECORDED_NOT_MERGED"


class BaseBundleMetrics(StrictModel):
    queue_duration_ms: int = Field(ge=0)
    wall_duration_ms: int = Field(ge=0)
    peak_concurrency: int = Field(ge=1, le=7)
    batch_count: int = Field(default=7, ge=1, le=16)
    unit_count: Literal[5] = 5
    model_call_count: int = Field(ge=0, le=16)
    repair_count: int = Field(ge=0, le=7)
    tool_call_count: Literal[0] = 0
    prompt_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    prompt_budget_policy_version: Literal["2.0"] = PROMPT_BUDGET_POLICY_VERSION
    prompt_budget_warning_count: int = Field(default=0, ge=0, le=16)
    prompt_budget_hard_failure_count: int = Field(default=0, ge=0, le=16)
    max_provider_prompt_tokens: int | None = Field(default=None, ge=0)
    batches_over_target: list[str] = Field(default_factory=list, max_length=16)
    batches_over_hard_limit: list[str] = Field(default_factory=list, max_length=16)
    slowest_batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")
    slowest_batch_duration_ms: int = Field(ge=0)
    slowest_unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    slowest_unit_duration_ms: int = Field(ge=0)
    batch_metrics: list[BaseBundleBatchMetric] = Field(min_length=1, max_length=16)
    unit_metrics: list[BaseBundleUnitMetric] = Field(min_length=5, max_length=5)
    failed_batch_ids: list[str] = Field(default_factory=list, max_length=16)


class BaseRiskReviewBundle(StrictModel):
    bundle_version: Literal["1.0"] = "1.0"
    bundle_id: str = Field(pattern=r"^risk-bundle-[0-9a-f]{32}$")
    identity: BaseBundleIdentity
    plan_id: str = Field(pattern=r"^risk-plan-[0-9a-f]{32}$")
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["COMPLETED", "PARTIAL_FAILED"]
    units: list[BaseReviewUnitResult] = Field(min_length=5, max_length=5)
    batch_results: list[ReviewBatchResult] = Field(min_length=1, max_length=16)
    cross_unit_overlap_candidates: list[CrossUnitOverlapCandidate] = Field(
        default_factory=list
    )
    metrics: BaseBundleMetrics

    @model_validator(mode="after")
    def validate_complete_base_bundle(self) -> "BaseRiskReviewBundle":
        unit_ids = [item.unit_id for item in self.units]
        if tuple(unit_ids) != BASE_UNIT_IDS:
            raise ValueError("Base Bundle must contain the five base Units in frozen order")
        codes = [
            item.check_code for unit in self.units for item in unit.check_results
        ]
        if tuple(codes) != EXPECTED_BASE_CHECK_CODES:
            raise ValueError("Base Bundle must cover all 34 base Check codes exactly once")
        batch_ids = [item.batch_id for item in self.batch_results]
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("Base Bundle Batch IDs must be unique")
        if self.identity.plan_id != self.plan_id or self.identity.plan_hash != self.plan_hash:
            raise ValueError("Base Bundle identity must match the Plan")
        return self


class BaseBundleFailure(StrictModel):
    bundle_version: Literal["1.0"] = "1.0"
    bundle_id: str = Field(pattern=r"^risk-bundle-[0-9a-f]{32}$")
    status: Literal["FAILED"] = "FAILED"
    plan_id: str = Field(pattern=r"^risk-plan-[0-9a-f]{32}$")
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    failed_unit_id: str | None = Field(default=None, pattern=BASE_UNIT_ID_PATTERN)
    failed_batch_id: str | None = Field(
        default=None,
        pattern=r"^risk-batch-[0-9a-f]{32}$",
    )
    error_code: str = Field(min_length=1, max_length=160)
    error_message: str = Field(min_length=1, max_length=4000)
    completed_batch_count: int = Field(ge=0, le=16)
    cancelled_batch_count: int = Field(ge=0, le=16)
    in_flight_batch_count: int = Field(ge=0, le=16)
    trace_id: str = Field(min_length=1, max_length=200)
    completed_batch_ids: list[str] = Field(default_factory=list, max_length=16)
    cancelled_batch_ids: list[str] = Field(default_factory=list, max_length=16)
    diagnostic_batch_results: list[ReviewBatchResult] = Field(
        default_factory=list,
        max_length=16,
    )


class BaseBundleExecutionError(DirectReviewError):
    def __init__(self, failure: BaseBundleFailure) -> None:
        super().__init__(failure.error_code, failure.error_message)
        self.failure = failure


class GenericLlmCompleter(Protocol):
    async def complete_with_usage(self, **kwargs: Any) -> LlmCompletionResult: ...


class GenericAttemptArtifact(StrictModel):
    unit_id: str = Field(pattern=BASE_UNIT_ID_PATTERN)
    batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")
    attempt_no: int = Field(ge=1, le=2)
    attempt_type: Literal["INITIAL", "REPAIR"]
    repair_type: Literal[
        "SCHEMA_REPAIR",
        "EVIDENCE_SELECTION_REPAIR",
    ] | None = None
    repair_no: int = Field(ge=0, le=1)
    started_at: str
    completed_at: str
    raw_response: str | None = None
    extracted_raw_object: dict[str, Any] | None = None
    deterministic_normalization: dict[str, Any]
    validation_errors: dict[str, str | None]
    prompt_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)
    model_duration_ms: int | None = Field(default=None, ge=0)
    trace_id: str | None = Field(default=None, max_length=160)
    provider_request_id: str | None = Field(default=None, max_length=500)
    finish_reason: str | None = Field(default=None, max_length=160)
    repair_reason: str | None = None
    before_summary: dict[str, Any] | None = None
    after_summary: dict[str, Any] | None = None
    semantic_diff: dict[str, Any] | None = None
    semantic_preservation_passed: bool | None = None
    accepted: bool
    acceptance_reason: str = Field(min_length=1, max_length=500)
    input_diagnostics: dict[str, Any] | None = None


class GenericAttemptArtifactSink(Protocol):
    def __call__(self, value: GenericAttemptArtifact) -> None: ...


@dataclass(frozen=True, slots=True)
class GenericParsedOutput:
    response: GenericModelResponseRaw
    raw_object: dict[str, Any]
    normalization: SchemaNormalizationRecord


@dataclass(frozen=True, slots=True)
class PoEvidenceCatalog:
    evidence_sources: dict[str, RiskEvidenceSource]
    absence_sources: dict[str, RiskAbsenceEvidenceSource]
    allowed_source_ids_by_check: dict[str, tuple[str, ...]]
    candidate_source_ids: dict[
        tuple[str, str],
        tuple[tuple[str, ...], tuple[str, ...]],
    ]


@dataclass(frozen=True, slots=True)
class FindingFieldResolution:
    value: GenericModelFindingDraft
    risk_type_source: Literal["MODEL", "CANDIDATE", "UNIQUE_ALLOWED_TYPE"]
    check_code_enriched: bool
    category_enriched: bool
    risk_type_enriched: bool
    ignored_model_check_code: bool
    ignored_model_category: bool


@dataclass(slots=True)
class GenericBaseDirectReviewer:
    runtime_factory: Callable[[str], GenericLlmCompleter] = LlmRuntime

    async def review(
        self,
        request: GenericReviewRequest,
        *,
        tenant_id: str,
        model_id: str,
        framework_run_id: str | None = None,
        attempt_artifact_sink: GenericAttemptArtifactSink | None = None,
        allow_evidence_selection_repair: bool = True,
    ) -> ReviewBatchResult:
        if request.unit_id in {
            "performance_obligations",
            "ip_confidentiality_data",
            "liability_remedies_exit",
        }:
            return await _review_po_candidate_batch(
                request,
                runtime_factory=self.runtime_factory,
                tenant_id=tenant_id,
                model_id=model_id,
                framework_run_id=framework_run_id,
                attempt_artifact_sink=attempt_artifact_sink,
            )
        prompt, ir_refs, anchor_refs = _generic_prompt(request)
        input_diagnostics = generic_input_diagnostics(request, prompt)
        po_catalog: PoEvidenceCatalog | None = None
        if request.unit_id in {
            "performance_obligations",
            "ip_confidentiality_data",
            "liability_remedies_exit",
        }:
            anchor_ref_by_id = {
                item.anchor_id: ref for ref, item in anchor_refs.items()
            }
            po_catalog = _po_evidence_catalog(
                request,
                _build_generic_candidates(
                    request,
                    ir_refs,
                    anchor_ref_by_id,
                ),
                ir_refs,
                anchor_refs,
            )
        expected_codes = tuple(
            item.check_code for item in request.assigned_check_specs
        )
        runtime = self.runtime_factory(tenant_id)
        started = time.perf_counter()
        calls: list[LlmCompletionResult] = []
        diagnostics: list[LlmAttemptDiagnostic] = []
        repair_reasons: list[str] = []
        invalid_content = ""
        invalid_reason = ""
        first_snapshot: dict[str, Any] | None = None
        accepted_normalization = SchemaNormalizationRecord(applied=False)
        ignored_reason_codes = 0
        evidence_binding_normalization_count = 0
        ignored_model_link_fields_count = 0
        deterministic_enrichments: list[DeterministicFindingEnrichment] = []
        repair_type: Literal[
            "SCHEMA_REPAIR",
            "EVIDENCE_SELECTION_REPAIR",
        ] | None = None

        for repair_no in range(2):
            parsed: GenericParsedOutput | None = None
            attempt_started_at = _utc_now()
            messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
            if repair_no:
                repair_payload = _generic_repair_payload(
                    repair_type=repair_type or "SCHEMA_REPAIR",
                    invalid_content=invalid_content,
                    invalid_reason=invalid_reason,
                    expected_codes=expected_codes,
                    po_catalog=po_catalog,
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": invalid_content},
                        {
                            "role": "user",
                            "content": json.dumps(
                                repair_payload,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        },
                    ]
                )
            try:
                completion = await runtime.complete_with_usage(
                    messages=messages,
                    model_id=model_id,
                    system_prompt=_GENERIC_SYSTEM_PROMPT,
                    max_tokens=4000,
                    temperature=0,
                    thinking_override=False,
                    response_format={"type": "json_object"},
                    review_unit_id=request.unit_id,
                    review_id=request.review_id,
                    framework_run_id=framework_run_id,
                    attempt_no=request.attempt_no,
                    repair_no=repair_no,
                    **deferred_completion_kwargs(
                        calls[-1] if calls else None,
                        repair_no=repair_no,
                    ),
                )
            except Exception as exc:
                _emit_generic_attempt_artifact(
                    attempt_artifact_sink,
                    request=request,
                    repair_no=repair_no,
                    started_at=attempt_started_at,
                    completion=None,
                    raw_object=None,
                    normalization=SchemaNormalizationRecord(applied=False),
                    error=exc,
                    repair_type=repair_type,
                    repair_reason=invalid_reason or None,
                    before_snapshot=first_snapshot,
                    after_snapshot=None,
                    semantic_preservation_passed=None,
                    accepted=False,
                    acceptance_reason="MODEL_CALL_FAILED",
                    input_diagnostics=input_diagnostics,
                )
                raise
            calls.append(completion)
            try:
                enforce_provider_prompt_budget(completion, request)
                parsed = _parse_generic_output(completion.content, expected_codes)
                semantic_preservation_passed: bool | None = None
                if repair_no and first_snapshot is not None:
                    _validate_generic_semantic_preservation(
                        first_snapshot,
                        _generic_repair_snapshot(parsed.raw_object),
                        repair_type=repair_type or "SCHEMA_REPAIR",
                    )
                    semantic_preservation_passed = True
                (
                    check_results,
                    findings,
                    ignored_reason_codes,
                    fva_assessments,
                    evidence_binding_normalization_count,
                    ignored_model_link_fields_count,
                    deterministic_enrichments,
                ) = _materialize_generic(
                    request,
                    parsed.response,
                    ir_refs,
                    anchor_refs,
                )
                diagnostics.append(
                    _attempt_diagnostic(
                        completion,
                        validation_error=None,
                        normalization=parsed.normalization,
                        semantic_preservation_passed=semantic_preservation_passed,
                    )
                )
                accepted_snapshot = _generic_repair_snapshot(parsed.raw_object)
                if (
                    completion.completion_tokens is not None
                    and completion.completion_tokens > 4000
                ):
                    raise DirectReviewError(
                        "RISK_OUTPUT_BUDGET_EXCEEDED",
                        f"{request.unit_id} exceeded the hard output token limit",
                    )
                _emit_generic_attempt_artifact(
                    attempt_artifact_sink,
                    request=request,
                    repair_no=repair_no,
                    started_at=attempt_started_at,
                    completion=completion,
                    raw_object=parsed.raw_object,
                    normalization=parsed.normalization,
                    error=None,
                    repair_type=repair_type,
                    repair_reason=invalid_reason or None,
                    before_snapshot=first_snapshot,
                    after_snapshot=accepted_snapshot,
                    semantic_preservation_passed=semantic_preservation_passed,
                    accepted=True,
                    acceptance_reason="ACCEPTED",
                    input_diagnostics=input_diagnostics,
                )
                accepted_normalization = parsed.normalization
                await finalize_completion_success(completion)
                break
            except DirectReviewError as exc:
                await finalize_completion_validation_failed(completion, exc.code)
                raw_object = (
                    parsed.raw_object
                    if parsed is not None
                    else exc.structured_output
                )
                after_snapshot = _generic_repair_snapshot(raw_object)
                diagnostics.append(
                    _attempt_diagnostic(
                        completion,
                        validation_error=f"{exc.code}: {exc}",
                        normalization=SchemaNormalizationRecord(applied=False),
                        semantic_preservation_passed=(
                            False if exc.code == "RISK_REPAIR_SEMANTICS_CHANGED" else None
                        ),
                    )
                )
                next_repair_type = (
                    _generic_repair_type(exc, po_catalog)
                    if exc.repairable and repair_no == 0
                    else None
                )
                will_repair = (
                    next_repair_type is not None
                    and (
                        allow_evidence_selection_repair
                        or next_repair_type != "EVIDENCE_SELECTION_REPAIR"
                    )
                )
                _emit_generic_attempt_artifact(
                    attempt_artifact_sink,
                    request=request,
                    repair_no=repair_no,
                    started_at=attempt_started_at,
                    completion=completion,
                    raw_object=raw_object,
                    normalization=(
                        parsed.normalization
                        if parsed is not None
                        else SchemaNormalizationRecord(applied=False)
                    ),
                    error=exc,
                    repair_type=repair_type,
                    repair_reason=invalid_reason or None,
                    before_snapshot=first_snapshot,
                    after_snapshot=after_snapshot,
                    semantic_preservation_passed=(
                        False
                        if exc.code == "RISK_REPAIR_SEMANTICS_CHANGED"
                        else None
                    ),
                    accepted=False,
                    acceptance_reason=(
                        "REPAIR_REQUIRED" if will_repair else exc.code
                    ),
                    input_diagnostics=input_diagnostics,
                )
                if not will_repair:
                    exc.attempt_diagnostics = diagnostics
                    raise
                invalid_content = completion.content
                invalid_reason = f"{exc.code}: {exc}"
                repair_type = next_repair_type
                first_snapshot = _generic_repair_snapshot(
                    exc.structured_output
                    if exc.structured_output is not None
                    else (parsed.raw_object if parsed is not None else None)
                )
                repair_reasons.append(invalid_reason)
            except asyncio.CancelledError:
                await finalize_completion_validation_failed(
                    completion,
                    "MODEL_OUTPUT_PROCESSING_CANCELLED",
                )
                raise
            except Exception:
                await finalize_completion_validation_failed(
                    completion,
                    "MODEL_OUTPUT_PROCESSING_FAILED",
                )
                raise
        else:  # pragma: no cover
            raise DirectReviewError(
                "RISK_DIRECT_OUTPUT_INVALID",
                "Direct review did not return a result",
            )

        duration_ms = round((time.perf_counter() - started) * 1000)
        metrics = [_generic_metric(item, request.unit_id) for item in calls]
        final_completion_tokens = calls[-1].completion_tokens
        if final_completion_tokens is not None and final_completion_tokens > 4000:
            raise DirectReviewError(
                "RISK_OUTPUT_BUDGET_EXCEEDED",
                f"{request.unit_id} exceeded the hard output token limit",
            )
        warnings = []
        if final_completion_tokens is not None and final_completion_tokens > 2500:
            warnings.append("RISK_OUTPUT_SOFT_LIMIT_EXCEEDED")
        return ReviewBatchResult(
            unit_id=request.unit_id,
            domain=request.unit_id,
            batch_id=request.batch_id,
            status=(
                "PARTIAL_FAILED"
                if any(
                    item.status == "FAILED"
                    or item.reason_code == "INSUFFICIENT_EVIDENCE"
                    for item in check_results
                )
                else "COMPLETED"
            ),
            check_results=check_results,
            findings=findings,
            warnings=warnings,
            model_call_count=len(calls),
            repair_count=len(calls) - 1,
            schema_repair_count=sum(
                item.repair_no == 1 and repair_type == "SCHEMA_REPAIR"
                for item in metrics
            ),
            evidence_selection_repair_count=sum(
                item.repair_no == 1
                and repair_type == "EVIDENCE_SELECTION_REPAIR"
                for item in metrics
            ),
            evidence_binding_normalization_count=(
                evidence_binding_normalization_count
            ),
            ignored_model_link_fields_count=ignored_model_link_fields_count,
            selected_evidence_source_ids=_selected_po_source_ids(
                findings,
                po_catalog,
            ),
            prompt_tokens=_sum_optional(item.prompt_tokens for item in calls),
            cached_tokens=_sum_optional(item.cached_tokens for item in calls),
            completion_tokens=_sum_optional(
                item.completion_tokens for item in calls
            ),
            total_tokens=_sum_optional(item.total_tokens for item in calls),
            duration_ms=duration_ms,
            trace_ids=[item.trace_id for item in calls],
            call_metrics=metrics,
            repair_reasons=repair_reasons,
            schema_normalization_applied=accepted_normalization.applied,
            schema_normalization_type=accepted_normalization.normalization_type,
            attempt_diagnostics=diagnostics,
            reason_code_enrichment_count=len(check_results),
            reason_code_rule_version="1.0",
            ignored_model_reason_code_count=ignored_reason_codes,
            deterministic_enrichment_count=sum(
                item.check_code_enriched
                + item.category_enriched
                + item.risk_type_enriched
                for item in deterministic_enrichments
            ),
            check_code_enrichment_count=sum(
                item.check_code_enriched for item in deterministic_enrichments
            ),
            category_enrichment_count=sum(
                item.category_enriched for item in deterministic_enrichments
            ),
            risk_type_enrichment_count=sum(
                item.risk_type_enriched for item in deterministic_enrichments
            ),
            ignored_model_check_code_count=sum(
                item.ignored_model_check_code for item in deterministic_enrichments
            ),
            ignored_model_category_count=sum(
                item.ignored_model_category for item in deterministic_enrichments
            ),
            deterministic_enrichments=deterministic_enrichments,
            fva_assessments=fva_assessments,
        )


async def _review_po_candidate_batch(
    request: GenericReviewRequest,
    *,
    runtime_factory: Callable[[str], GenericLlmCompleter],
    tenant_id: str,
    model_id: str,
    framework_run_id: str | None,
    attempt_artifact_sink: GenericAttemptArtifactSink | None,
) -> ReviewBatchResult:
    prompt, ir_refs, anchor_refs = _generic_prompt(request)
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )
    model_candidates = [
        item for item in candidates if item.requires_model_decision
    ]
    candidates_by_id = {item.candidate_id: item for item in model_candidates}
    expected_ids = tuple(item.candidate_id for item in model_candidates)
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise DirectReviewError(
            "RISK_CANDIDATE_DUPLICATED",
            "Deterministic candidate IDs must be unique",
        )
    if not expected_ids:
        raise DirectReviewError(
            "RISK_MODEL_CANDIDATE_EMPTY",
            "The current PO Batch has no Candidate requiring model review",
        )
    catalog = _po_evidence_catalog(request, candidates, ir_refs, anchor_refs)
    runtime = runtime_factory(tenant_id)
    calls: list[LlmCompletionResult] = []
    diagnostics: list[LlmAttemptDiagnostic] = []
    repair_reasons: list[str] = []
    invalid_content = ""
    invalid_reason = ""
    first_snapshot: dict[str, Any] | None = None
    started = time.perf_counter()

    for repair_no in range(2):
        parsed_object: dict[str, Any] | None = None
        attempt_started_at = _utc_now()
        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
        if repair_no:
            messages.extend(
                [
                    {"role": "assistant", "content": invalid_content},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "task": "SCHEMA_REPAIR",
                                "exact_validation_error": invalid_reason,
                                "required_candidate_ids": list(expected_ids),
                                "constraints": [
                                    "只修JSON、字段名或非语义类型错误",
                                    "不得新增或删除CandidateDecision",
                                    "不得改变candidate_id、verdict、decision_summary或severity_factors",
                                    "不得改变Supporting、Counter或recommended_control_codes",
                                    "不得补写首轮遗漏的Candidate裁决",
                                    "顶层只能是candidate_decisions",
                                ],
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    },
                ]
            )
        try:
            completion = await runtime.complete_with_usage(
                messages=messages,
                model_id=model_id,
                system_prompt=_PO_CANDIDATE_SYSTEM_PROMPT,
                max_tokens=4000,
                temperature=0,
                thinking_override=False,
                response_format={"type": "json_object"},
                review_unit_id=request.unit_id,
                review_id=request.review_id,
                framework_run_id=framework_run_id,
                attempt_no=request.attempt_no,
                repair_no=repair_no,
                **deferred_completion_kwargs(
                    calls[-1] if calls else None,
                    repair_no=repair_no,
                ),
            )
        except Exception as exc:
            _emit_generic_attempt_artifact(
                attempt_artifact_sink,
                request=request,
                repair_no=repair_no,
                started_at=attempt_started_at,
                completion=None,
                raw_object=None,
                normalization=SchemaNormalizationRecord(applied=False),
                error=exc,
                repair_type="SCHEMA_REPAIR" if repair_no else None,
                repair_reason=invalid_reason or None,
                before_snapshot=None,
                after_snapshot=None,
                semantic_preservation_passed=None,
                accepted=False,
                acceptance_reason="MODEL_CALL_FAILED",
            )
            raise
        calls.append(completion)
        try:
            enforce_provider_prompt_budget(completion, request)
            response, parsed_object = _parse_po_candidate_output(
                completion.content,
                expected_ids=expected_ids,
                candidates_by_id=candidates_by_id,
            )
            current_snapshot = _po_decision_snapshot(parsed_object)
            semantic_preservation_passed: bool | None = None
            if repair_no:
                _validate_po_decision_semantic_preservation(
                    first_snapshot,
                    current_snapshot,
                )
                semantic_preservation_passed = True
            (
                check_results,
                findings,
                candidate_decisions,
                canonical_risk_roots,
                check_decisions,
                supporting_primary_overlap_count,
                decision_summary_perspective_warning_count,
            ) = _materialize_po_candidate_decisions(
                request,
                response,
                candidates,
                catalog,
                ir_refs,
                anchor_refs,
            )
            diagnostics.append(
                _attempt_diagnostic(
                    completion,
                    validation_error=None,
                    normalization=SchemaNormalizationRecord(applied=False),
                    semantic_preservation_passed=semantic_preservation_passed,
                )
            )
            if (
                completion.completion_tokens is not None
                and completion.completion_tokens > 4000
            ):
                raise DirectReviewError(
                    "RISK_OUTPUT_BUDGET_EXCEEDED",
                    "performance_obligations exceeded the hard output token limit",
                )
            _emit_generic_attempt_artifact(
                attempt_artifact_sink,
                request=request,
                repair_no=repair_no,
                started_at=attempt_started_at,
                completion=completion,
                raw_object=parsed_object,
                normalization=SchemaNormalizationRecord(applied=False),
                error=None,
                repair_type="SCHEMA_REPAIR" if repair_no else None,
                repair_reason=invalid_reason or None,
                before_snapshot=None,
                after_snapshot=None,
                semantic_preservation_passed=semantic_preservation_passed,
                accepted=True,
                acceptance_reason="ACCEPTED",
            )
            await finalize_completion_success(completion)
            break
        except DirectReviewError as exc:
            await finalize_completion_validation_failed(completion, exc.code)
            parsed_object = (
                parsed_object
                if parsed_object is not None
                else exc.structured_output
            )
            diagnostics.append(
                _attempt_diagnostic(
                    completion,
                    validation_error=f"{exc.code}: {exc}",
                    normalization=SchemaNormalizationRecord(applied=False),
                    semantic_preservation_passed=None,
                )
            )
            can_repair = (
                repair_no == 0
                and exc.repairable
                and exc.code == "RISK_DIRECT_SCHEMA_INVALID"
                and _po_has_exact_candidate_ids(parsed_object, expected_ids)
            )
            _emit_generic_attempt_artifact(
                attempt_artifact_sink,
                request=request,
                repair_no=repair_no,
                started_at=attempt_started_at,
                completion=completion,
                raw_object=parsed_object,
                normalization=SchemaNormalizationRecord(applied=False),
                error=exc,
                repair_type="SCHEMA_REPAIR" if repair_no else None,
                repair_reason=invalid_reason or None,
                before_snapshot=None,
                after_snapshot=None,
                semantic_preservation_passed=None,
                accepted=False,
                acceptance_reason="REPAIR_REQUIRED" if can_repair else exc.code,
            )
            if not can_repair:
                exc.attempt_diagnostics = diagnostics
                raise
            invalid_content = completion.content
            invalid_reason = f"{exc.code}: {exc}"
            first_snapshot = _po_decision_snapshot(parsed_object)
            repair_reasons.append(invalid_reason)
        except asyncio.CancelledError:
            await finalize_completion_validation_failed(
                completion,
                "MODEL_OUTPUT_PROCESSING_CANCELLED",
            )
            raise
        except Exception:
            await finalize_completion_validation_failed(
                completion,
                "MODEL_OUTPUT_PROCESSING_FAILED",
            )
            raise
    else:  # pragma: no cover
        raise DirectReviewError(
            "RISK_DIRECT_OUTPUT_INVALID",
            "PO candidate review did not return a result",
        )

    duration_ms = round((time.perf_counter() - started) * 1000)
    metrics = [_generic_metric(item, request.unit_id) for item in calls]
    final_completion_tokens = calls[-1].completion_tokens
    if final_completion_tokens is not None and final_completion_tokens > 4000:
        raise DirectReviewError(
            "RISK_OUTPUT_BUDGET_EXCEEDED",
            "performance_obligations exceeded the hard output token limit",
        )
    warnings = []
    if final_completion_tokens is not None and final_completion_tokens > 2500:
        warnings.append("RISK_OUTPUT_SOFT_LIMIT_EXCEEDED")
    return ReviewBatchResult(
        unit_id=request.unit_id,
        domain=request.unit_id,
        batch_id=request.batch_id,
        status=(
            "PARTIAL_FAILED"
            if any(
                item.status == "FAILED"
                or item.reason_code == "INSUFFICIENT_EVIDENCE"
                for item in check_results
            )
            else "COMPLETED"
        ),
        check_results=check_results,
        findings=findings,
        warnings=warnings,
        model_call_count=len(calls),
        repair_count=len(calls) - 1,
        schema_repair_count=len(calls) - 1,
        evidence_selection_repair_count=0,
        evidence_binding_normalization_count=0,
        ignored_model_link_fields_count=0,
        selected_evidence_source_ids=_selected_po_source_ids(findings, catalog),
        prompt_tokens=_sum_optional(item.prompt_tokens for item in calls),
        cached_tokens=_sum_optional(item.cached_tokens for item in calls),
        completion_tokens=_sum_optional(item.completion_tokens for item in calls),
        total_tokens=_sum_optional(item.total_tokens for item in calls),
        duration_ms=duration_ms,
        trace_ids=[item.trace_id for item in calls],
        call_metrics=metrics,
        repair_reasons=repair_reasons,
        schema_normalization_applied=False,
        schema_normalization_type=None,
        attempt_diagnostics=diagnostics,
        reason_code_enrichment_count=len(check_results),
        reason_code_rule_version="1.0",
        ignored_model_reason_code_count=0,
        candidate_decisions=candidate_decisions,
        canonical_risk_roots=canonical_risk_roots,
        check_decisions=check_decisions,
        supporting_primary_overlap_count=supporting_primary_overlap_count,
        decision_summary_perspective_warning_count=(
            decision_summary_perspective_warning_count
        ),
        proposed_severity_factor_count=sum(
            len(item.proposed_semantic_severity_factors)
            for item in candidate_decisions
        ),
        accepted_severity_factor_count=sum(
            len(item.validated_semantic_severity_factors)
            for item in candidate_decisions
        ),
        rejected_severity_factor_count=sum(
            len(item.rejected_severity_factors)
            for item in candidate_decisions
        ),
    )


def _parse_po_candidate_output(
    content: str,
    *,
    expected_ids: tuple[str, ...],
    candidates_by_id: dict[str, DeterministicRiskCandidate],
) -> tuple[CandidateDecisionResponseRaw, dict[str, Any]]:
    raw_object: dict[str, Any] | None = None
    try:
        parsed = parse_json_output(content)
        if not parsed.ok or not isinstance(parsed.structured, dict):
            raise ValueError("model did not return one complete JSON object")
        raw_object = parsed.structured
        decisions = raw_object.get("candidate_decisions")
        if isinstance(decisions, list):
            ids = [
                item.get("candidate_id")
                for item in decisions
                if isinstance(item, dict)
            ]
            if len(ids) != len(decisions) or any(not isinstance(item, str) for item in ids):
                raise DirectReviewError(
                    "RISK_CANDIDATE_MISSING",
                    "Every CandidateDecision must contain candidate_id",
                    structured_output=raw_object,
                )
            if len(ids) != len(set(ids)):
                raise DirectReviewError(
                    "RISK_CANDIDATE_DECISION_DUPLICATED",
                    "A Candidate was decided more than once",
                    structured_output=raw_object,
                )
            unknown = [item for item in ids if item not in candidates_by_id]
            if unknown:
                raise DirectReviewError(
                    "RISK_CANDIDATE_UNKNOWN",
                    f"Unknown CandidateDecision IDs: {unknown}",
                    structured_output=raw_object,
                )
            if tuple(ids) != expected_ids:
                missing = [item for item in expected_ids if item not in set(ids)]
                raise DirectReviewError(
                    "RISK_CANDIDATE_COVERAGE_INVALID",
                    f"CandidateDecision coverage/order is invalid; missing={missing}",
                    structured_output=raw_object,
                )
        response = CandidateDecisionResponseRaw.model_validate(raw_object)
        if tuple(item.candidate_id for item in response.candidate_decisions) != expected_ids:
            raise DirectReviewError(
                "RISK_CANDIDATE_COVERAGE_INVALID",
                "Every assigned Candidate must be decided once in input order",
                structured_output=raw_object,
            )
        return response, raw_object
    except DirectReviewError:
        raise
    except (ValueError, TypeError, ValidationError, json.JSONDecodeError) as exc:
        raise DirectReviewError(
            "RISK_DIRECT_SCHEMA_INVALID",
            f"PO CandidateDecision output is invalid: {exc}",
            repairable=True,
            structured_output=raw_object,
        ) from exc


def _po_has_exact_candidate_ids(
    value: dict[str, Any] | None,
    expected_ids: tuple[str, ...],
) -> bool:
    if not isinstance(value, dict):
        return False
    decisions = value.get("candidate_decisions")
    if not isinstance(decisions, list):
        return False
    ids = [
        item.get("candidate_id")
        for item in decisions
        if isinstance(item, dict)
    ]
    return len(ids) == len(decisions) and tuple(ids) == expected_ids


def _po_decision_snapshot(
    value: dict[str, Any] | None,
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(value, dict) or not isinstance(
        value.get("candidate_decisions"),
        list,
    ):
        return None
    result: dict[str, dict[str, Any]] = {}
    for item in value["candidate_decisions"]:
        if not isinstance(item, dict) or not isinstance(item.get("candidate_id"), str):
            return None
        candidate_id = item["candidate_id"]
        if candidate_id in result:
            return None
        result[candidate_id] = dict(item)
    return result


def _validate_po_decision_semantic_preservation(
    before: dict[str, dict[str, Any]] | None,
    after: dict[str, dict[str, Any]] | None,
) -> None:
    if before is None or after is None or set(before) != set(after):
        raise DirectReviewError(
            "RISK_REPAIR_SEMANTICS_CHANGED",
            "Schema Repair changed CandidateDecision coverage",
        )
    for candidate_id in before:
        previous = before[candidate_id]
        current = after[candidate_id]
        for field, previous_value in previous.items():
            if field == "candidate_id":
                continue
            if previous_value is not None and current.get(field) != previous_value:
                raise DirectReviewError(
                    "RISK_REPAIR_SEMANTICS_CHANGED",
                    f"Schema Repair changed {field} for {candidate_id}",
                )


def _materialize_po_candidate_decisions(
    request: GenericReviewRequest,
    response: CandidateDecisionResponseRaw,
    candidates: list[DeterministicRiskCandidate],
    catalog: PoEvidenceCatalog,
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
) -> tuple[
    list[CheckCoverageResult],
    list[FindingDraft],
    list[CandidateDecision],
    list[CanonicalRiskRoot],
    list[CheckDecisionResult],
    int,
    int,
]:
    specs = {item.check_code: item for item in request.assigned_check_specs}
    candidates_by_id = {item.candidate_id: item for item in candidates}
    expected_ids = tuple(
        item.candidate_id
        for item in candidates
        if item.requires_model_decision
    )
    raw_by_id = {item.candidate_id: item for item in response.candidate_decisions}
    if (
        len(candidates_by_id) != len(candidates)
        or len(raw_by_id) != len(response.candidate_decisions)
        or tuple(raw_by_id) != expected_ids
    ):
        raise DirectReviewError(
            "RISK_CANDIDATE_COVERAGE_INVALID",
            "Every assigned PO Candidate must have exactly one Decision",
        )

    final_decisions: list[CandidateDecision] = []
    findings: list[FindingDraft] = []
    finding_by_candidate: dict[str, str] = {}
    supporting_primary_overlap_count = 0
    decision_summary_perspective_warning_count = 0
    for candidate in candidates:
        if not candidate.requires_model_decision:
            po_precondition = candidate.po003_precondition
            icd_precondition = candidate.icd_perspective_precondition
            if po_precondition is not None:
                if po_precondition.model_review_required:
                    raise DirectReviewError(
                        "RISK_CANDIDATE_PRECONDITION_INVALID",
                        "A deterministic PO Candidate has a reviewable precondition",
                    )
                decision_summary = (
                    "确定性前置条件未成立："
                    + "；".join(po_precondition.unmet_conditions)
                )
            elif icd_precondition is not None:
                if icd_precondition.model_review_required:
                    raise DirectReviewError(
                        "RISK_CANDIDATE_PRECONDITION_INVALID",
                        "A deterministic ICD Candidate has a reviewable precondition",
                    )
                decision_summary = icd_precondition.decision_reason
            else:
                raise DirectReviewError(
                    "RISK_CANDIDATE_PRECONDITION_INVALID",
                    "A deterministic Candidate lacks a failed precondition",
                )
            final_decisions.append(
                CandidateDecision(
                    candidate_id=candidate.candidate_id,
                    check_code=candidate.check_code,
                    candidate_type=candidate.candidate_type,
                    verdict="NO_RISK",
                    reason_code="NO_RISK_SUPPORTED",
                    risk_level=None,
                    primary_evidence_source_ids=(
                        candidate.primary_evidence_source_ids
                    ),
                    supporting_evidence_source_ids=[],
                    counter_evidence_source_ids=[],
                    decision_summary=decision_summary,
                    severity_factors=_po_severity_factors([]),
                    proposed_semantic_severity_factors=[],
                    validated_semantic_severity_factors=[],
                    rejected_severity_factors=[],
                    decision_source="DETERMINISTIC_PRECONDITION",
                    po003_precondition=po_precondition,
                    icd_perspective_precondition=icd_precondition,
                    recommended_control_codes=[],
                )
            )
            continue
        raw = raw_by_id[candidate.candidate_id]
        primary_ids = set(candidate.primary_evidence_source_ids)
        supporting_primary_overlap_count += sum(
            source_id in primary_ids
            for source_id in raw.supporting_evidence_source_ids
        )
        normalized_supporting_ids = [
            source_id
            for source_id in raw.supporting_evidence_source_ids
            if source_id not in primary_ids
        ]
        supporting_ids = _validate_po_selected_sources(
            normalized_supporting_ids,
            candidate.allowed_supporting_evidence_source_ids,
            candidate=candidate,
            role="Supporting",
            catalog=catalog,
        )
        counter_ids = _validate_po_selected_sources(
            raw.counter_evidence_source_ids,
            _candidate_allowed_source_ids(candidate, "Counter"),
            candidate=candidate,
            role="Counter",
            catalog=catalog,
        )
        accepted_semantic_factors, rejected_semantic_factors = (
            _validate_po_semantic_severity_factors(
                candidate,
                raw.severity_factors,
                supporting_ids=supporting_ids,
                catalog=catalog,
            )
        )
        severity_factors = _merge_po_severity_factors(
            _po_severity_factors(candidate.deterministic_severity_factors),
            _po_severity_factors(accepted_semantic_factors),
        )
        control_codes = _validate_po_control_codes(
            candidate,
            raw.recommended_control_codes,
            verdict=raw.verdict,
        )
        decision_summary_perspective_warning_count += (
            _po_decision_summary_perspective_warning_count(
                request,
                raw.decision_summary,
            )
        )
        _validate_po_candidate_verdict(
            candidate,
            raw,
            counter_ids=counter_ids,
            severity_factors=severity_factors,
        )
        risk_level = (
            _candidate_risk_level(candidate, severity_factors)
            if raw.verdict == "RISK"
            else None
        )
        reason_code: Literal[
            "RISK_IDENTIFIED",
            "NO_RISK_SUPPORTED",
            "INSUFFICIENT_EVIDENCE",
        ] = (
            "RISK_IDENTIFIED"
            if raw.verdict == "RISK"
            else (
                "INSUFFICIENT_EVIDENCE"
                if raw.verdict == "INSUFFICIENT_EVIDENCE"
                else "NO_RISK_SUPPORTED"
            )
        )
        decision = CandidateDecision(
            candidate_id=candidate.candidate_id,
            check_code=candidate.check_code,
            candidate_type=candidate.candidate_type,
            verdict=raw.verdict,
            reason_code=reason_code,
            risk_level=risk_level,
            primary_evidence_source_ids=candidate.primary_evidence_source_ids,
            supporting_evidence_source_ids=supporting_ids,
            counter_evidence_source_ids=counter_ids,
            decision_summary=raw.decision_summary,
            severity_factors=severity_factors,
            proposed_semantic_severity_factors=raw.severity_factors,
            validated_semantic_severity_factors=accepted_semantic_factors,
            rejected_severity_factors=rejected_semantic_factors,
            decision_source="MODEL",
            po003_precondition=candidate.po003_precondition,
            icd_perspective_precondition=candidate.icd_perspective_precondition,
            recommended_control_codes=control_codes,
        )
        final_decisions.append(decision)

    canonical_roots: list[CanonicalRiskRoot] = []
    decisions_by_id = {
        decision.candidate_id: decision for decision in final_decisions
    }
    for root_candidates in _po_canonical_root_groups(
        candidates,
        decisions_by_id,
        specs,
        catalog,
    ):
        root_decisions = [
            decisions_by_id[candidate.candidate_id]
            for candidate in root_candidates
        ]
        spec = specs[root_candidates[0].check_code]
        risk_type = _po_candidate_risk_type(root_candidates[0], spec)
        source_candidate_ids = [
            candidate.candidate_id for candidate in root_candidates
        ]
        core_primary_ids = list(
            dict.fromkeys(
                source_id
                for candidate in root_candidates
                for source_id in candidate.core_primary_evidence_source_ids
            )
        )
        context_primary_ids = [
            source_id
            for source_id in dict.fromkeys(
                source_id
                for candidate in root_candidates
                for source_id in candidate.context_primary_evidence_source_ids
            )
            if source_id not in set(core_primary_ids)
        ]
        primary_ids = [*core_primary_ids, *context_primary_ids]
        supporting_ids = [
            source_id
            for source_id in dict.fromkeys(
                source_id
                for decision in root_decisions
                for source_id in decision.supporting_evidence_source_ids
            )
            if source_id not in set(primary_ids)
        ]
        severity_factors = _merge_po_severity_factors(
            *(decision.severity_factors for decision in root_decisions)
        )
        control_codes = list(
            dict.fromkeys(
                code
                for decision in root_decisions
                for code in decision.recommended_control_codes
            )
        )
        root_rule_ids = {
            candidate.root_severity_rule_id for candidate in root_candidates
        }
        if len(root_rule_ids) != 1:
            raise DirectReviewError(
                "RISK_ROOT_POLICY_INVALID",
                "Merged Candidates disagree on the Root severity rule",
            )
        root_rule_id = next(iter(root_rule_ids))
        risk_level = _candidate_root_risk_level(
            root_rule_id,
            severity_factors,
            [decision.risk_level for decision in root_decisions],
        )
        root_id = _stable_id(
            "risk-root",
            {
                "domain": request.unit_id,
                "check_code": root_candidates[0].check_code,
                "risk_type": risk_type,
                "root_type": root_candidates[0].canonical_root_type,
                "core_primary_evidence_source_ids": sorted(core_primary_ids),
            },
        )
        evidence_source_ids = [*primary_ids, *supporting_ids]
        title, issue, impact, suggestion = _po_root_formal_finding_text(
            request,
            root_candidates,
            severity_factors=severity_factors,
            control_codes=control_codes,
            catalog=catalog,
        )
        model_finding = GenericModelFindingDraft(
            check_code=root_candidates[0].check_code,
            category=spec.allowed_categories[0],
            candidate_ids=source_candidate_ids,
            risk_type=risk_type,
            risk_level=risk_level,
            title=title,
            issue=issue,
            impact_to_our_party=impact,
            suggestion=suggestion,
            evidence_source_ids=evidence_source_ids,
        )
        finding, _, _ = _generic_finding(
            request,
            model_finding,
            ir_refs,
            anchor_refs,
            catalog,
            po_allowed_source_ids=evidence_source_ids,
        )
        _validate_domain_safety(finding)
        _validate_po_perspective_language(request, finding)
        findings.append(finding)
        canonical_roots.append(
            CanonicalRiskRoot(
                root_id=root_id,
                domain=request.unit_id,
                check_code=root_candidates[0].check_code,
                risk_type=risk_type,
                root_type=root_candidates[0].canonical_root_type,
                source_candidate_ids=source_candidate_ids,
                primary_evidence_source_ids=primary_ids,
                core_primary_evidence_source_ids=core_primary_ids,
                context_primary_evidence_source_ids=context_primary_ids,
                supporting_evidence_source_ids=supporting_ids,
                severity_factors=severity_factors,
                recommended_control_codes=control_codes,
                root_severity_rule_id=root_rule_id,
                risk_level=risk_level,
                finding_local_id=finding.finding_local_id,
            )
        )
        for candidate_id in source_candidate_ids:
            finding_by_candidate[candidate_id] = finding.finding_local_id

    finding_ids = [item.finding_local_id for item in findings]
    evidence_ids = [
        evidence.evidence_local_id
        for finding in findings
        for evidence in finding.evidence_candidates
    ]
    if (
        len(finding_ids) != len(set(finding_ids))
        or len(evidence_ids) != len(set(evidence_ids))
    ):
        raise DirectReviewError(
            "RISK_FINDING_DUPLICATED",
            "Candidate materialization produced duplicate Finding or Evidence IDs",
        )

    coverage: list[CheckCoverageResult] = []
    check_decisions: list[CheckDecisionResult] = []
    for spec in request.assigned_check_specs:
        check_candidates = [
            item for item in candidates if item.check_code == spec.check_code
        ]
        check_candidate_ids = [item.candidate_id for item in check_candidates]
        decisions = [
            item
            for item in final_decisions
            if item.check_code == spec.check_code
        ]
        if len(decisions) != len(check_candidates):
            raise DirectReviewError(
                "RISK_CANDIDATE_COVERAGE_INVALID",
                f"{spec.check_code} is incomplete at Candidate level",
            )
        local_ids = list(
            dict.fromkeys(
                finding_by_candidate[candidate_id]
                for candidate_id in check_candidate_ids
                if candidate_id in finding_by_candidate
            )
        )
        reason_code: Literal[
            "RISK_IDENTIFIED",
            "NO_RISK_IDENTIFIED",
            "INSUFFICIENT_EVIDENCE",
        ] = (
            "RISK_IDENTIFIED"
            if local_ids
            else (
                "INSUFFICIENT_EVIDENCE"
                if any(
                    item.verdict == "INSUFFICIENT_EVIDENCE"
                    for item in decisions
                )
                else "NO_RISK_IDENTIFIED"
            )
        )
        decision_note = "；".join(
            f"{item.candidate_type}:{item.decision_summary}" for item in decisions
        )
        if not decision_note:
            decision_note = (
                "未发现满足当前检查成立前置条件的合同场景，"
                "Python确定性判定不生成风险Candidate"
            )
        coverage.append(
            CheckCoverageResult(
                check_code=spec.check_code,
                status="REVIEWED",
                reason_code=reason_code,
                decision_note=decision_note[:1000],
                finding_local_ids=local_ids,
            )
        )
        check_decisions.append(
            CheckDecisionResult(
                check_code=spec.check_code,
                status="REVIEWED",
                reason_code=reason_code,
                candidate_ids=check_candidate_ids,
                finding_local_ids=local_ids,
            )
        )
    return (
        coverage,
        findings,
        final_decisions,
        canonical_roots,
        check_decisions,
        supporting_primary_overlap_count,
        decision_summary_perspective_warning_count,
    )


def _candidate_allowed_source_ids(
    candidate: DeterministicRiskCandidate,
    role: Literal["Supporting", "Counter"],
) -> list[str]:
    allowed = (
        candidate.allowed_supporting_evidence_source_ids
        if role == "Supporting"
        else candidate.allowed_counter_evidence_source_ids
    )
    if role == "Counter" and candidate.check_code.startswith("LRE-"):
        return list(
            dict.fromkeys(
                [
                    *candidate.primary_evidence_source_ids,
                    *allowed,
                ]
            )
        )
    return list(allowed)


def _validate_po_selected_sources(
    selected: list[str],
    allowed: list[str],
    *,
    candidate: DeterministicRiskCandidate,
    role: Literal["Supporting", "Counter"],
    catalog: PoEvidenceCatalog,
) -> list[str]:
    if len(selected) != len(set(selected)):
        raise DirectReviewError(
            f"RISK_{role.upper()}_EVIDENCE_DUPLICATED",
            f"{role} Evidence Source IDs must be unique",
        )
    allowed_set = set(allowed)
    invalid = [source_id for source_id in selected if source_id not in allowed_set]
    if invalid:
        raise DirectReviewError(
            f"RISK_{role.upper()}_EVIDENCE_NOT_ALLOWED",
            f"{role} Evidence crossed its Candidate boundary: {invalid}",
        )
    known = set(catalog.evidence_sources) | set(catalog.absence_sources)
    if any(source_id not in known for source_id in selected):
        raise DirectReviewError(
            f"RISK_{role.upper()}_EVIDENCE_UNKNOWN",
            f"{role} Evidence is not part of the current Batch/Generation",
        )
    candidate_allowed = _candidate_allowed_source_ids(candidate, role)
    if allowed != candidate_allowed:
        raise DirectReviewError(
            "RISK_CANDIDATE_EVIDENCE_POLICY_INVALID",
            "Candidate Evidence role policy changed during materialization",
        )
    return list(selected)


def _validate_po_candidate_verdict(
    candidate: DeterministicRiskCandidate,
    raw: CandidateDecisionRaw,
    *,
    counter_ids: list[str],
    severity_factors: CandidateSeverityFactors,
) -> None:
    if raw.verdict == "NO_RISK":
        if (
            candidate.candidate_strength in {"HARD_RULE", "STRONG_SIGNAL"}
            and not counter_ids
        ):
            raise DirectReviewError(
                "RISK_NEGATIVE_DECISION_UNSUPPORTED",
                f"{candidate.candidate_strength} Candidate requires Counter Evidence",
            )
        if any(
            source_id.startswith("risk-as-")
            for source_id in counter_ids
        ):
            raise DirectReviewError(
                "RISK_NEGATIVE_DECISION_UNSUPPORTED",
                "Absence Evidence cannot prove that a risk is mitigated",
            )
    if raw.verdict != "RISK":
        return
    factors = severity_factors
    if (
        candidate.candidate_type.endswith("_ABSENT")
        and not factors.missing_core_mechanism
    ):
        raise DirectReviewError(
            "SEVERITY_FACTORS_INVALID",
            "An absence Candidate marked RISK must confirm missing_core_mechanism",
        )
    if candidate.candidate_type in {
        "SCOPE_EXPANSION",
        "RIGHTS_OBLIGATIONS_IMBALANCE",
        "CHANGE_CONTROL_REVIEW",
    } and not factors.unilateral_control:
        raise DirectReviewError(
            "SEVERITY_FACTORS_INVALID",
            "A unilateral-control Candidate marked RISK must confirm "
            "unilateral_control",
        )


_PO_SEVERITY_FACTOR_FIELDS = {
    "UNILATERAL_CONTROL": "unilateral_control",
    "NO_EFFECTIVE_REMEDY": "no_effective_remedy",
    "BROAD_SCOPE": "broad_scope",
    "FINANCIAL_IMPACT": "financial_impact",
    "SCHEDULE_IMPACT": "schedule_impact",
    "OPERATIONAL_IMPACT": "operational_impact",
    "MISSING_CORE_MECHANISM": "missing_core_mechanism",
    "OWNERSHIP_AMBIGUITY": "ownership_ambiguity",
    "OVERBROAD_TRANSFER": "overbroad_transfer",
    "EXCLUSIVE_OR_IRREVOCABLE": "exclusive_or_irrevocable",
    "UNLIMITED_SCOPE": "unlimited_scope",
    "POST_TERMINATION_EFFECT": "post_termination_effect",
    "ONE_SIDED_PROTECTION": "one_sided_protection",
    "THIRD_PARTY_EXPOSURE": "third_party_exposure",
    "NO_RETURN_OR_DELETION": "no_return_or_deletion",
    "NO_SECURITY_STANDARD": "no_security_standard",
    "MISSING_INCIDENT_NOTICE": "missing_incident_notice",
    "UNLIMITED_LIABILITY": "unlimited_liability",
    "ONE_SIDED_LIABILITY": "one_sided_liability",
    "OVERBROAD_INDEMNITY": "overbroad_indemnity",
    "INDIRECT_LOSS_EXPOSURE": "indirect_loss_exposure",
    "LIABILITY_CAP_BYPASSED": "liability_cap_bypassed",
    "CUMULATIVE_REMEDIES": "cumulative_remedies",
    "UNILATERAL_TERMINATION": "unilateral_termination",
    "NO_CURE_PERIOD": "no_cure_period",
    "NO_TERMINATION_SETTLEMENT": "no_termination_settlement",
    "POST_TERMINATION_EXPOSURE": "post_termination_exposure",
    "AUTOMATIC_RENEWAL": "automatic_renewal",
    "RESTRICTED_EXIT_WINDOW": "restricted_exit_window",
    "DISPUTE_CLAUSE_CONFLICT": "dispute_clause_conflict",
    "FOREIGN_OR_BURDENSOME_FORUM": "foreign_or_burdensome_forum",
}

_PO_SEVERITY_FACTOR_POLICIES: dict[str, SeverityFactorPolicy] = {
    "UNILATERAL_CONTROL": SeverityFactorPolicy(
        factor_code="UNILATERAL_CONTROL",
        allowed_check_codes=["PO-001", "PO-002", "PO-006"],
        allowed_candidate_types=[
            "SCOPE_EXPANSION",
            "RIGHTS_OBLIGATIONS_IMBALANCE",
            "CHANGE_CONTROL_REVIEW",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["单方、自行、随时、有权、必须按相对方要求"],
    ),
    "NO_EFFECTIVE_REMEDY": SeverityFactorPolicy(
        factor_code="NO_EFFECTIVE_REMEDY",
        allowed_check_codes=[
            "PO-001",
            "PO-002",
            "PO-003",
            "PO-004",
            "PO-005",
            "PO-006",
            "PO-007",
        ],
        allowed_candidate_types=[
            "PROJECTED_IR_REVIEW",
            "MISSING_EXPECTED_IR",
            "SCOPE_EXPANSION",
            "DELIVERY_SCHEDULE_REVIEW",
            "RIGHTS_OBLIGATIONS_IMBALANCE",
            "COOPERATION_DEPENDENCY",
            "COOPERATION_OBLIGATION_ABSENT",
            "QUALITY_STANDARD_UNMEASURABLE",
            "ACCEPTANCE_MECHANISM_REVIEW",
            "ACCEPTANCE_MECHANISM_ABSENT",
            "ASSIGNMENT_SUBCONTRACT_REVIEW",
            "CHANGE_CONTROL_REVIEW",
            "CHANGE_CONTROL_ABSENT",
            "WARRANTY_SUPPORT_REVIEW",
            "WARRANTY_SUPPORT_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["明示排除或限制异议、整改、补救、顺延、解除、复核或协商"],
        required_absence_source_types=["REMEDY_PROCEDURE_ABSENT"],
    ),
    "BROAD_SCOPE": SeverityFactorPolicy(
        factor_code="BROAD_SCOPE",
        allowed_check_codes=["PO-001", "PO-002", "PO-005", "PO-006"],
        allowed_candidate_types=[
            "SCOPE_EXPANSION",
            "RIGHTS_OBLIGATIONS_IMBALANCE",
            "ASSIGNMENT_SUBCONTRACT_REVIEW",
            "CHANGE_CONTROL_REVIEW",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["包括但不限于、任何、全部、其他要求、随时扩大或调整"],
    ),
    "FINANCIAL_IMPACT": SeverityFactorPolicy(
        factor_code="FINANCIAL_IMPACT",
        allowed_check_codes=["PO-001", "PO-002", "PO-004", "PO-005", "PO-006", "PO-007"],
        allowed_candidate_types=[
            "SCOPE_EXPANSION",
            "DELIVERY_SCHEDULE_REVIEW",
            "RIGHTS_OBLIGATIONS_IMBALANCE",
            "QUALITY_STANDARD_UNMEASURABLE",
            "ACCEPTANCE_MECHANISM_REVIEW",
            "ACCEPTANCE_MECHANISM_ABSENT",
            "ASSIGNMENT_SUBCONTRACT_REVIEW",
            "CHANGE_CONTROL_REVIEW",
            "CHANGE_CONTROL_ABSENT",
            "WARRANTY_SUPPORT_REVIEW",
            "WARRANTY_SUPPORT_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["明确金钱后果及其与当前风险的因果联系"],
    ),
    "SCHEDULE_IMPACT": SeverityFactorPolicy(
        factor_code="SCHEDULE_IMPACT",
        allowed_check_codes=["PO-001", "PO-002", "PO-003", "PO-004", "PO-006", "PO-007"],
        allowed_candidate_types=[
            "SCOPE_EXPANSION",
            "DELIVERY_SCHEDULE_REVIEW",
            "RIGHTS_OBLIGATIONS_IMBALANCE",
            "COOPERATION_DEPENDENCY",
            "COOPERATION_OBLIGATION_ABSENT",
            "ACCEPTANCE_MECHANISM_REVIEW",
            "ACCEPTANCE_MECHANISM_ABSENT",
            "CHANGE_CONTROL_REVIEW",
            "CHANGE_CONTROL_ABSENT",
            "WARRANTY_SUPPORT_REVIEW",
            "WARRANTY_SUPPORT_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["明确期限或进度后果及其与当前风险的因果联系"],
        conflicting_or_disqualifying_signals=["仅出现完成、交付、履行或提供"],
    ),
    "OPERATIONAL_IMPACT": SeverityFactorPolicy(
        factor_code="OPERATIONAL_IMPACT",
        allowed_check_codes=[
            "PO-001",
            "PO-002",
            "PO-003",
            "PO-004",
            "PO-005",
            "PO-006",
            "PO-007",
        ],
        allowed_candidate_types=[
            "PROJECTED_IR_REVIEW",
            "MISSING_EXPECTED_IR",
            "SCOPE_EXPANSION",
            "DELIVERY_SCHEDULE_REVIEW",
            "RIGHTS_OBLIGATIONS_IMBALANCE",
            "COOPERATION_DEPENDENCY",
            "COOPERATION_OBLIGATION_ABSENT",
            "QUALITY_STANDARD_UNMEASURABLE",
            "ACCEPTANCE_MECHANISM_REVIEW",
            "ACCEPTANCE_MECHANISM_ABSENT",
            "ASSIGNMENT_SUBCONTRACT_REVIEW",
            "CHANGE_CONTROL_REVIEW",
            "CHANGE_CONTROL_ABSENT",
            "WARRANTY_SUPPORT_REVIEW",
            "WARRANTY_SUPPORT_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["明确无法履行、中断、返工、拒收或其他实际履行后果"],
    ),
    "MISSING_CORE_MECHANISM": SeverityFactorPolicy(
        factor_code="MISSING_CORE_MECHANISM",
        allowed_check_codes=["PO-003", "PO-004", "PO-006", "PO-007"],
        allowed_candidate_types=[
            "COOPERATION_OBLIGATION_ABSENT",
            "QUALITY_STANDARD_UNMEASURABLE",
            "ACCEPTANCE_MECHANISM_REVIEW",
            "ACCEPTANCE_MECHANISM_ABSENT",
            "CHANGE_CONTROL_REVIEW",
            "CHANGE_CONTROL_ABSENT",
            "WARRANTY_SUPPORT_REVIEW",
            "WARRANTY_SUPPORT_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["明示缺少或不适用核心机制"],
        required_absence_source_types=["CORE_MECHANISM_ABSENT"],
    ),
}


_ICD_ALL_CHECK_CODES = tuple(f"ICD-{index:03d}" for index in range(1, 7))
_ICD_ALL_CANDIDATE_TYPES = (
    "FOREGROUND_IP_OWNERSHIP_REVIEW",
    "FOREGROUND_IP_OWNERSHIP_ABSENT",
    "BACKGROUND_IP_LICENSE_REVIEW",
    "BACKGROUND_IP_LICENSE_ABSENT",
    "THIRD_PARTY_IP_PROTECTION_REVIEW",
    "THIRD_PARTY_IP_PROTECTION_ABSENT",
    "CONFIDENTIALITY_PROTECTION_REVIEW",
    "CONFIDENTIALITY_COMPLETENESS_ABSENT",
    "DATA_PROCESSING_SECURITY_REVIEW",
    "DATA_PROCESSING_SECURITY_ABSENT",
    "DATA_RETURN_DELETION_REVIEW",
    "DATA_RETURN_DELETION_ABSENT",
)
_ICD_SEVERITY_FACTOR_POLICIES: dict[str, SeverityFactorPolicy] = {
    "OWNERSHIP_AMBIGUITY": SeverityFactorPolicy(
        factor_code="OWNERSHIP_AMBIGUITY",
        allowed_check_codes=["ICD-001", "ICD-002", "ICD-006"],
        allowed_candidate_types=[
            "FOREGROUND_IP_OWNERSHIP_REVIEW",
            "FOREGROUND_IP_OWNERSHIP_ABSENT",
            "BACKGROUND_IP_LICENSE_REVIEW",
            "BACKGROUND_IP_LICENSE_ABSENT",
            "DATA_RETURN_DELETION_REVIEW",
            "DATA_RETURN_DELETION_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["归属、所有、权属、知识产权或合法缺失机制"],
    ),
    "OVERBROAD_TRANSFER": SeverityFactorPolicy(
        factor_code="OVERBROAD_TRANSFER",
        allowed_check_codes=["ICD-001", "ICD-002"],
        allowed_candidate_types=[
            "FOREGROUND_IP_OWNERSHIP_REVIEW",
            "BACKGROUND_IP_LICENSE_REVIEW",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["全部转让、无偿转让、概括转让或超出项目成果范围"],
    ),
    "EXCLUSIVE_OR_IRREVOCABLE": SeverityFactorPolicy(
        factor_code="EXCLUSIVE_OR_IRREVOCABLE",
        allowed_check_codes=["ICD-002"],
        allowed_candidate_types=["BACKGROUND_IP_LICENSE_REVIEW"],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["独占、排他、不可撤销或永久许可"],
    ),
    "UNLIMITED_SCOPE": SeverityFactorPolicy(
        factor_code="UNLIMITED_SCOPE",
        allowed_check_codes=["ICD-001", "ICD-002", "ICD-004", "ICD-005"],
        allowed_candidate_types=[
            "FOREGROUND_IP_OWNERSHIP_REVIEW",
            "BACKGROUND_IP_LICENSE_REVIEW",
            "CONFIDENTIALITY_PROTECTION_REVIEW",
            "DATA_PROCESSING_SECURITY_REVIEW",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["不限用途、期限、地域、对象或范围"],
    ),
    "POST_TERMINATION_EFFECT": SeverityFactorPolicy(
        factor_code="POST_TERMINATION_EFFECT",
        allowed_check_codes=["ICD-002", "ICD-004", "ICD-006"],
        allowed_candidate_types=[
            "BACKGROUND_IP_LICENSE_REVIEW",
            "CONFIDENTIALITY_PROTECTION_REVIEW",
            "CONFIDENTIALITY_COMPLETENESS_ABSENT",
            "DATA_RETURN_DELETION_REVIEW",
            "DATA_RETURN_DELETION_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["终止后、永久、持续有效、返还、删除或留存"],
    ),
    "ONE_SIDED_PROTECTION": SeverityFactorPolicy(
        factor_code="ONE_SIDED_PROTECTION",
        allowed_check_codes=["ICD-004", "ICD-005"],
        allowed_candidate_types=[
            "CONFIDENTIALITY_PROTECTION_REVIEW",
            "DATA_PROCESSING_SECURITY_REVIEW",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["只约束our_party而未对相对方提供对等保护"],
    ),
    "THIRD_PARTY_EXPOSURE": SeverityFactorPolicy(
        factor_code="THIRD_PARTY_EXPOSURE",
        allowed_check_codes=["ICD-003"],
        allowed_candidate_types=[
            "THIRD_PARTY_IP_PROTECTION_REVIEW",
            "THIRD_PARTY_IP_PROTECTION_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["第三方权利、侵权、索赔或合法缺失机制"],
    ),
    "NO_RETURN_OR_DELETION": SeverityFactorPolicy(
        factor_code="NO_RETURN_OR_DELETION",
        allowed_check_codes=["ICD-006"],
        allowed_candidate_types=[
            "DATA_RETURN_DELETION_REVIEW",
            "DATA_RETURN_DELETION_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["返还、删除、销毁、留存或合法缺失机制"],
    ),
    "NO_SECURITY_STANDARD": SeverityFactorPolicy(
        factor_code="NO_SECURITY_STANDARD",
        allowed_check_codes=["ICD-005"],
        allowed_candidate_types=[
            "DATA_PROCESSING_SECURITY_REVIEW",
            "DATA_PROCESSING_SECURITY_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["安全标准、访问控制、保护措施或合法缺失机制"],
    ),
    "MISSING_INCIDENT_NOTICE": SeverityFactorPolicy(
        factor_code="MISSING_INCIDENT_NOTICE",
        allowed_check_codes=["ICD-005"],
        allowed_candidate_types=[
            "DATA_PROCESSING_SECURITY_REVIEW",
            "DATA_PROCESSING_SECURITY_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["安全事件通知、泄露通知、响应时限或合法缺失机制"],
    ),
    "OPERATIONAL_IMPACT": SeverityFactorPolicy(
        factor_code="OPERATIONAL_IMPACT",
        allowed_check_codes=list(_ICD_ALL_CHECK_CODES),
        allowed_candidate_types=list(_ICD_ALL_CANDIDATE_TYPES),
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["停用、返工、中断、无法继续使用或实际履行影响"],
    ),
    "FINANCIAL_IMPACT": SeverityFactorPolicy(
        factor_code="FINANCIAL_IMPACT",
        allowed_check_codes=list(_ICD_ALL_CHECK_CODES),
        allowed_candidate_types=list(_ICD_ALL_CANDIDATE_TYPES),
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["赔偿、费用、价款、罚款、成本或明确金钱后果"],
    ),
    "SCHEDULE_IMPACT": SeverityFactorPolicy(
        factor_code="SCHEDULE_IMPACT",
        allowed_check_codes=list(_ICD_ALL_CHECK_CODES),
        allowed_candidate_types=list(_ICD_ALL_CANDIDATE_TYPES),
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["延期、延迟、逾期、工期、期限或明确进度后果"],
    ),
    "NO_EFFECTIVE_REMEDY": SeverityFactorPolicy(
        factor_code="NO_EFFECTIVE_REMEDY",
        allowed_check_codes=list(_ICD_ALL_CHECK_CODES),
        allowed_candidate_types=list(_ICD_ALL_CANDIDATE_TYPES),
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["明示排除救济或合法救济缺失机制"],
    ),
    "MISSING_CORE_MECHANISM": SeverityFactorPolicy(
        factor_code="MISSING_CORE_MECHANISM",
        allowed_check_codes=list(_ICD_ALL_CHECK_CODES),
        allowed_candidate_types=[
            candidate_type
            for candidate_type in _ICD_ALL_CANDIDATE_TYPES
            if candidate_type.endswith("_ABSENT")
        ],
        required_evidence_types=["ABSENCE"],
        required_text_signals=["合法ICD Absence Source"],
    ),
}

_LRE_ALL_CHECK_CODES = tuple(f"LRE-{index:03d}" for index in range(1, 9))
_LRE_ALL_CANDIDATE_TYPES = (
    "BROAD_BREACH_TRIGGER_REVIEW",
    "OVERBROAD_LOSS_SCOPE_REVIEW",
    "CUMULATIVE_REMEDIES_REVIEW",
    "LIABILITY_CAP_ABSENT",
    "LIABILITY_CAP_BYPASS_REVIEW",
    "OVERBROAD_INDEMNITY_REVIEW",
    "TERMINATION_RIGHTS_REVIEW",
    "TERMINATION_SETTLEMENT_REVIEW",
    "TERMINATION_SETTLEMENT_ABSENT",
    "FORCE_MAJEURE_MECHANISM_ABSENT",
    "DISPUTE_RESOLUTION_ABSENT",
)
_LRE_SEVERITY_FACTOR_POLICIES: dict[str, SeverityFactorPolicy] = {
    "UNLIMITED_LIABILITY": SeverityFactorPolicy(
        factor_code="UNLIMITED_LIABILITY",
        allowed_check_codes=["LRE-002", "LRE-003", "LRE-004"],
        allowed_candidate_types=[
            "OVERBROAD_LOSS_SCOPE_REVIEW",
            "LIABILITY_CAP_ABSENT",
            "LIABILITY_CAP_BYPASS_REVIEW",
            "OVERBROAD_INDEMNITY_REVIEW",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["全部、所有、不限金额、无上限或开放损失且合法责任上限缺失"],
    ),
    "ONE_SIDED_LIABILITY": SeverityFactorPolicy(
        factor_code="ONE_SIDED_LIABILITY",
        allowed_check_codes=["LRE-001", "LRE-003", "LRE-004"],
        allowed_candidate_types=[
            "BROAD_BREACH_TRIGGER_REVIEW",
            "LIABILITY_CAP_BYPASS_REVIEW",
            "OVERBROAD_INDEMNITY_REVIEW",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["仅一方承担责任或仅一方获得责任保护"],
    ),
    "OVERBROAD_INDEMNITY": SeverityFactorPolicy(
        factor_code="OVERBROAD_INDEMNITY",
        allowed_check_codes=["LRE-002", "LRE-004"],
        allowed_candidate_types=[
            "OVERBROAD_LOSS_SCOPE_REVIEW",
            "OVERBROAD_INDEMNITY_REVIEW",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["全部、包括但不限于、第三方索赔或开放费用范围"],
    ),
    "INDIRECT_LOSS_EXPOSURE": SeverityFactorPolicy(
        factor_code="INDIRECT_LOSS_EXPOSURE",
        allowed_check_codes=["LRE-002", "LRE-003", "LRE-004"],
        allowed_candidate_types=[
            "OVERBROAD_LOSS_SCOPE_REVIEW",
            "LIABILITY_CAP_ABSENT",
            "OVERBROAD_INDEMNITY_REVIEW",
        ],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["间接损失、利润、商誉、机会或可预期利益损失"],
    ),
    "LIABILITY_CAP_BYPASSED": SeverityFactorPolicy(
        factor_code="LIABILITY_CAP_BYPASSED",
        allowed_check_codes=["LRE-003"],
        allowed_candidate_types=["LIABILITY_CAP_BYPASS_REVIEW"],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["责任上限条款与不受限制的赔偿或责任条款同时存在"],
    ),
    "CUMULATIVE_REMEDIES": SeverityFactorPolicy(
        factor_code="CUMULATIVE_REMEDIES",
        allowed_check_codes=["LRE-002"],
        allowed_candidate_types=["CUMULATIVE_REMEDIES_REVIEW"],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["违约金、赔偿、退款或其他责任可以同时或累计主张"],
    ),
    "UNILATERAL_TERMINATION": SeverityFactorPolicy(
        factor_code="UNILATERAL_TERMINATION",
        allowed_check_codes=["LRE-005"],
        allowed_candidate_types=["TERMINATION_RIGHTS_REVIEW"],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["仅一方拥有解除权或一方解除条件明显更宽"],
    ),
    "NO_CURE_PERIOD": SeverityFactorPolicy(
        factor_code="NO_CURE_PERIOD",
        allowed_check_codes=["LRE-005"],
        allowed_candidate_types=["TERMINATION_RIGHTS_REVIEW"],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["违约解除且缺少整改、补救或通知期限"],
    ),
    "NO_TERMINATION_SETTLEMENT": SeverityFactorPolicy(
        factor_code="NO_TERMINATION_SETTLEMENT",
        allowed_check_codes=["LRE-006"],
        allowed_candidate_types=[
            "TERMINATION_SETTLEMENT_REVIEW",
            "TERMINATION_SETTLEMENT_ABSENT",
        ],
        required_evidence_types=["ABSENCE"],
        required_text_signals=["合法终止结算缺失机制"],
    ),
    "POST_TERMINATION_EXPOSURE": SeverityFactorPolicy(
        factor_code="POST_TERMINATION_EXPOSURE",
        allowed_check_codes=["LRE-006"],
        allowed_candidate_types=[
            "TERMINATION_SETTLEMENT_REVIEW",
            "TERMINATION_SETTLEMENT_ABSENT",
        ],
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["终止后继续使用、责任持续、返还或迁移边界不清"],
    ),
    "AUTOMATIC_RENEWAL": SeverityFactorPolicy(
        factor_code="AUTOMATIC_RENEWAL",
        allowed_check_codes=["LRE-005"],
        allowed_candidate_types=["TERMINATION_RIGHTS_REVIEW"],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["自动续期、默认续期或不通知即续期"],
    ),
    "RESTRICTED_EXIT_WINDOW": SeverityFactorPolicy(
        factor_code="RESTRICTED_EXIT_WINDOW",
        allowed_check_codes=["LRE-005"],
        allowed_candidate_types=["TERMINATION_RIGHTS_REVIEW"],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["明确且显著压缩合理退出机会的通知期限"],
    ),
    "DISPUTE_CLAUSE_CONFLICT": SeverityFactorPolicy(
        factor_code="DISPUTE_CLAUSE_CONFLICT",
        allowed_check_codes=["LRE-008"],
        allowed_candidate_types=["DISPUTE_RESOLUTION_ABSENT"],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["两个以上法院、仲裁或管辖安排发生实质冲突"],
    ),
    "FOREIGN_OR_BURDENSOME_FORUM": SeverityFactorPolicy(
        factor_code="FOREIGN_OR_BURDENSOME_FORUM",
        allowed_check_codes=["LRE-008"],
        allowed_candidate_types=["DISPUTE_RESOLUTION_ABSENT"],
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["明确异地法院或仲裁地且对我方维权形成实质负担"],
    ),
    "FINANCIAL_IMPACT": SeverityFactorPolicy(
        factor_code="FINANCIAL_IMPACT",
        allowed_check_codes=["LRE-001", "LRE-002", "LRE-003", "LRE-004", "LRE-006"],
        allowed_candidate_types=list(_LRE_ALL_CANDIDATE_TYPES),
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["与当前责任、赔偿或退出风险直接关联的明确金钱后果"],
    ),
    "SCHEDULE_IMPACT": SeverityFactorPolicy(
        factor_code="SCHEDULE_IMPACT",
        allowed_check_codes=["LRE-005", "LRE-006", "LRE-007"],
        allowed_candidate_types=list(_LRE_ALL_CANDIDATE_TYPES),
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["与当前退出或不可抗力风险直接相关的期限、延期或逾期后果"],
    ),
    "OPERATIONAL_IMPACT": SeverityFactorPolicy(
        factor_code="OPERATIONAL_IMPACT",
        allowed_check_codes=list(_LRE_ALL_CHECK_CODES),
        allowed_candidate_types=list(_LRE_ALL_CANDIDATE_TYPES),
        required_evidence_types=["TEXT_QUOTE"],
        required_text_signals=["与当前责任或退出风险直接相关的履行中断、返工或迁移后果"],
    ),
    "NO_EFFECTIVE_REMEDY": SeverityFactorPolicy(
        factor_code="NO_EFFECTIVE_REMEDY",
        allowed_check_codes=list(_LRE_ALL_CHECK_CODES),
        allowed_candidate_types=list(_LRE_ALL_CANDIDATE_TYPES),
        required_evidence_types=["TEXT_QUOTE", "ABSENCE"],
        required_text_signals=["明示排除救济或合法救济缺失机制"],
    ),
    "MISSING_CORE_MECHANISM": SeverityFactorPolicy(
        factor_code="MISSING_CORE_MECHANISM",
        allowed_check_codes=["LRE-003", "LRE-006", "LRE-007", "LRE-008"],
        allowed_candidate_types=[
            "LIABILITY_CAP_ABSENT",
            "TERMINATION_SETTLEMENT_ABSENT",
            "FORCE_MAJEURE_MECHANISM_ABSENT",
            "DISPUTE_RESOLUTION_ABSENT",
        ],
        required_evidence_types=["ABSENCE"],
        required_text_signals=["合法LRE Absence Source"],
    ),
}


def _candidate_severity_policies(
    unit_id: str,
) -> dict[str, SeverityFactorPolicy]:
    if unit_id == "performance_obligations":
        return _PO_SEVERITY_FACTOR_POLICIES
    if unit_id == "ip_confidentiality_data":
        return _ICD_SEVERITY_FACTOR_POLICIES
    if unit_id == "liability_remedies_exit":
        return _LRE_SEVERITY_FACTOR_POLICIES
    return {}


def _po_allowed_severity_factors(
    check_code: str,
    candidate_type: str,
) -> list[SeverityFactorCode]:
    return [
        factor_code
        for factor_code, policy in _PO_SEVERITY_FACTOR_POLICIES.items()
        if check_code in policy.allowed_check_codes
        and candidate_type in policy.allowed_candidate_types
    ]


def _icd_allowed_severity_factors(
    check_code: str,
    candidate_type: str,
) -> list[SeverityFactorCode]:
    return [
        factor_code
        for factor_code, policy in _ICD_SEVERITY_FACTOR_POLICIES.items()
        if check_code in policy.allowed_check_codes
        and candidate_type in policy.allowed_candidate_types
    ]


def _lre_allowed_severity_factors(
    check_code: str,
    candidate_type: str,
) -> list[SeverityFactorCode]:
    return [
        factor_code
        for factor_code, policy in _LRE_SEVERITY_FACTOR_POLICIES.items()
        if check_code in policy.allowed_check_codes
        and candidate_type in policy.allowed_candidate_types
    ]


def _po_severity_source_text(source: RiskEvidenceSource) -> str:
    return " ".join(
        filter(
            None,
            (
                source.subject,
                source.predicate,
                source.object,
                source.quoted_text,
            ),
        )
    )


def _po_valid_remedy_absence_source(source: RiskAbsenceEvidenceSource) -> bool:
    value = " ".join(
        (
            source.checked_scope,
            source.verification_method,
            source.missing_target,
        )
    )
    return bool(
        re.search(r"(异议|申诉|整改|补救|顺延|拒绝|解除|复核|协商|救济)", value)
        and re.search(r"(缺少|缺失|未约定|未定位|不存在)", value)
    )


def _po_factor_has_required_evidence(
    factor_code: str,
    *,
    text_sources: list[RiskEvidenceSource],
    absence_sources: list[RiskAbsenceEvidenceSource],
) -> tuple[bool, str]:
    texts = [_po_severity_source_text(source) for source in text_sources]
    if factor_code == "UNILATERAL_CONTROL":
        return (
            any(
                re.search(
                    r"(单方|自行|随时|最终解释|有权.{0,16}(决定|拒绝|暂停|要求|检查)|"
                    r"(必须|须|应).{0,12}按.{0,12}要求)",
                    text,
                )
                for text in texts
            ),
            "REQUIRED_TEXT_SIGNAL_MISSING",
        )
    if factor_code == "BROAD_SCOPE":
        return (
            any(
                re.search(
                    r"(包括但不限于|任何|全部|其他要求|其[他它]要求|"
                    r"随时.{0,12}(增加|扩大|调整)|范围.{0,12}(扩大|不受限))",
                    text,
                )
                for text in texts
            ),
            "REQUIRED_TEXT_SIGNAL_MISSING",
        )
    if factor_code == "FINANCIAL_IMPACT":
        money = re.compile(
            r"(费用|价款|扣款|罚款|违约金|赔偿|成本|额外支出|"
            r"付款.{0,8}(减少|拒绝|不予|扣除))"
        )
        causal = re.compile(
            r"(导致|造成|致使|因此|从而|增加|承担|支付|扣减|拒付)"
        )
        return (
            any(money.search(text) and causal.search(text) for text in texts),
            "DIRECT_CAUSAL_EVIDENCE_MISSING",
        )
    if factor_code == "SCHEDULE_IMPACT":
        schedule = re.compile(
            r"(期限|时间节点|工期|延迟|延期|延误|逾期|顺延|按期|"
            r"工作日|自然日|日内|日前)"
        )
        causal = re.compile(
            r"(导致|造成|致使|因此|从而|不予顺延|不得顺延|仍承担|"
            r"缩短|延后|延期|延误|逾期)"
        )
        return (
            any(schedule.search(text) and causal.search(text) for text in texts),
            "DIRECT_CAUSAL_EVIDENCE_MISSING",
        )
    if factor_code == "NO_EFFECTIVE_REMEDY":
        explicit_denial = re.compile(
            r"(无权|不得|不予|放弃|排除|限制|取消).{0,18}"
            r"(异议|申诉|整改|补救|顺延|拒绝|解除|复核|协商|救济)"
        )
        if any(explicit_denial.search(text) for text in texts):
            return True, "VALID_ABSENCE_SOURCE_MISSING"
        return (
            any(_po_valid_remedy_absence_source(source) for source in absence_sources),
            "VALID_ABSENCE_SOURCE_MISSING",
        )
    if factor_code == "OPERATIONAL_IMPACT":
        consequence = re.compile(
            r"(无法履行|不能履行|履行中断|停工|返工|拒收|无法交付|"
            r"不能交付|责任.{0,8}(承担|加重)|验收.{0,8}(拒绝|不通过))"
        )
        causal = re.compile(r"(导致|造成|致使|因此|从而|使)")
        return (
            any(consequence.search(text) and causal.search(text) for text in texts),
            "DIRECT_CAUSAL_EVIDENCE_MISSING",
        )
    if factor_code == "MISSING_CORE_MECHANISM":
        if absence_sources:
            return True, "VALID_ABSENCE_SOURCE_MISSING"
        return (
            any(
                re.search(
                    r"(未约定|未明确|缺少|缺失|没有).{0,18}"
                    r"(机制|程序|标准|期限|配合|变更|验收|质保|整改)",
                    text,
                )
                for text in texts
            ),
            "REQUIRED_TEXT_SIGNAL_MISSING",
        )
    raise DirectReviewError(
        "SEVERITY_FACTOR_POLICY_UNKNOWN",
        f"Unknown PO Severity Factor: {factor_code}",
    )


def _icd_factor_has_required_evidence(
    factor_code: str,
    *,
    text_sources: list[RiskEvidenceSource],
    absence_sources: list[RiskAbsenceEvidenceSource],
) -> tuple[bool, str]:
    texts = [_po_severity_source_text(source) for source in text_sources]
    absence_text = " ".join(
        " ".join(
            (
                source.checked_scope,
                source.verification_method,
                source.missing_target,
            )
        )
        for source in absence_sources
    )
    patterns = {
        "OWNERSHIP_AMBIGUITY": r"(知识产权|著作权|所有权|权属|归属)",
        "OVERBROAD_TRANSFER": r"(全部|无偿|概括).{0,10}(转让|归属)|转让.{0,10}(全部|任何)",
        "EXCLUSIVE_OR_IRREVOCABLE": r"(独占|排他|不可撤销|永久)",
        "UNLIMITED_SCOPE": r"(不限|任何|全部|永久|全球).{0,12}(用途|期限|地域|范围|对象|使用)",
        "POST_TERMINATION_EFFECT": r"(终止后|解除后|永久|持续有效|返还|删除|销毁|留存)",
        "ONE_SIDED_PROTECTION": r"(仅|只).{0,8}(一方|甲方|乙方|我方|相对方).{0,12}(保密|保护|承担)",
        "THIRD_PARTY_EXPOSURE": r"(第三方|侵权|索赔|权利瑕疵|不侵权)",
        "NO_RETURN_OR_DELETION": r"(返还|退还|删除|销毁|留存|备份)",
        "NO_SECURITY_STANDARD": r"(安全标准|安全措施|访问控制|最小权限|加密|保护措施)",
        "MISSING_INCIDENT_NOTICE": r"(安全事件|数据泄露|事件通知|泄露通知|通知时限)",
    }
    if factor_code in patterns:
        if any(re.search(patterns[factor_code], text) for text in texts):
            return True, "REQUIRED_TEXT_SIGNAL_MISSING"
        if absence_sources and re.search(patterns[factor_code], absence_text):
            return True, "VALID_ABSENCE_SOURCE_MISSING"
        return False, (
            "VALID_ABSENCE_SOURCE_MISSING"
            if absence_sources
            else "REQUIRED_TEXT_SIGNAL_MISSING"
        )
    if factor_code == "MISSING_CORE_MECHANISM":
        return bool(absence_sources), "VALID_ABSENCE_SOURCE_MISSING"
    return _po_factor_has_required_evidence(
        factor_code,
        text_sources=text_sources,
        absence_sources=absence_sources,
    )


def _lre_factor_has_required_evidence(
    factor_code: str,
    *,
    text_sources: list[RiskEvidenceSource],
    absence_sources: list[RiskAbsenceEvidenceSource],
) -> tuple[bool, str]:
    texts = [_po_severity_source_text(source) for source in text_sources]
    absence_text = " ".join(
        " ".join(
            (
                source.checked_scope,
                source.verification_method,
                source.missing_target,
            )
        )
        for source in absence_sources
    )

    def has(pattern: str) -> bool:
        return any(re.search(pattern, text) for text in texts)

    if factor_code == "UNLIMITED_LIABILITY":
        open_scope = has(r"(全部|所有|不限金额|无上限|包括但不限于)")
        cap_absent = bool(
            absence_sources
            and re.search(r"(责任.{0,8}(上限|限额)|上限|限额)", absence_text)
        )
        return open_scope and cap_absent, "VALID_ABSENCE_SOURCE_MISSING"
    if factor_code == "ONE_SIDED_LIABILITY":
        return has(r"(仅|只).{0,8}(甲方|乙方|一方).{0,18}(承担|赔偿|责任|免责)"), "REQUIRED_TEXT_SIGNAL_MISSING"
    if factor_code == "OVERBROAD_INDEMNITY":
        return has(
            r"(全部赔偿|包括但不限于|所有损失|任何法律责任|第三方.{0,12}(赔偿|索赔)|"
            r"(律师费|调查费|公证费|诉讼费|仲裁费))"
        ), "REQUIRED_TEXT_SIGNAL_MISSING"
    if factor_code == "INDIRECT_LOSS_EXPOSURE":
        return has(r"(间接损失|利润损失|商誉损失|机会损失|可预期利益)"), "REQUIRED_TEXT_SIGNAL_MISSING"
    if factor_code == "LIABILITY_CAP_BYPASSED":
        cap = has(r"(责任|赔偿|违约金).{0,18}(上限|限额|最高|不超过)")
        bypass = has(r"(全部赔偿|所有损失|不受.{0,10}(上限|限制)|同时.{0,8}主张)")
        return cap and bypass, "DIRECT_CAUSAL_EVIDENCE_MISSING"
    if factor_code == "CUMULATIVE_REMEDIES":
        return has(
            r"(同时.{0,12}(主张|承担|赔偿)|违约金.{0,20}(并|且|同时).{0,16}赔偿|"
            r"(扣分|退款).{0,20}(违约金|赔偿))"
        ), "REQUIRED_TEXT_SIGNAL_MISSING"
    if factor_code == "UNILATERAL_TERMINATION":
        roles = contract_party_roles(
            perspective=request.perspective,
            our_party=request.our_party,
            counterparty=request.counterparty,
        )
        our_role = roles.our_role
        counterparty_role = roles.counterparty_role
        subjects = " ".join(source.subject or "" for source in text_sources)
        # A one-party literal is only a signal. The Candidate still supplies
        # both parties' complete termination source pool for model comparison.
        return (
            (our_role in subjects) ^ (counterparty_role in subjects),
            "REQUIRED_TEXT_SIGNAL_MISSING",
        )
    if factor_code == "NO_CURE_PERIOD":
        termination = has(r"(解除|终止)")
        cure = has(r"(整改|改正|补救|通知).{0,12}(日|期限)|([0-9一二三四五六七八九十]+个?工作日).{0,10}(整改|改正)")
        valid_absence = bool(
            absence_sources and re.search(r"(整改|补救|通知).{0,8}(缺失|未约定|未定位)", absence_text)
        )
        return termination and not cure and valid_absence, "VALID_ABSENCE_SOURCE_MISSING"
    if factor_code == "NO_TERMINATION_SETTLEMENT":
        return bool(
            absence_sources and re.search(r"(终止|解除).{0,18}(结算|费用|已完成工作)", absence_text)
        ), "VALID_ABSENCE_SOURCE_MISSING"
    if factor_code == "POST_TERMINATION_EXPOSURE":
        explicit = has(r"(解除|终止)后.{0,24}(继续|使用|承担|有效|不影响)")
        absence = bool(
            absence_sources and re.search(r"(终止|解除).{0,20}(返还|结算|持续义务|退出)", absence_text)
        )
        return explicit or absence, "VALID_ABSENCE_SOURCE_MISSING"
    if factor_code == "AUTOMATIC_RENEWAL":
        return has(r"(自动|默认).{0,8}续期|不通知.{0,8}续期"), "REQUIRED_TEXT_SIGNAL_MISSING"
    if factor_code == "RESTRICTED_EXIT_WINDOW":
        return has(r"(不续期|退出|终止).{0,18}(提前|通知).{0,8}([0-9一二三四五六七八九十]+).{0,4}(日|月)"), "DIRECT_CAUSAL_EVIDENCE_MISSING"
    if factor_code == "DISPUTE_CLAUSE_CONFLICT":
        forums = sum(
            bool(re.search(r"(人民法院|法院|仲裁委员会|仲裁机构|提交仲裁)", text))
            for text in texts
        )
        return forums >= 2 and has(r"(法院|诉讼)") and has(r"仲裁"), "DIRECT_CAUSAL_EVIDENCE_MISSING"
    if factor_code == "FOREIGN_OR_BURDENSOME_FORUM":
        return has(r"(异地|境外|外国|外地).{0,12}(法院|仲裁|管辖)"), "DIRECT_CAUSAL_EVIDENCE_MISSING"
    if factor_code == "MISSING_CORE_MECHANISM":
        return bool(absence_sources), "VALID_ABSENCE_SOURCE_MISSING"
    return _po_factor_has_required_evidence(
        factor_code,
        text_sources=text_sources,
        absence_sources=absence_sources,
    )


def _validate_po_semantic_severity_factors(
    candidate: DeterministicRiskCandidate,
    proposed: list[SeverityFactorCode],
    *,
    supporting_ids: list[str],
    catalog: PoEvidenceCatalog,
) -> tuple[list[SeverityFactorCode], list[RejectedSeverityFactor]]:
    source_ids = list(
        dict.fromkeys(
            [
                *candidate.primary_evidence_source_ids,
                *supporting_ids,
            ]
        )
    )
    text_sources = [
        catalog.evidence_sources[source_id]
        for source_id in source_ids
        if source_id in catalog.evidence_sources
    ]
    absence_sources = [
        catalog.absence_sources[source_id]
        for source_id in source_ids
        if source_id in catalog.absence_sources
    ]
    allowed = set(candidate.allowed_severity_factors)
    accepted: list[SeverityFactorCode] = []
    rejected: list[RejectedSeverityFactor] = []
    for factor_code in proposed:
        if factor_code not in allowed:
            rejected.append(
                RejectedSeverityFactor(
                    factor_code=factor_code,
                    reason_code="FACTOR_NOT_ALLOWED_FOR_CANDIDATE",
                    evidence_source_ids=source_ids,
                )
            )
            continue
        if candidate.check_code.startswith("PO-"):
            factor_validator = _po_factor_has_required_evidence
        elif candidate.check_code.startswith("ICD-"):
            factor_validator = _icd_factor_has_required_evidence
        else:
            factor_validator = _lre_factor_has_required_evidence
        valid, reason_code = factor_validator(
            factor_code,
            text_sources=text_sources,
            absence_sources=absence_sources,
        )
        if valid:
            accepted.append(factor_code)
        else:
            rejected.append(
                RejectedSeverityFactor(
                    factor_code=factor_code,
                    reason_code=reason_code,
                    evidence_source_ids=source_ids,
                )
            )
    return accepted, rejected


def _po_severity_factors(values: list[str]) -> CandidateSeverityFactors:
    selected = set(values)
    return CandidateSeverityFactors(
        **{
            field_name: code in selected
            for code, field_name in _PO_SEVERITY_FACTOR_FIELDS.items()
        }
    )


def _merge_po_severity_factors(
    *values: CandidateSeverityFactors,
) -> CandidateSeverityFactors:
    return CandidateSeverityFactors(
        **{
            field_name: any(getattr(value, field_name) for value in values)
            for field_name in _PO_SEVERITY_FACTOR_FIELDS.values()
        }
    )


def _validate_po_control_codes(
    candidate: DeterministicRiskCandidate,
    selected: list[str],
    *,
    verdict: str,
) -> list[str]:
    allowed = set(_candidate_allowed_control_codes(candidate))
    unknown = [code for code in selected if code not in allowed]
    if unknown:
        raise DirectReviewError(
            "RISK_CONTROL_CODE_NOT_ALLOWED",
            f"Control Code crossed its Candidate boundary: {unknown}",
        )
    if verdict == "RISK" and not selected:
        raise DirectReviewError(
            "RISK_CONTROL_CODE_REQUIRED",
            f"RISK Candidate requires a Control Code: {candidate.candidate_id}",
        )
    if verdict != "RISK" and selected:
        raise DirectReviewError(
            "RISK_CONTROL_CODE_UNEXPECTED",
            "Only a RISK Candidate may select Control Codes",
        )
    return list(selected)


def _candidate_allowed_control_codes(
    candidate: DeterministicRiskCandidate,
) -> tuple[str, ...]:
    if candidate.check_code.startswith("PO-"):
        return _PO_ALLOWED_CONTROL_CODES[candidate.candidate_type]
    if candidate.check_code.startswith("ICD-"):
        return _ICD_ALLOWED_CONTROL_CODES[candidate.candidate_type]
    if candidate.check_code.startswith("LRE-"):
        return _LRE_ALLOWED_CONTROL_CODES[candidate.candidate_type]
    raise DirectReviewError(
        "RISK_CONTROL_POLICY_UNKNOWN",
        f"No Control Code Registry for {candidate.check_code}",
    )


def _po_decision_summary_perspective_warning_count(
    request: GenericReviewRequest,
    summary: str,
) -> int:
    role_words = (
        "甲方",
        "乙方",
        "我方",
        "相对方",
        request.our_party,
        request.counterparty,
    )
    return int(any(word and word in summary for word in role_words))


def _po_formal_finding_text(
    request: GenericReviewRequest,
    candidate: DeterministicRiskCandidate,
    *,
    severity_factors: CandidateSeverityFactors,
    control_codes: list[str],
    catalog: PoEvidenceCatalog,
) -> tuple[str, str, str, str]:
    if candidate.check_code.startswith("PO-"):
        template = _PO_FINDING_TEMPLATES[candidate.candidate_type]
        control_templates = _PO_CONTROL_CODE_TEMPLATES
    elif candidate.check_code.startswith("ICD-"):
        template = _ICD_FINDING_TEMPLATES[candidate.candidate_type]
        control_templates = _ICD_CONTROL_CODE_TEMPLATES
    else:
        template = _LRE_FINDING_TEMPLATES[candidate.candidate_type]
        control_templates = _LRE_CONTROL_CODE_TEMPLATES
    primary_quotes = [
        catalog.evidence_sources[source_id].quoted_text
        for source_id in candidate.primary_evidence_source_ids
        if source_id in catalog.evidence_sources
    ]
    evidence_summary = "；".join(primary_quotes[:2])
    if not evidence_summary:
        evidence_summary = candidate.trigger_reason
    issue = template["issue"].format(
        our_party=request.our_party,
        counterparty=request.counterparty,
        fact=candidate.trigger_reason,
        evidence=evidence_summary,
    )
    impact = template["impact"].format(
        our_party=request.our_party,
        counterparty=request.counterparty,
        fact=candidate.trigger_reason,
        severity=_po_factor_summary(severity_factors),
    )
    suggestion = "；".join(
        control_templates[code].format(
            our_party=request.our_party,
            counterparty=request.counterparty,
        )
        for code in control_codes
    )
    return template["title"], issue, impact, suggestion


def _po_factor_summary(factors: CandidateSeverityFactors) -> str:
    labels = {
        "unilateral_control": "存在单方控制",
        "no_effective_remedy": "缺少有效救济",
        "broad_scope": "影响范围较广",
        "financial_impact": "可能产生财务影响",
        "schedule_impact": "可能影响履行进度",
        "operational_impact": "可能影响实际履行",
        "missing_core_mechanism": "核心机制缺失",
        "ownership_ambiguity": "权属边界不清",
        "overbroad_transfer": "权利转让范围过宽",
        "exclusive_or_irrevocable": "许可具有独占或不可撤销性",
        "unlimited_scope": "用途、期限或范围缺少限制",
        "post_termination_effect": "终止后持续影响未封闭",
        "one_sided_protection": "保护义务单向失衡",
        "third_party_exposure": "存在第三方权利暴露",
        "no_return_or_deletion": "缺少返还或删除闭环",
        "no_security_standard": "缺少数据安全标准",
        "missing_incident_notice": "缺少安全事件通知机制",
        "unlimited_liability": "责任金额缺少上限",
        "one_sided_liability": "责任保护单向失衡",
        "overbroad_indemnity": "赔偿范围过宽",
        "indirect_loss_exposure": "包含间接或后果性损失",
        "liability_cap_bypassed": "责任上限可能被绕过",
        "cumulative_remedies": "多种责任可能累计",
        "unilateral_termination": "解除权单方化",
        "no_cure_period": "缺少合理整改期",
        "no_termination_settlement": "缺少终止结算",
        "post_termination_exposure": "终止后责任或退出边界不清",
        "automatic_renewal": "存在自动续期",
        "restricted_exit_window": "退出窗口受限",
        "dispute_clause_conflict": "争议解决条款冲突",
        "foreign_or_burdensome_forum": "争议地对我方形成实质负担",
    }
    selected = [
        label
        for field, label in labels.items()
        if getattr(factors, field)
    ]
    return "、".join(selected) if selected else "存在需要控制的履行风险"


def _po_candidate_risk_type(
    candidate: DeterministicRiskCandidate,
    spec: GenericCheckSpec,
) -> str:
    if candidate.candidate_type in spec.allowed_risk_types:
        return candidate.candidate_type
    if len(spec.allowed_risk_types) == 1:
        return spec.allowed_risk_types[0]
    raise DirectReviewError(
        "RISK_TYPE_REQUIRED",
        f"{candidate.check_code} has no unique deterministic risk_type mapping",
    )


def _po_risk_level(
    candidate: DeterministicRiskCandidate,
    factors: CandidateSeverityFactors,
) -> Literal["HIGH", "MEDIUM", "LOW", "INFO"]:
    rule = candidate.severity_rule_id
    if rule in {
        "PO_ACCEPTANCE_ABSENT_V1",
        "PO_CHANGE_CONTROL_ABSENT_V1",
        "PO_COOPERATION_ABSENT_V1",
    }:
        return "HIGH" if factors.missing_core_mechanism else "MEDIUM"
    if rule == "PO_QUALITY_STANDARD_V1":
        return (
            "HIGH"
            if factors.missing_core_mechanism
            and factors.operational_impact
            else "MEDIUM"
        )
    if rule in {
        "PO_SCOPE_EXPANSION_V1",
        "PO_UNILATERAL_CONTROL_V1",
        "PO_CHANGE_CONTROL_V1",
    }:
        severe_impact = (
            factors.broad_scope
            or factors.financial_impact
            or factors.schedule_impact
            or factors.operational_impact
        )
        return (
            "HIGH"
            if factors.unilateral_control
            and factors.no_effective_remedy
            and severe_impact
            else "MEDIUM"
        )
    if rule in {
        "PO_ACCEPTANCE_REVIEW_V1",
        "PO_WARRANTY_SUPPORT_V1",
        "PO_WARRANTY_SUPPORT_ABSENT_V1",
    }:
        return (
            "HIGH"
            if factors.no_effective_remedy
            and factors.operational_impact
            and (
                factors.missing_core_mechanism
                or factors.broad_scope
                or factors.schedule_impact
            )
            else "MEDIUM"
        )
    if rule == "PO_ASSIGNMENT_SUBCONTRACT_V1":
        return (
            "HIGH"
            if factors.no_effective_remedy
            and factors.broad_scope
            and factors.operational_impact
            else "MEDIUM"
        )
    if rule in {
        "PO_DELIVERY_SCHEDULE_V1",
        "PO_COOPERATION_DEPENDENCY_V1",
        "PO_PROJECTED_IR_REVIEW_V1",
        "PO_MISSING_EXPECTED_IR_V1",
    }:
        return (
            "HIGH"
            if factors.no_effective_remedy
            and (
                factors.financial_impact
                or factors.schedule_impact
                or factors.operational_impact
            )
            and (factors.broad_scope or factors.missing_core_mechanism)
            else "MEDIUM"
        )
    raise DirectReviewError(
        "SEVERITY_RULE_UNKNOWN",
        f"Unknown PO severity rule: {rule}",
    )


def _icd_risk_level(
    candidate: DeterministicRiskCandidate,
    factors: CandidateSeverityFactors,
) -> Literal["HIGH", "MEDIUM", "LOW", "INFO"]:
    """Apply the frozen ICD baseline and evidence-gated escalation rules."""
    rule = candidate.severity_rule_id
    if not rule.startswith("ICD_"):
        raise DirectReviewError(
            "SEVERITY_RULE_UNKNOWN",
            f"Unknown ICD severity rule: {rule}",
        )
    high_impact = factors.financial_impact or factors.operational_impact
    if candidate.candidate_type in {
        "FOREGROUND_IP_OWNERSHIP_REVIEW",
        "BACKGROUND_IP_LICENSE_REVIEW",
    }:
        rights_expansion = (
            factors.overbroad_transfer
            or factors.exclusive_or_irrevocable
            or factors.unlimited_scope
        )
        return "HIGH" if rights_expansion and high_impact else "MEDIUM"
    if candidate.candidate_type in {
        "THIRD_PARTY_IP_PROTECTION_REVIEW",
        "THIRD_PARTY_IP_PROTECTION_ABSENT",
    }:
        return (
            "HIGH"
            if factors.third_party_exposure and high_impact
            else "MEDIUM"
        )
    if candidate.candidate_type in {
        "DATA_PROCESSING_SECURITY_REVIEW",
        "DATA_PROCESSING_SECURITY_ABSENT",
    }:
        weak_security = (
            factors.no_security_standard
            or factors.missing_incident_notice
        )
        return "HIGH" if weak_security and high_impact else "MEDIUM"
    if candidate.candidate_type in _ICD_ALL_CANDIDATE_TYPES:
        return "MEDIUM"
    raise DirectReviewError(
        "SEVERITY_RULE_UNKNOWN",
        f"Unknown ICD Candidate severity rule: {rule}",
    )


def _lre_risk_level(
    candidate: DeterministicRiskCandidate,
    factors: CandidateSeverityFactors,
) -> Literal["HIGH", "MEDIUM", "LOW", "INFO"]:
    """Apply LRE deterministic baselines and evidence-gated escalation."""
    if candidate.candidate_type in {
        "OVERBROAD_LOSS_SCOPE_REVIEW",
        "LIABILITY_CAP_ABSENT",
        "LIABILITY_CAP_BYPASS_REVIEW",
        "OVERBROAD_INDEMNITY_REVIEW",
    }:
        return (
            "HIGH"
            if factors.unlimited_liability
            and (
                factors.indirect_loss_exposure
                or factors.overbroad_indemnity
                or factors.liability_cap_bypassed
            )
            else "MEDIUM"
        )
    if candidate.candidate_type == "CUMULATIVE_REMEDIES_REVIEW":
        # A direct financial consequence is inherent in cumulative monetary
        # remedies and must not, by itself, double-count the same harm as a
        # HIGH escalation. Uncapped, indirect-loss and cap-bypass exposure
        # are materialized under their dedicated LRE canonical roots.
        return "MEDIUM"
    if candidate.candidate_type == "TERMINATION_RIGHTS_REVIEW":
        return (
            "HIGH"
            if factors.unilateral_termination
            and factors.no_cure_period
            and factors.no_termination_settlement
            else "MEDIUM"
        )
    if candidate.candidate_type in {
        "FORCE_MAJEURE_MECHANISM_ABSENT",
        "DISPUTE_RESOLUTION_ABSENT",
    }:
        return "MEDIUM"
    if candidate.candidate_type in _LRE_ALL_CANDIDATE_TYPES:
        return "MEDIUM"
    raise DirectReviewError(
        "SEVERITY_RULE_UNKNOWN",
        f"Unknown LRE Candidate severity rule: {candidate.severity_rule_id}",
    )


def _candidate_risk_level(
    candidate: DeterministicRiskCandidate,
    factors: CandidateSeverityFactors,
) -> Literal["HIGH", "MEDIUM", "LOW", "INFO"]:
    if candidate.check_code.startswith("PO-"):
        return _po_risk_level(candidate, factors)
    if candidate.check_code.startswith("ICD-"):
        return _icd_risk_level(candidate, factors)
    if candidate.check_code.startswith("LRE-"):
        return _lre_risk_level(candidate, factors)
    raise DirectReviewError(
        "SEVERITY_RULE_UNKNOWN",
        f"No Candidate severity registry for {candidate.check_code}",
    )


def _po_canonical_root_groups(
    candidates: list[DeterministicRiskCandidate],
    decisions_by_id: dict[str, CandidateDecision],
    specs: dict[str, GenericCheckSpec],
    catalog: PoEvidenceCatalog,
) -> list[list[DeterministicRiskCandidate]]:
    risk_candidates = [
        candidate
        for candidate in candidates
        if decisions_by_id[candidate.candidate_id].verdict == "RISK"
    ]
    parents = list(range(len(risk_candidates)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left_index, left in enumerate(risk_candidates):
        for right_index in range(left_index + 1, len(risk_candidates)):
            right = risk_candidates[right_index]
            if _po_candidates_share_canonical_root(
                left,
                right,
                specs=specs,
                catalog=catalog,
            ):
                union(left_index, right_index)

    grouped: dict[int, list[DeterministicRiskCandidate]] = {}
    for index, candidate in enumerate(risk_candidates):
        grouped.setdefault(find(index), []).append(candidate)
    return list(grouped.values())


def _po_candidates_share_canonical_root(
    left: DeterministicRiskCandidate,
    right: DeterministicRiskCandidate,
    *,
    specs: dict[str, GenericCheckSpec],
    catalog: PoEvidenceCatalog,
) -> bool:
    cross_lre_check = (
        left.check_code.startswith("LRE-")
        and right.check_code.startswith("LRE-")
    )
    if (
        (left.check_code != right.check_code and not cross_lre_check)
        or left.canonical_root_type != right.canonical_root_type
        or left.root_severity_rule_id != right.root_severity_rule_id
        or left.merge_group_id is None
        or left.merge_group_id != right.merge_group_id
        or right.candidate_type not in left.merge_compatible_candidate_types
        or left.candidate_type not in right.merge_compatible_candidate_types
    ):
        return False
    if not cross_lre_check:
        spec = specs[left.check_code]
        if _po_candidate_risk_type(left, spec) != _po_candidate_risk_type(right, spec):
            return False
    left_anchors = _po_core_evidence_anchor_ids(left, catalog)
    right_anchors = _po_core_evidence_anchor_ids(right, catalog)
    return bool(left_anchors & right_anchors)


def _po_core_evidence_anchor_ids(
    candidate: DeterministicRiskCandidate,
    catalog: PoEvidenceCatalog,
) -> set[str]:
    return {
        source.anchor_id
        for source_id in candidate.core_primary_evidence_source_ids
        if (source := catalog.evidence_sources.get(source_id)) is not None
    }


def _po_root_risk_level(
    rule_id: str,
    factors: CandidateSeverityFactors,
    candidate_levels: list[str | None],
) -> Literal["HIGH", "MEDIUM", "LOW", "INFO"]:
    severity_rank = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
    valid_levels = [level for level in candidate_levels if level is not None]
    if not valid_levels:
        raise DirectReviewError(
            "RISK_ROOT_SEVERITY_INVALID",
            "A Canonical Risk Root requires at least one Candidate severity",
        )
    maximum = max(valid_levels, key=severity_rank.__getitem__)
    if rule_id == "PO_SCOPE_DELIVERY_ROOT_V1":
        if (
            factors.unilateral_control
            and factors.broad_scope
            and factors.schedule_impact
        ):
            return "HIGH"
        return maximum
    if rule_id == "LRE_UNBOUNDED_LIABILITY_ROOT_V1":
        if factors.unlimited_liability and (
            factors.indirect_loss_exposure
            or factors.overbroad_indemnity
            or factors.liability_cap_bypassed
        ):
            return "HIGH"
        return maximum
    return maximum


def _candidate_root_risk_level(
    rule_id: str,
    factors: CandidateSeverityFactors,
    candidate_levels: list[str | None],
) -> Literal["HIGH", "MEDIUM", "LOW", "INFO"]:
    if rule_id.startswith("PO_"):
        return _po_root_risk_level(rule_id, factors, candidate_levels)
    if rule_id.startswith("ICD_"):
        valid_levels = [level for level in candidate_levels if level is not None]
        if not valid_levels:
            raise DirectReviewError(
                "RISK_ROOT_SEVERITY_INVALID",
                "An ICD Canonical Risk Root requires Candidate severity",
            )
        rank = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
        return max(valid_levels, key=rank.__getitem__)
    if rule_id.startswith("LRE_"):
        valid_levels = [level for level in candidate_levels if level is not None]
        if not valid_levels:
            raise DirectReviewError(
                "RISK_ROOT_SEVERITY_INVALID",
                "An LRE Canonical Risk Root requires Candidate severity",
            )
        rank = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
        maximum = max(valid_levels, key=rank.__getitem__)
        if (
            rule_id == "LRE_UNBOUNDED_LIABILITY_ROOT_V1"
            and factors.unlimited_liability
            and (
                factors.indirect_loss_exposure
                or factors.overbroad_indemnity
            )
        ):
            return "HIGH"
        return maximum
    raise DirectReviewError(
        "RISK_ROOT_SEVERITY_INVALID",
        f"No Canonical Root severity registry for {rule_id}",
    )


def _po_root_formal_finding_text(
    request: GenericReviewRequest,
    candidates: list[DeterministicRiskCandidate],
    *,
    severity_factors: CandidateSeverityFactors,
    control_codes: list[str],
    catalog: PoEvidenceCatalog,
) -> tuple[str, str, str, str]:
    first = candidates[0]
    if len(candidates) == 1:
        return _po_formal_finding_text(
            request,
            first,
            severity_factors=severity_factors,
            control_codes=control_codes,
            catalog=catalog,
        )
    if first.canonical_root_type == "UNBOUNDED_LIABILITY_EXPOSURE":
        primary_ids = list(
            dict.fromkeys(
                source_id
                for candidate in candidates
                for source_id in candidate.core_primary_evidence_source_ids
            )
        )
        evidence_summary = "；".join(
            dict.fromkeys(
                catalog.evidence_sources[source_id].quoted_text
                for source_id in primary_ids
                if source_id in catalog.evidence_sources
            )
        )
        if not evidence_summary:
            evidence_summary = "；".join(
                dict.fromkeys(candidate.trigger_reason for candidate in candidates)
            )
        return (
            "赔偿范围开放且累计责任缺少有效上限",
            (
                "同一责任结构同时包含开放损失范围、赔偿责任或责任上限缺口。"
                f"Primary Evidence显示：{evidence_summary}。"
            ),
            (
                f"{request.our_party}可能承担缺少金额边界的直接、间接或第三方责任；"
                f"风险因素为：{_po_factor_summary(severity_factors)}。"
            ),
            "；".join(
                _LRE_CONTROL_CODE_TEMPLATES[code].format(
                    our_party=request.our_party,
                    counterparty=request.counterparty,
                )
                for code in control_codes
            ),
        )
    if first.canonical_root_type != "CORE_SCOPE_AND_DELIVERY_IMBALANCE":
        raise DirectReviewError(
            "RISK_ROOT_TEMPLATE_UNKNOWN",
            f"No merged Finding template for {first.canonical_root_type}",
        )
    primary_ids = list(
        dict.fromkeys(
            source_id
            for candidate in candidates
            for source_id in candidate.core_primary_evidence_source_ids
        )
    )
    evidence_summary = "；".join(
        dict.fromkeys(
            catalog.evidence_sources[source_id].quoted_text
            for source_id in primary_ids
            if source_id in catalog.evidence_sources
        )
    )
    if not evidence_summary:
        evidence_summary = "；".join(
            dict.fromkeys(candidate.trigger_reason for candidate in candidates)
        )
    title = "履行范围、交付边界及进度责任存在同源失衡风险"
    issue = (
        f"同一组核心履行条款既允许{request.counterparty}提出开放式要求，"
        f"又要求{request.our_party}按要求完成项目任务和交付，但未同步封闭"
        f"范围、书面变更和交付节点。Primary Evidence显示：{evidence_summary}。"
    )
    impact = (
        f"{request.our_party}可能在工作范围被扩大时仍承担原有交付期限和"
        f"进度责任；风险因素为：{_po_factor_summary(severity_factors)}。"
    )
    suggestion = "；".join(
        _PO_CONTROL_CODE_TEMPLATES[code].format(
            our_party=request.our_party,
            counterparty=request.counterparty,
        )
        for code in control_codes
    )
    return title, issue, impact, suggestion


def _validate_po_perspective_language(
    request: GenericReviewRequest,
    finding: FindingDraft,
) -> None:
    combined = "".join(
        (
            finding.title,
            finding.issue,
            finding.impact_to_our_party,
            finding.suggestion,
        )
    )
    roles = contract_party_roles(
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
    )
    opposite = roles.counterparty_role
    patterns = (
        f"我方（{opposite}）",
        f"我方({opposite})",
        f"我方即{opposite}",
        f"我方为{opposite}",
    )
    if any(pattern in combined for pattern in patterns):
        raise DirectReviewError(
            "RISK_PERSPECTIVE_CONFLICT",
            "Finding narrative reverses the frozen review perspective",
        )


def generic_request_from_context(
    value: BaseModel | dict[str, Any],
    *,
    allow_absence_only_evidence_catalog: bool = False,
) -> GenericReviewRequest:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value

    def project_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "ir_type": item["ir_type"],
            "item_id": item["item_id"],
            "subject": item.get("subject"),
            "predicate": item["predicate"],
            "object": item.get("object"),
            "source_anchors": [
                {"anchor_id": anchor["anchor_id"]}
                for anchor in item["source_anchors"]
            ],
        }

    return GenericReviewRequest.model_validate(
        {
            "review_id": payload["review_id"],
            "document_id": payload["document_id"],
            "generation_id": payload["generation_id"],
            "attempt_no": payload["attempt_no"],
            "plan_id": payload["plan_id"],
            "context_hash": payload["context_hash"],
            "unit_id": payload["unit_id"],
            "batch_id": payload["batch_id"],
            "perspective": payload["perspective"],
            "our_party": payload["our_party"],
            "counterparty": payload["counterparty"],
            "contract_type": payload["contract_type"],
            "review_attitude": payload["review_attitude"],
            "assigned_check_specs": [
                {
                    "check_code": item["check_code"],
                    "review_question": item["review_question"],
                    "allowed_categories": item["allowed_categories"],
                    "allowed_risk_types": item["allowed_risk_types"],
                    "required_ir_types": item["required_ir_types"],
                    "criticality": item["criticality"],
                }
                for item in payload["check_specs"]
            ],
            "definitions": [project_item(item) for item in payload["definitions"]],
            "projected_ir_items": [
                project_item(item) for item in payload["projected_ir_items"]
            ],
            "source_excerpts": payload["source_excerpts"],
            "evidence_sources": payload.get("evidence_sources", []),
            "absence_evidence_sources": payload.get(
                "absence_evidence_sources",
                [],
            ),
            "check_evidence_policies": payload.get(
                "check_evidence_policies",
                [],
            ),
            "present_ir_types": payload["present_ir_types"],
            "missing_ir_types": payload["missing_ir_types"],
            "estimated_input_tokens": payload["estimated_input_tokens"],
        },
        context={
            "allow_absence_only_evidence_catalog": (
                allow_absence_only_evidence_catalog
            )
        },
    )


def _generic_prompt(
    request: GenericReviewRequest,
) -> tuple[
    str,
    dict[str, CommercialIrItem],
    dict[str, CommercialSourceExcerpt],
]:
    excerpts = sorted(
        request.source_excerpts,
        key=lambda item: (item.block_no, item.char_start, item.anchor_id),
    )
    anchor_refs = {f"A{index:03d}": item for index, item in enumerate(excerpts, 1)}
    anchor_ref_by_id = {item.anchor_id: ref for ref, item in anchor_refs.items()}
    ir_items = [*request.definitions, *request.projected_ir_items]
    ir_refs = {f"I{index:03d}": item for index, item in enumerate(ir_items, 1)}
    projected: list[list[object]] = []
    for ref, item in ir_refs.items():
        projected.append(
            [
                ref,
                item.ir_type,
                item.subject,
                item.predicate,
                item.object,
                [
                    anchor_ref_by_id[anchor.anchor_id]
                    for anchor in item.source_anchors
                ],
            ]
        )
    candidates = _build_generic_candidates(request, ir_refs, anchor_ref_by_id)
    po_catalog = (
        _po_evidence_catalog(
            request,
            candidates,
            ir_refs,
            anchor_refs,
        )
        if request.unit_id
        in {
            "performance_obligations",
            "ip_confidentiality_data",
            "liability_remedies_exit",
        }
        else None
    )
    if po_catalog is not None:
        return (
            _po_candidate_prompt(request, candidates, po_catalog),
            ir_refs,
            anchor_refs,
        )
    if po_catalog is None:
        candidate_legend = [
            "candidate_id",
            "check_code",
            "candidate_type",
            "trigger_reason",
            "candidate_ir_refs",
            "candidate_evidence_refs",
            "requires_model_decision",
        ]
        candidate_rows = [
            [
                item.candidate_id,
                item.check_code,
                item.candidate_type,
                item.trigger_reason,
                item.candidate_ir_refs,
                item.candidate_evidence_refs,
                item.requires_model_decision,
            ]
            for item in candidates
        ]
    else:
        candidate_legend = [
            "candidate_id",
            "check_code",
            "candidate_type",
            "trigger_reason",
            "candidate_evidence_source_ids",
            "candidate_absence_source_ids",
            "requires_model_decision",
        ]
        candidate_rows = [
            [
                item.candidate_id,
                item.check_code,
                item.candidate_type,
                item.trigger_reason,
                list(
                    po_catalog.candidate_source_ids[
                        (item.check_code, item.candidate_type)
                    ][0]
                ),
                list(
                    po_catalog.candidate_source_ids[
                        (item.check_code, item.candidate_type)
                    ][1]
                ),
                item.requires_model_decision,
            ]
            for item in candidates
        ]
    output_rules = [
        "每个required_check_code恰好返回一次，不得返回其他check_code",
        "无实质风险时status=REVIEWED且findings=[]",
        "有Finding时status必须为REVIEWED，category/risk_type必须等于Check允许值",
        "reason_code由Python生成，模型禁止输出",
        "FVA-002必须先输出assessment_type和external_verification_required，再按三态一致性决定是否有Finding",
        "FVA-002缺少外部材料只能判外部核验或无可见问题，禁止断言现实中无资格或无授权",
        "FVA-002不得讨论或复述source_policy.disallowed_semantic_topics",
        "deterministic_candidates只是待裁决假设，不得不经Evidence判断直接转为Finding",
        "PO只允许审履行范围、交付进度、质量/验收、配合、变更、质保和权利义务平衡",
        "PO不得输出授权签署、劳动社保、外部资质、知识产权保密数据、责任终止争议或纯付款税费风险",
        "禁止输出未列字段、技术ID、Hash、Block和字符位置",
    ]
    if po_catalog is None:
        output_rules[4:4] = [
            "TEXT_QUOTE/CONTEXT必须同时给出同一IR允许的ir_ref和evidence_ref",
            "每条Evidence必须显式填写evidence_type；有ir_ref/evidence_ref时通常为TEXT_QUOTE或CONTEXT",
            "ABSENCE只能用于真实缺失，必须填写checked_scope和verification_note",
        ]
    else:
        output_rules[2] = (
            "有Finding时status必须为REVIEWED；分类技术字段按本批确定性补全规则处理"
        )
        output_rules[4:4] = [
            "每条Finding天然属于所在check_result；Finding内不要重复输出check_code和category",
            "Finding可用candidate_ids选择当前Check提供的确定性候选；不得创造candidate_id",
            "所选候选只有一个candidate_type时可省略risk_type；否则必须从当前Check的allowed_risk_types中明确选择",
            "每个PO check_result只能从它自己的allowed_evidence_sources或allowed_absence_sources选择evidence_source_ids",
            "即使某source_id出现在同Batch的其他Check中，当前Check未列出时也绝对禁止选择",
            "一个source_id已不可分割地绑定IR、Anchor和原文；不得自行组合、改写或构造source_id",
            "不得输出ir_ref、evidence_ref、anchor_id、block_id、字符位置、quoted_text或Hash",
            "多条款Finding可选择多个source_id；Python从Source确定性派生全部技术Evidence",
            "PO-003不能仅凭乙方保证材料真实认定甲方配合义务缺失；若证明双方义务不对等，须选择分别直接支持该关系的多个合法Source",
        ]
    roles = contract_party_roles(
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
    )
    payload = {
        "review_context": {
            "perspective": request.perspective,
            "our_party": request.our_party,
            "counterparty": request.counterparty,
            "party_a": roles.party_a_name,
            "party_b": roles.party_b_name,
            "our_contract_role": roles.our_role,
            "counterparty_contract_role": roles.counterparty_role,
            "contract_type": request.contract_type,
            "review_attitude": request.review_attitude,
        },
        "unit": {
            "unit_id": request.unit_id,
            "name": UNIT_NAMES[request.unit_id],
            "boundary": UNIT_BOUNDARIES[request.unit_id],
        },
        "assigned_check_legend": [
            "check_code",
            "decision_rule",
            "category",
            "risk_type",
        ],
        "assigned_checks": [
            [
                spec.check_code,
                CHECK_DECISION_RULES[spec.check_code],
                spec.allowed_categories[0],
                spec.allowed_risk_types[0],
            ]
            for spec in request.assigned_check_specs
        ],
        "deterministic_candidate_legend": candidate_legend,
        "deterministic_candidates": candidate_rows,
        "projected_ir_legend": [
            "ir_ref",
            "ir_type",
            "subject",
            "predicate",
            "object",
            "evidence_refs",
        ],
        "projected_ir": projected,
        "source_excerpt_legend": ["evidence_ref", "verbatim_text"],
        "source_excerpts": [
            [ref, item.quoted_text]
            for ref, item in anchor_refs.items()
        ],
        "output_contract": {
            "required_check_codes": [
                item.check_code for item in request.assigned_check_specs
            ],
            "rules": output_rules,
            "required_check_fields": [
                "check_code",
                "status",
                "decision_note",
                "findings",
            ],
            "fva002_required_fields": [
                "assessment_type",
                "external_verification_required",
            ],
            "fva002_state_contract": {
                "TEXTUAL_AUTHORITY_RISK": {
                    "status": "REVIEWED",
                    "external_verification_required": False,
                    "findings": "至少1条且必须有当前合同原文Evidence",
                    "meaning": "仅合同文本明确存在主体/签署/授权冲突、无权、越权或明确条件缺失",
                },
                "EXTERNAL_VERIFICATION_REQUIRED": {
                    "status": "REVIEWED",
                    "external_verification_required": True,
                    "findings": [],
                    "meaning": "合同没有明确文本冲突，但现实授权只能依赖外部材料确认",
                    "decision_note": (
                        "说明需要核验法定代表人证明、授权委托书、董事会/股东会批准、"
                        "营业执照或内部审批；不得声称已经无权或未授权；"
                        "不得加入或复述source_policy禁止的非授权领域材料"
                    ),
                },
                "NO_VISIBLE_ISSUE": {
                    "status": "REVIEWED",
                    "external_verification_required": False,
                    "findings": [],
                    "meaning": "合同文本一致且没有特别外部核验线索",
                },
            },
            "finding_required_fields": (
                [
                    "risk_level",
                    "title",
                    "issue",
                    "impact_to_our_party",
                    "suggestion",
                    "evidence_source_ids",
                ]
                if po_catalog is not None
                else [
                    "check_code",
                    "category",
                    "risk_type",
                    "risk_level",
                    "title",
                    "issue",
                    "impact_to_our_party",
                    "suggestion",
                    "evidence",
                ]
            ),
            "finding_context_fields": (
                {
                    "check_code": "由父级check_result绑定，Finding内禁止重复",
                    "category": "由Registry按check_code确定性补全，Finding内禁止重复",
                    "candidate_ids": "可选；只能选择当前Check提供的candidate_id",
                    "risk_type": (
                        "候选类型或唯一allowed_risk_type可确定时可省略；"
                        "否则必须显式返回"
                    ),
                }
                if po_catalog is not None
                else {
                    "check_code": "与父级check_result一致",
                    "category": "必须等于Check允许值",
                    "risk_type": "必须等于Check允许值",
                }
            ),
            "minimal_evidence_shapes": {
                "TEXT_QUOTE": {
                    "evidence_type": "TEXT_QUOTE",
                    "ir_ref": "I001",
                    "evidence_ref": "A001",
                },
                "ABSENCE": {
                    "evidence_type": "ABSENCE",
                    "checked_scope": "被检查的合同范围",
                    "verification_note": "仅说明文本范围内的缺失",
                },
            },
        },
    }
    if (
        request.unit_id == "formation_validity_authority"
        and request.check_evidence_policies
    ):
        specs = {
            item.check_code: item for item in request.assigned_check_specs
        }
        policies = {
            item.check_code: item for item in request.check_evidence_policies
        }
        source_by_id = {
            item.source_id: item for item in request.evidence_sources
        }
        absence_by_id = {
            item.source_id: item for item in request.absence_evidence_sources
        }
        ir_ref_by_item_id = {
            item.item_id: ref for ref, item in ir_refs.items()
        }
        evidence_ref_by_anchor_id = {
            item.anchor_id: ref for ref, item in anchor_refs.items()
        }
        payload["assigned_checks"] = []
        for check_code in (
            item.check_code for item in request.assigned_check_specs
        ):
            policy = policies[check_code]
            allowed_sources = [
                source_by_id[source_id]
                for source_id in policy.allowed_evidence_source_ids
                if source_id in source_by_id
            ]
            payload["assigned_checks"].append(
                {
                    "check_code": check_code,
                    "decision_rule": CHECK_DECISION_RULES[check_code],
                    "category": specs[check_code].allowed_categories[0],
                    "risk_type": specs[check_code].allowed_risk_types[0],
                    "deterministic_candidates": [
                        [
                            item.candidate_id,
                            item.candidate_type,
                            item.trigger_reason,
                            item.candidate_ir_refs,
                            item.candidate_evidence_refs,
                            item.requires_model_decision,
                        ]
                        for item in candidates
                        if item.check_code == check_code
                    ],
                    "allowed_evidence_sources": [
                        [
                            source.source_id,
                            ir_ref_by_item_id[source.ir_item_id],
                            evidence_ref_by_anchor_id[source.anchor_id],
                            source.ir_type,
                            source.subject,
                            source.predicate,
                            source.object,
                            source.quoted_text,
                        ]
                        for source in allowed_sources
                    ],
                    "allowed_absence_sources": [
                        absence_by_id[source_id].model_dump(mode="json")
                        for source_id in policy.allowed_absence_source_ids
                        if source_id in absence_by_id
                    ],
                    **(
                        {
                            "source_policy": {
                                "allowed_ir_types": ["rights", "obligations"],
                                "allowed_candidate_types": [
                                    "PROJECTED_IR_REVIEW",
                                    "MISSING_EXPECTED_IR",
                                ],
                                "allowed_evidence_source_ids": list(
                                    policy.allowed_evidence_source_ids
                                ),
                                "allowed_absence_source_ids": list(
                                    policy.allowed_absence_source_ids
                                ),
                                "disallowed_semantic_topics": [
                                    "PERSONNEL_QUALIFICATION",
                                    "PROJECT_STAFF_CAPABILITY",
                                    "EMPLOYMENT_RELATIONSHIP",
                                    "SOCIAL_INSURANCE",
                                    "STAFFING_LEVEL",
                                    "DELIVERY_TEAM_CONFIGURATION",
                                    "TECHNICAL_CAPABILITY",
                                    "PROJECT_EXPERIENCE",
                                    "GENERAL_PERFORMANCE_CAPABILITY",
                                ],
                            },
                            "deterministic_precondition": {
                                "textual_authority_conflict_visible": any(
                                    re.search(
                                        r"(无权|越权|无授权|授权范围不足|"
                                        r"签署主体.{0,12}(不一致|冲突)|"
                                        r"代表人.{0,12}(不一致|冲突))",
                                        source.quoted_text,
                                    )
                                    for source in allowed_sources
                                ),
                                "allowed_assessment_types": (
                                    ["TEXTUAL_AUTHORITY_RISK"]
                                    if any(
                                        re.search(
                                            r"(无权|越权|无授权|授权范围不足|"
                                            r"签署主体.{0,12}(不一致|冲突)|"
                                            r"代表人.{0,12}(不一致|冲突))",
                                            source.quoted_text,
                                        )
                                        for source in allowed_sources
                                    )
                                    else [
                                        "EXTERNAL_VERIFICATION_REQUIRED",
                                        "NO_VISIBLE_ISSUE",
                                    ]
                                ),
                            },
                        }
                        if check_code == "FVA-002"
                        else {}
                    ),
                }
            )
        payload.pop("assigned_check_legend")
        payload.pop("deterministic_candidate_legend")
        payload.pop("deterministic_candidates")
        payload.pop("projected_ir_legend")
        payload.pop("projected_ir")
        payload.pop("source_excerpt_legend")
        payload.pop("source_excerpts")
        payload["output_contract"]["rules"].extend(
            [
                "每个FVA Check只能读取自身allowed_evidence_sources"
                "和allowed_absence_sources",
                "即使同Batch其他Check拥有某项Source，当前Check未列出时也禁止使用",
                "FVA-002输入只允许主体、签署、代表权、授权、签章和生效审批材料",
            ]
        )
    if po_catalog is not None:
        payload.pop("projected_ir_legend")
        payload.pop("projected_ir")
        payload.pop("source_excerpt_legend")
        payload.pop("source_excerpts")
        payload.pop("assigned_check_legend")
        payload.pop("deterministic_candidate_legend")
        payload.pop("deterministic_candidates")
        specs = {
            item.check_code: item for item in request.assigned_check_specs
        }
        payload["assigned_checks"] = [
            {
                "check_code": check_code,
                "review_question": specs[check_code].review_question,
                "decision_rule": CHECK_DECISION_RULES[check_code],
                "registry_category": specs[check_code].allowed_categories[0],
                "allowed_risk_types": specs[check_code].allowed_risk_types,
                "deterministic_candidates": [
                    {
                        "candidate_id": item.candidate_id,
                        "candidate_type": item.candidate_type,
                        "trigger_reason": item.trigger_reason,
                        "candidate_evidence_source_ids": list(
                            po_catalog.candidate_source_ids[
                                (item.check_code, item.candidate_type)
                            ][0]
                        ),
                        "candidate_absence_source_ids": list(
                            po_catalog.candidate_source_ids[
                                (item.check_code, item.candidate_type)
                            ][1]
                        ),
                    }
                    for item in candidates
                    if item.check_code == check_code
                ],
                "allowed_evidence_sources": [
                    {
                        "source_id": source_id,
                        "ir_type": po_catalog.evidence_sources[source_id].ir_type,
                        "subject": po_catalog.evidence_sources[source_id].subject,
                        "predicate": po_catalog.evidence_sources[source_id].predicate,
                        "object": po_catalog.evidence_sources[source_id].object,
                        "quoted_text": po_catalog.evidence_sources[
                            source_id
                        ].quoted_text,
                    }
                    for source_id in po_catalog.allowed_source_ids_by_check[
                        check_code
                    ]
                    if source_id in po_catalog.evidence_sources
                ],
                "allowed_absence_sources": [
                    {
                        "source_id": source_id,
                        "checked_scope": po_catalog.absence_sources[
                            source_id
                        ].checked_scope,
                        "verification_method": po_catalog.absence_sources[
                            source_id
                        ].verification_method,
                        "present_ir_types": po_catalog.absence_sources[
                            source_id
                        ].present_ir_types,
                        "missing_target": po_catalog.absence_sources[
                            source_id
                        ].missing_target,
                    }
                    for source_id in po_catalog.allowed_source_ids_by_check[
                        check_code
                    ]
                    if source_id in po_catalog.absence_sources
                ],
            }
            for check_code in (
                item.check_code for item in request.assigned_check_specs
            )
        ]
        payload["output_contract"]["minimal_evidence_shapes"] = {
            "SOURCE_BACKED": {
                "evidence_source_ids": [
                    next(iter(po_catalog.evidence_sources), "risk-es-" + "0" * 32)
                ]
            },
            "ABSENCE": {
                "evidence_source_ids": [
                    next(iter(po_catalog.absence_sources), "risk-as-" + "0" * 32)
                ]
            },
        }
    return (
        "审查当前Batch。仅返回JSON，顶层只能是check_results：\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        ir_refs,
        anchor_refs,
    )


def generic_input_diagnostics(
    request: GenericReviewRequest,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Return hashes only; never persist the complete model prompt."""
    if prompt is None:
        prompt, ir_refs, anchor_refs = _generic_prompt(request)
    else:
        excerpts = sorted(
            request.source_excerpts,
            key=lambda item: (item.block_no, item.char_start, item.anchor_id),
        )
        anchor_refs = {
            f"A{index:03d}": item for index, item in enumerate(excerpts, 1)
        }
        anchor_ref_by_id = {
            item.anchor_id: ref for ref, item in anchor_refs.items()
        }
        ir_items = [*request.definitions, *request.projected_ir_items]
        ir_refs = {
            f"I{index:03d}": item for index, item in enumerate(ir_items, 1)
        }
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        {item.anchor_id: ref for ref, item in anchor_refs.items()},
    )

    def digest(value: Any) -> str:
        text = (
            value
            if isinstance(value, str)
            else json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    return {
        "batch_context_hash": request.context_hash,
        "assigned_checks_hash": digest(
            [item.model_dump(mode="json") for item in request.assigned_check_specs]
        ),
        "candidate_set_hash": digest(
            [item.model_dump(mode="json") for item in candidates]
        ),
        "evidence_policy_hash": digest(
            {
                "policies": [
                    item.model_dump(mode="json")
                    for item in request.check_evidence_policies
                ],
                "evidence_source_ids": [
                    item.source_id for item in request.evidence_sources
                ],
                "absence_source_ids": [
                    item.source_id for item in request.absence_evidence_sources
                ],
            }
        ),
        "system_prompt_hash": digest(_GENERIC_SYSTEM_PROMPT),
        "user_prompt_hash": digest(prompt),
        "serialized_prompt_hash": digest(
            [
                {"role": "system", "content": _GENERIC_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        ),
    }


def _po_candidate_prompt(
    request: GenericReviewRequest,
    candidates: list[DeterministicRiskCandidate],
    catalog: PoEvidenceCatalog,
) -> str:
    specs = {item.check_code: item for item in request.assigned_check_specs}
    source_payload = {
        source_id: {
            "source_id": source_id,
            "ir_type": source.ir_type,
            "subject": source.subject,
            "predicate": source.predicate,
            "object": source.object,
            "quoted_text": source.quoted_text,
        }
        for source_id, source in catalog.evidence_sources.items()
    }
    source_payload.update(
        {
            source_id: {
                "source_id": source_id,
                "evidence_type": "ABSENCE",
                "checked_scope": source.checked_scope,
                "verification_method": source.verification_method,
                "present_ir_types": source.present_ir_types,
                "missing_target": source.missing_target,
            }
            for source_id, source in catalog.absence_sources.items()
        }
    )
    roles = contract_party_roles(
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
    )
    payload = {
        "review_context": {
            "perspective": request.perspective,
            "our_party": request.our_party,
            "counterparty": request.counterparty,
            "party_a": roles.party_a_name,
            "party_b": roles.party_b_name,
            "our_contract_role": roles.our_role,
            "counterparty_contract_role": roles.counterparty_role,
            "contract_type": request.contract_type,
            "review_attitude": request.review_attitude,
            "party_role_rule": (
                "甲方、乙方由party_a和party_b固定；不得根据付款方、"
                "履约方或参数顺序重新猜测，不得在任何输出中倒置主体"
            ),
        },
        "unit": {
            "unit_id": request.unit_id,
            "name": UNIT_NAMES[request.unit_id],
            "boundary": UNIT_BOUNDARIES[request.unit_id],
        },
        "assigned_checks": [
            {
                "check_code": check_code,
                "review_question": specs[check_code].review_question,
                "decision_rule": CHECK_DECISION_RULES[check_code],
                "candidates": [
                    [
                        item.candidate_id,
                        item.candidate_type,
                        item.candidate_strength,
                        item.criticality,
                        item.facts,
                        item.trigger_conditions,
                        item.mitigating_conditions,
                        item.disqualifying_conditions,
                        item.primary_evidence_requirements,
                        item.absence_evidence_requirements,
                        item.primary_evidence_source_ids,
                        item.allowed_supporting_evidence_source_ids,
                        item.allowed_counter_evidence_source_ids,
                        item.severity_rule_id,
                        item.allowed_severity_factors,
                        (
                            item.po003_precondition.model_dump(mode="json")
                            if item.po003_precondition is not None
                            else (
                                item.icd_perspective_precondition.model_dump(
                                    mode="json"
                                )
                                if item.icd_perspective_precondition is not None
                                else None
                            )
                        ),
                        list(_candidate_allowed_control_codes(item)),
                    ]
                    for item in candidates
                    if item.check_code == check_code
                    and item.requires_model_decision
                ],
                "allowed_evidence_sources": [
                    [
                        source_payload[source_id]["source_id"],
                        source_payload[source_id]["ir_type"],
                        source_payload[source_id]["quoted_text"],
                    ]
                    for source_id in catalog.allowed_source_ids_by_check[
                        check_code
                    ]
                    if source_id in source_payload
                    and source_id in catalog.evidence_sources
                ],
                "allowed_absence_sources": [
                    [
                        source_payload[source_id]["source_id"],
                        source_payload[source_id]["checked_scope"],
                        source_payload[source_id]["verification_method"],
                        source_payload[source_id]["missing_target"],
                    ]
                    for source_id in catalog.allowed_source_ids_by_check[
                        check_code
                    ]
                    if source_id in source_payload
                    and source_id in catalog.absence_sources
                ],
            }
            for check_code in (
                item.check_code for item in request.assigned_check_specs
            )
        ],
        "candidate_legend": [
            "candidate_id",
            "candidate_type",
            "candidate_strength",
            "criticality",
            "facts",
            "trigger_conditions",
            "mitigating_conditions",
            "disqualifying_conditions",
            "primary_evidence_requirements",
            "absence_evidence_requirements",
            "primary_evidence_source_ids",
            "allowed_supporting_evidence_source_ids",
            "allowed_counter_evidence_source_ids",
            "severity_rule_id",
            "allowed_severity_factors",
            "candidate_precondition",
            "allowed_control_codes",
        ],
        "output_contract": {
            "required_candidate_ids": [
                item.candidate_id
                for item in candidates
                if item.requires_model_decision
            ],
            "candidate_decision_fields": [
                "candidate_id",
                "verdict",
                "decision_summary",
                "severity_factors",
                "supporting_evidence_source_ids",
                "counter_evidence_source_ids",
                "recommended_control_codes",
            ],
            "severity_factor_policy": {
                factor_code: {
                    key: value
                    for key, value in {
                        "evidence_types": policy.required_evidence_types,
                        "text_signals": policy.required_text_signals,
                        "absence_types": policy.required_absence_source_types,
                        "disqualifying_signals": (
                            policy.conflicting_or_disqualifying_signals
                        ),
                    }.items()
                    if value
                }
                for factor_code, policy in _candidate_severity_policies(
                    request.unit_id
                ).items()
                if any(
                    factor_code in candidate.allowed_severity_factors
                    for candidate in candidates
                    if candidate.requires_model_decision
                )
            },
            "rules": [
                "每个required_candidate_id必须返回一次且仅一次，并保持输入顺序",
                "不得返回未知Candidate，不得用Check级结论代替Candidate裁决",
                "RISK必须选择至少一个allowed_control_code；其他verdict的recommended_control_codes必须为空",
                "NO_RISK不得使用空泛结论；HARD_RULE和STRONG_SIGNAL必须引用合法Counter Evidence",
                "INSUFFICIENT_EVIDENCE必须在decision_summary明确说明缺少的证据",
                "Primary Evidence由Python固定，输出Schema中不存在Primary字段",
                "Supporting和Counter只能从当前Candidate各自允许列表选择",
                "severity_factors只能从当前Candidate的allowed_severity_factors选择，且必须由当前Candidate Evidence直接支持；不得返回risk_level",
                "FINANCIAL_IMPACT必须有当前风险导致明确金钱后果的直接Evidence",
                "SCHEDULE_IMPACT必须有当前风险导致期限、工期、延期或逾期后果的直接Evidence；仅出现完成、交付、履行或时间节点不成立",
                "NO_EFFECTIVE_REMEDY必须有明示排除救济的文本或合法Absence Source；不得根据未看到条款自由推断",
                "candidate_type以_ABSENT结尾且判RISK时必须包含MISSING_CORE_MECHANISM",
                "SCOPE_EXPANSION、RIGHTS_OBLIGATIONS_IMBALANCE或CHANGE_CONTROL_REVIEW判RISK时必须包含UNILATERAL_CONTROL",
                *(
                    [
                        "有形交付、资料提供或普通项目使用权不等于知识产权转让、永久许可或无限许可",
                        "权属缺失、许可缺失、保密完整性缺失、数据安全缺失和返还删除缺失只能依据当前Candidate的合法Absence Source",
                        "不得在没有数据处理场景时推断数据泄露、安全能力缺失、行政违法或处罚后果",
                        "不得把责任上限、解除权、付款、发票、税务、验收或主体授权作为ICD独立风险根因",
                    ]
                    if request.unit_id == "ip_confidentiality_data"
                    else []
                ),
                *(
                    [
                        "不得把义务本身不平衡重复报成LRE；LRE只审违反义务后的责任、赔偿、解除、终止、不可抗力和争议机制",
                        "责任上限缺失、终止结算缺失、不可抗力缺失及争议解决缺失只能依据当前Candidate合法Absence Source",
                        "LIABILITY_CAP_BYPASSED必须同时存在责任上限文本与可能绕过上限的责任文本",
                        "INDIRECT_LOSS_EXPOSURE必须有利润、商誉、机会、可预期利益或其他间接损失明文",
                        "合同同时给予双方解除权并设置整改期时，不得仅因一方触发条件较多就认定我方风险",
                        "仅出现诉讼费或仲裁费不构成争议解决条款，也不能据此推断法院与仲裁冲突",
                        "不存在续期文本时不得提出AUTOMATIC_RENEWAL或RESTRICTED_EXIT_WINDOW",
                        "LRE判NO_RISK时可将当前Candidate已经列明的Primary Evidence Source ID直接填入Counter；Primary ID无需在allowed_counter列表重复列出",
                        "LRE判RISK时，若当前Candidate的allowed_supporting_evidence_source_ids为空，supporting_evidence_source_ids必须严格返回空数组",
                        "不得把其他Candidate的Primary或Supporting Source用于当前Candidate，即使两个Candidate将由Python归入同一Canonical Root",
                    ]
                    if request.unit_id == "liability_remedies_exit"
                    else []
                ),
                "Counter允许列表为空不表示证据不足；若Primary Evidence已满足触发条件应判RISK",
                "decision_summary只描述依据，禁止写甲方、乙方、我方、相对方或主体名称",
                "不得输出check_code、category、risk_type、risk_level、perspective、our_party、counterparty、Primary Evidence、正式Finding文案或Evidence技术字段",
            ],
        },
        "source_legends": {
            "allowed_evidence_sources": [
                "source_id",
                "ir_type",
                "quoted_text",
            ],
            "allowed_absence_sources": [
                "source_id",
                "checked_scope",
                "verification_method",
                "missing_target",
            ],
        },
    }
    return (
        "逐项裁决当前Batch中的全部RiskCandidate。仅返回JSON：\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


def _build_generic_candidates(
    request: GenericReviewRequest,
    ir_refs: dict[str, CommercialIrItem],
    anchor_ref_by_id: dict[str, str],
) -> list[DeterministicRiskCandidate]:
    if request.unit_id == "performance_obligations":
        return _build_po_candidates(request, ir_refs, anchor_ref_by_id)
    if request.unit_id == "ip_confidentiality_data":
        return _build_icd_candidates(request, ir_refs, anchor_ref_by_id)
    if request.unit_id == "liability_remedies_exit":
        return _build_lre_candidates(request, ir_refs, anchor_ref_by_id)
    candidates = []
    policies = {
        item.check_code: item for item in request.check_evidence_policies
    }
    source_by_id = {item.source_id: item for item in request.evidence_sources}
    for spec in request.assigned_check_specs:
        policy = policies.get(spec.check_code)
        allowed_item_ids = {
            source_by_id[source_id].ir_item_id
            for source_id in (
                policy.allowed_evidence_source_ids if policy is not None else []
            )
            if source_id in source_by_id
        }
        refs = sorted(
            ref
            for ref, item in ir_refs.items()
            if item.ir_type in spec.required_ir_types
            and (
                request.unit_id != "formation_validity_authority"
                or policy is None
                or item.item_id in allowed_item_ids
            )
        )
        evidence_refs = sorted(
            {
                anchor_ref_by_id[anchor.anchor_id]
                for ref in refs
                for anchor in ir_refs[ref].source_anchors
            }
        )
        candidates.append(
            DeterministicRiskCandidate(
                candidate_id=_stable_id(
                    "risk-candidate",
                    {
                        "check_code": spec.check_code,
                        "candidate_type": (
                            "PROJECTED_IR_REVIEW"
                            if refs
                            else "MISSING_EXPECTED_IR"
                        ),
                        "required_ir_types": spec.required_ir_types,
                        "candidate_ir_refs": refs,
                        "candidate_evidence_refs": evidence_refs,
                    },
                ),
                check_code=spec.check_code,
                candidate_type=(
                    "PROJECTED_IR_REVIEW" if refs else "MISSING_EXPECTED_IR"
                ),
                trigger_reason=(
                    "存在本检查所需IR，须由模型结合Evidence裁决"
                    if refs
                    else "未发现本检查预期IR，须由模型核对是否属于真实缺失"
                ),
                required_ir_types=spec.required_ir_types,
                candidate_ir_refs=refs,
                candidate_evidence_refs=evidence_refs,
            )
        )
    return candidates


def _po_evidence_catalog(
    request: GenericReviewRequest,
    candidates: list[DeterministicRiskCandidate],
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
) -> PoEvidenceCatalog:
    sources_by_id = {item.source_id: item for item in request.evidence_sources}
    absence_by_id = {
        item.source_id: item for item in request.absence_evidence_sources
    }
    source_by_binding = {
        (item.ir_item_id, item.anchor_id): item
        for item in request.evidence_sources
    }
    policies = {
        item.check_code: item for item in request.check_evidence_policies
    }
    anchor_ref_by_id = {
        item.anchor_id: ref for ref, item in anchor_refs.items()
    }
    candidate_sources: dict[
        tuple[str, str],
        tuple[tuple[str, ...], tuple[str, ...]],
    ] = {}
    allowed_by_check: dict[str, set[str]] = {
        item.check_code: set() for item in request.assigned_check_specs
    }
    selected_source_ids: set[str] = set()
    selected_absence_ids: set[str] = set()
    for candidate in candidates:
        policy = policies[candidate.check_code]
        source_ids: list[str] = []
        for ir_ref in candidate.candidate_ir_refs:
            item = ir_refs[ir_ref]
            for anchor in item.source_anchors:
                if anchor_ref_by_id.get(anchor.anchor_id) not in set(
                    candidate.candidate_evidence_refs
                ):
                    continue
                source = source_by_binding.get((item.item_id, anchor.anchor_id))
                if (
                    source is None
                    or source.source_id
                    not in policy.allowed_evidence_source_ids
                ):
                    raise DirectReviewError(
                        "RISK_EVIDENCE_SOURCE_CATALOG_INVALID",
                        "PO candidate cannot resolve to an allowed Evidence Source",
                    )
                source_ids.append(source.source_id)
        absence_ids: list[str] = []
        missing_candidate = (
            candidate.candidate_type.endswith("_ABSENT")
            or (
                not candidate.candidate_ir_refs
                and candidate.candidate_type == "MISSING_EXPECTED_IR"
            )
        )
        if missing_candidate:
            absence_ids = list(policy.allowed_absence_source_ids)
        source_tuple = tuple(dict.fromkeys(source_ids))
        absence_tuple = tuple(dict.fromkeys(absence_ids))
        if not source_tuple and not absence_tuple:
            raise DirectReviewError(
                "RISK_EVIDENCE_SOURCE_CATALOG_INVALID",
                "Every PO candidate must resolve to text or Absence Evidence Source",
            )
        resolved_primary_ids = tuple([*source_tuple, *absence_tuple])
        if request.unit_id == "liability_remedies_exit":
            legal_resolved_ids = set(resolved_primary_ids)
            if any(
                source_id not in legal_resolved_ids
                for source_id in candidate.primary_evidence_source_ids
            ):
                raise DirectReviewError(
                    "RISK_CANDIDATE_PRIMARY_EVIDENCE_INVALID",
                    "LRE candidate Primary Evidence must be a deterministic subset "
                    "of its IR/Absence binding",
                )
            source_tuple = tuple(
                source_id
                for source_id in candidate.primary_evidence_source_ids
                if source_id in sources_by_id
            )
            absence_tuple = tuple(
                source_id
                for source_id in candidate.primary_evidence_source_ids
                if source_id in absence_by_id
            )
        elif resolved_primary_ids != tuple(candidate.primary_evidence_source_ids):
            raise DirectReviewError(
                "RISK_CANDIDATE_PRIMARY_EVIDENCE_INVALID",
                "PO candidate Primary Evidence does not match its deterministic "
                "IR/Absence binding",
            )
        role_source_ids = tuple(
            dict.fromkeys(
                [
                    *candidate.primary_evidence_source_ids,
                    *candidate.allowed_supporting_evidence_source_ids,
                    *candidate.allowed_counter_evidence_source_ids,
                ]
            )
        )
        if any(
            source_id not in sources_by_id and source_id not in absence_by_id
            for source_id in role_source_ids
        ):
            raise DirectReviewError(
                "RISK_CANDIDATE_EVIDENCE_ROLE_INVALID",
                "PO candidate references an unknown role-scoped Evidence Source",
            )
        candidate_sources[
            (candidate.check_code, candidate.candidate_type)
        ] = (source_tuple, absence_tuple)
        allowed_by_check[candidate.check_code].update(role_source_ids)
        selected_source_ids.update(
            source_id for source_id in role_source_ids if source_id in sources_by_id
        )
        selected_absence_ids.update(
            source_id for source_id in role_source_ids if source_id in absence_by_id
        )
    return PoEvidenceCatalog(
        evidence_sources={
            source_id: sources_by_id[source_id]
            for source_id in sorted(selected_source_ids)
        },
        absence_sources={
            source_id: absence_by_id[source_id]
            for source_id in sorted(selected_absence_ids)
        },
        allowed_source_ids_by_check={
            check_code: tuple(sorted(source_ids))
            for check_code, source_ids in sorted(allowed_by_check.items())
        },
        candidate_source_ids=candidate_sources,
    )


_LRE_CONTROL_CODE_TEMPLATES = {
    "TIE_LIABILITY_TO_SPECIFIC_OBLIGATION": "将违约责任与明确义务、过错、因果关系和实际损失逐项对应",
    "LIMIT_LOSS_TO_DIRECT_AND_FORESEEABLE": "将赔偿范围限定为可证明、可预见且与违约直接相关的实际损失",
    "EXCLUDE_INDIRECT_AND_CONSEQUENTIAL_LOSS": "排除间接、附带、惩罚性及利润、商誉、机会等后果性损失",
    "ADD_AGGREGATE_LIABILITY_CAP": "增加双方适用的累计责任总上限，避免责任金额无边界",
    "DEFINE_LIABILITY_CAP_BASE": "明确责任上限以合同总价、已付费用或约定期间费用为计算基数",
    "ALIGN_CAP_EXCEPTIONS": "封闭责任上限例外并确保其他赔偿、退款和违约金条款不得绕过上限",
    "PREVENT_DOUBLE_RECOVERY": "明确违约金、赔偿、退款和其他救济不得就同一损失重复或累计受偿",
    "DEFINE_THIRD_PARTY_CLAIM_PROCEDURE": "明确第三方索赔通知、抗辩控制、和解同意及费用承担程序",
    "ADD_NOTICE_AND_MITIGATION_DUTY": "增加损失通知、证据提供及合理减损义务",
    "ADD_MUTUAL_REMEDIES": "为双方配置对等的违约责任和救济机制",
    "ADD_CURE_PERIOD": "在非重大违约解除或责任升级前增加合理通知和整改期限",
    "LIMIT_TERMINATION_TRIGGERS": "将解除条件限定为客观、重大且可证明的违约或履行障碍",
    "ADD_TERMINATION_SETTLEMENT": "明确终止时已完成工作、已发生费用、预付款和应付款项的结算规则",
    "DEFINE_POST_TERMINATION_OBLIGATIONS": "明确终止后的返还、删除、保密、知识产权和责任存续边界",
    "ADD_TRANSITION_ASSISTANCE": "明确终止后的交接、迁移和过渡协助范围、期限及费用",
    "REMOVE_AUTOMATIC_RENEWAL": "删除默认或自动续期，续期须经双方书面确认",
    "ADD_RENEWAL_NOTICE": "增加续期或不续期的合理提前通知机制",
    "ADD_REASONABLE_EXIT_WINDOW": "设置足以评估和退出续期的合理通知窗口",
    "CLARIFY_FORCE_MAJEURE": "明确不可抗力定义并排除一般商业困难和单纯成本上涨",
    "DEFINE_FORCE_MAJEURE_NOTICE": "明确不可抗力通知、证明、减损、持续期间及费用和进度后果",
    "DEFINE_INSURANCE_REQUIREMENTS": "明确保险险种、保额、期限、被保险人及证明义务",
    "CHOOSE_SINGLE_DISPUTE_FORUM": "选择单一、明确且可执行的诉讼或仲裁路径",
    "CLARIFY_GOVERNING_LAW": "明确合同适用法律并消除冲突表述",
    "SELECT_REASONABLE_FORUM": "选择与主体或履约地有合理联系且不过度增加我方维权成本的管辖地",
    "ADD_DISPUTE_ESCALATION_DEADLINE": "为协商等前置程序设置明确期限，避免阻碍及时救济",
}

_LRE_ALLOWED_CONTROL_CODES: dict[str, tuple[str, ...]] = {
    "BROAD_BREACH_TRIGGER_REVIEW": (
        "TIE_LIABILITY_TO_SPECIFIC_OBLIGATION",
        "ADD_NOTICE_AND_MITIGATION_DUTY",
        "ADD_CURE_PERIOD",
    ),
    "OVERBROAD_LOSS_SCOPE_REVIEW": (
        "LIMIT_LOSS_TO_DIRECT_AND_FORESEEABLE",
        "EXCLUDE_INDIRECT_AND_CONSEQUENTIAL_LOSS",
        "ADD_AGGREGATE_LIABILITY_CAP",
        "ADD_NOTICE_AND_MITIGATION_DUTY",
    ),
    "CUMULATIVE_REMEDIES_REVIEW": (
        "PREVENT_DOUBLE_RECOVERY",
        "ALIGN_CAP_EXCEPTIONS",
    ),
    "LIABILITY_CAP_ABSENT": (
        "ADD_AGGREGATE_LIABILITY_CAP",
        "DEFINE_LIABILITY_CAP_BASE",
        "ALIGN_CAP_EXCEPTIONS",
    ),
    "LIABILITY_CAP_BYPASS_REVIEW": (
        "ALIGN_CAP_EXCEPTIONS",
        "PREVENT_DOUBLE_RECOVERY",
    ),
    "OVERBROAD_INDEMNITY_REVIEW": (
        "LIMIT_LOSS_TO_DIRECT_AND_FORESEEABLE",
        "EXCLUDE_INDIRECT_AND_CONSEQUENTIAL_LOSS",
        "DEFINE_THIRD_PARTY_CLAIM_PROCEDURE",
        "ADD_NOTICE_AND_MITIGATION_DUTY",
    ),
    "TERMINATION_RIGHTS_REVIEW": (
        "ADD_MUTUAL_REMEDIES",
        "ADD_CURE_PERIOD",
        "LIMIT_TERMINATION_TRIGGERS",
    ),
    "TERMINATION_SETTLEMENT_REVIEW": (
        "ADD_TERMINATION_SETTLEMENT",
        "DEFINE_POST_TERMINATION_OBLIGATIONS",
        "ADD_TRANSITION_ASSISTANCE",
    ),
    "TERMINATION_SETTLEMENT_ABSENT": (
        "ADD_TERMINATION_SETTLEMENT",
        "DEFINE_POST_TERMINATION_OBLIGATIONS",
        "ADD_TRANSITION_ASSISTANCE",
    ),
    "FORCE_MAJEURE_MECHANISM_ABSENT": (
        "CLARIFY_FORCE_MAJEURE",
        "DEFINE_FORCE_MAJEURE_NOTICE",
    ),
    "DISPUTE_RESOLUTION_ABSENT": (
        "CHOOSE_SINGLE_DISPUTE_FORUM",
        "CLARIFY_GOVERNING_LAW",
        "SELECT_REASONABLE_FORUM",
        "ADD_DISPUTE_ESCALATION_DEADLINE",
    ),
}

_LRE_FINDING_TEMPLATES: dict[str, dict[str, str]] = {
    "BROAD_BREACH_TRIGGER_REVIEW": {
        "title": "违约责任触发和责任边界不够明确",
        "issue": "合同责任触发安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能因宽泛触发条件承担与实际违约程度不相称的责任；风险因素为：{severity}。",
    },
    "OVERBROAD_LOSS_SCOPE_REVIEW": {
        "title": "损失赔偿范围过宽且缺少可预见性边界",
        "issue": "合同损失范围存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能承担间接、预期利益及开放费用等难以控制的赔偿；风险因素为：{severity}。",
    },
    "CUMULATIVE_REMEDIES_REVIEW": {
        "title": "违约金与赔偿等责任可能重复累计",
        "issue": "合同责任累计安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能就同一损失同时承担多种责任；风险因素为：{severity}。",
    },
    "LIABILITY_CAP_ABSENT": {
        "title": "合同缺少责任总上限",
        "issue": "合同文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}的累计责任金额缺少可预测上限；风险因素为：{severity}。",
    },
    "LIABILITY_CAP_BYPASS_REVIEW": {
        "title": "责任上限可能被其他责任条款绕过",
        "issue": "合同责任上限与其他责任安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}表面受限的责任仍可能因例外或累计条款失去上限保护；风险因素为：{severity}。",
    },
    "OVERBROAD_INDEMNITY_REVIEW": {
        "title": "赔偿和第三方责任范围过宽",
        "issue": "合同赔偿安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能承担缺少过错、因果、范围或程序限制的赔偿责任；风险因素为：{severity}。",
    },
    "TERMINATION_RIGHTS_REVIEW": {
        "title": "解除权和整改程序存在失衡",
        "issue": "合同解除安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能因单方或宽泛解除条件失去合理补救机会；风险因素为：{severity}。",
    },
    "TERMINATION_SETTLEMENT_REVIEW": {
        "title": "终止后的结算和退出义务不完整",
        "issue": "合同终止处理存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能在终止时面对结算、返还或持续责任不确定性；风险因素为：{severity}。",
    },
    "TERMINATION_SETTLEMENT_ABSENT": {
        "title": "合同缺少终止结算和退出机制",
        "issue": "合同文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}可能在终止时面对结算、返还或持续责任不确定性；风险因素为：{severity}。",
    },
    "FORCE_MAJEURE_MECHANISM_ABSENT": {
        "title": "合同缺少完整不可抗力机制",
        "issue": "合同文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}遇到不可控事件时的通知、减损、费用、进度和退出安排不明确；风险因素为：{severity}。",
    },
    "DISPUTE_RESOLUTION_ABSENT": {
        "title": "合同缺少明确争议解决和管辖机制",
        "issue": "合同文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}发生争议时可能面临程序选择、管辖和维权成本不确定性；风险因素为：{severity}。",
    },
}

_LRE_CANDIDATE_POLICIES: dict[str, dict[str, object]] = {
    "BROAD_BREACH_TRIGGER_REVIEW": {
        "strength": "SEMANTIC_REVIEW",
        "severity_rule_id": "LRE_BREACH_TRIGGER_V1",
        "trigger_conditions": ["重大违约或责任触发缺少客观、具体且成比例的条件"],
        "mitigating_conditions": ["触发条件明确且责任与违约程度对应"],
    },
    "OVERBROAD_LOSS_SCOPE_REVIEW": {
        "strength": "HARD_RULE",
        "severity_rule_id": "LRE_LOSS_SCOPE_V1",
        "trigger_conditions": ["赔偿明确覆盖间接损失、可预期利益或开放费用范围"],
        "mitigating_conditions": ["赔偿限于直接、可预见和已证明的实际损失"],
    },
    "CUMULATIVE_REMEDIES_REVIEW": {
        "strength": "HARD_RULE",
        "severity_rule_id": "LRE_CUMULATIVE_REMEDIES_V1",
        "trigger_conditions": ["违约金、赔偿或其他责任可同时或累计主张"],
        "mitigating_conditions": ["合同明确禁止重复受偿并规定抵扣顺序"],
    },
    "LIABILITY_CAP_ABSENT": {
        "strength": "HARD_RULE",
        "severity_rule_id": "LRE_LIABILITY_CAP_ABSENT_V1",
        "trigger_conditions": ["存在开放赔偿责任且未定位到适用的累计责任总上限"],
        "mitigating_conditions": ["合同存在清晰、双向适用且未被架空的责任总上限"],
    },
    "LIABILITY_CAP_BYPASS_REVIEW": {
        "strength": "STRONG_SIGNAL",
        "severity_rule_id": "LRE_LIABILITY_CAP_BYPASS_V1",
        "trigger_conditions": ["责任上限与不受限制的赔偿、违约金或退款条款适用范围冲突"],
        "mitigating_conditions": ["上限例外封闭且所有责任均明确纳入累计上限"],
    },
    "OVERBROAD_INDEMNITY_REVIEW": {
        "strength": "HARD_RULE",
        "severity_rule_id": "LRE_INDEMNITY_V1",
        "trigger_conditions": ["赔偿覆盖开放损失或第三方责任且缺少过错、因果或程序边界"],
        "mitigating_conditions": ["赔偿对象、范围、通知、抗辩和和解程序均明确"],
    },
    "TERMINATION_RIGHTS_REVIEW": {
        "strength": "SEMANTIC_REVIEW",
        "severity_rule_id": "LRE_TERMINATION_RIGHTS_V1",
        "trigger_conditions": ["相对方解除条件明显更宽或我方缺少整改和对等救济"],
        "mitigating_conditions": ["双方解除权、客观条件和整改期限总体对等且可执行"],
    },
    "TERMINATION_SETTLEMENT_REVIEW": {
        "strength": "SEMANTIC_REVIEW",
        "severity_rule_id": "LRE_TERMINATION_SETTLEMENT_V1",
        "trigger_conditions": ["终止条款明示排除结算、返还或设置不合理的持续责任"],
        "mitigating_conditions": ["终止后的不利安排具有合理边界并配置对等结算和返还"],
    },
    "TERMINATION_SETTLEMENT_ABSENT": {
        "strength": "HARD_RULE",
        "severity_rule_id": "LRE_TERMINATION_SETTLEMENT_V1",
        "trigger_conditions": ["持续履行或已发生成本且缺少终止结算、返还或退出义务机制"],
        "mitigating_conditions": ["结算、返还、持续责任和交接安排完整"],
    },
    "FORCE_MAJEURE_MECHANISM_ABSENT": {
        "strength": "HARD_RULE",
        "severity_rule_id": "LRE_FORCE_MAJEURE_ABSENT_V1",
        "trigger_conditions": ["持续履行合同未定位到不可抗力及其通知、减损和后果机制"],
        "mitigating_conditions": ["合同存在完整不可抗力和风险分配安排"],
    },
    "DISPUTE_RESOLUTION_ABSENT": {
        "strength": "HARD_RULE",
        "severity_rule_id": "LRE_DISPUTE_ABSENT_V1",
        "trigger_conditions": ["未定位到适用法律及单一明确的争议解决和管辖机制"],
        "mitigating_conditions": ["适用法律、争议路径和管辖机构完整且无冲突"],
    },
}

_LRE_CANONICAL_ROOT_POLICIES: dict[str, dict[str, object]] = {
    candidate_type: {
        "canonical_root_type": candidate_type,
        "root_severity_rule_id": policy["severity_rule_id"],
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": (),
    }
    for candidate_type, policy in _LRE_CANDIDATE_POLICIES.items()
}
_LRE_CANONICAL_ROOT_POLICIES["OVERBROAD_LOSS_SCOPE_REVIEW"].update(
    canonical_root_type="UNBOUNDED_LIABILITY_EXPOSURE",
    root_severity_rule_id="LRE_UNBOUNDED_LIABILITY_ROOT_V1",
    merge_group_id="LRE_UNBOUNDED_LIABILITY",
    merge_compatible_candidate_types=(
        "BROAD_BREACH_TRIGGER_REVIEW",
        "LIABILITY_CAP_ABSENT",
        "OVERBROAD_INDEMNITY_REVIEW",
    ),
    deterministic_severity_factors=("INDIRECT_LOSS_EXPOSURE",),
)
_LRE_CANONICAL_ROOT_POLICIES["LIABILITY_CAP_ABSENT"].update(
    canonical_root_type="UNBOUNDED_LIABILITY_EXPOSURE",
    root_severity_rule_id="LRE_UNBOUNDED_LIABILITY_ROOT_V1",
    merge_group_id="LRE_UNBOUNDED_LIABILITY",
    merge_compatible_candidate_types=(
        "BROAD_BREACH_TRIGGER_REVIEW",
        "OVERBROAD_LOSS_SCOPE_REVIEW",
        "OVERBROAD_INDEMNITY_REVIEW",
    ),
    deterministic_severity_factors=("UNLIMITED_LIABILITY", "MISSING_CORE_MECHANISM"),
)
_LRE_CANONICAL_ROOT_POLICIES["OVERBROAD_INDEMNITY_REVIEW"].update(
    canonical_root_type="UNBOUNDED_LIABILITY_EXPOSURE",
    root_severity_rule_id="LRE_UNBOUNDED_LIABILITY_ROOT_V1",
    merge_group_id="LRE_UNBOUNDED_LIABILITY",
    merge_compatible_candidate_types=(
        "BROAD_BREACH_TRIGGER_REVIEW",
        "OVERBROAD_LOSS_SCOPE_REVIEW",
        "LIABILITY_CAP_ABSENT",
    ),
    deterministic_severity_factors=("OVERBROAD_INDEMNITY",),
)
_LRE_CANONICAL_ROOT_POLICIES["CUMULATIVE_REMEDIES_REVIEW"][
    "deterministic_severity_factors"
] = ("CUMULATIVE_REMEDIES",)
_LRE_CANONICAL_ROOT_POLICIES["FORCE_MAJEURE_MECHANISM_ABSENT"][
    "deterministic_severity_factors"
] = ("MISSING_CORE_MECHANISM", "NO_EFFECTIVE_REMEDY")
_LRE_CANONICAL_ROOT_POLICIES["DISPUTE_RESOLUTION_ABSENT"][
    "deterministic_severity_factors"
] = ("MISSING_CORE_MECHANISM", "NO_EFFECTIVE_REMEDY")
_LRE_CANONICAL_ROOT_POLICIES["TERMINATION_SETTLEMENT_ABSENT"].update(
    canonical_root_type="TERMINATION_SETTLEMENT_REVIEW",
    root_severity_rule_id="LRE_TERMINATION_SETTLEMENT_V1",
    deterministic_severity_factors=(
        "NO_TERMINATION_SETTLEMENT",
        "MISSING_CORE_MECHANISM",
    ),
)


_ICD_CONTROL_CODE_TEMPLATES = {
    "DEFINE_FOREGROUND_IP_OWNERSHIP": "明确项目成果、新增作品、软件和文档的知识产权归属及双方使用边界",
    "PRESERVE_BACKGROUND_IP": "明确双方背景知识产权继续归原权利人所有，不因本合同发生默示转让",
    "LIMIT_IP_LICENSE_SCOPE": "将知识产权许可限定于合同目的、期限、地域和必要使用范围，并明确转授权和终止后处理",
    "ADD_THIRD_PARTY_IP_WARRANTY": "增加第三方知识产权权利保证和不侵权承诺",
    "ADD_IP_INFRINGEMENT_REMEDIES": "明确第三方索赔通知、抗辩控制、替换修改、取得许可和赔偿救济",
    "DEFINE_CONFIDENTIAL_INFORMATION": "明确保密信息范围、识别方式和不属于保密信息的例外",
    "ADD_CONFIDENTIALITY_EXCEPTIONS": "补充已公开、合法在先持有、独立开发及依法披露等保密例外和披露程序",
    "DEFINE_CONFIDENTIALITY_TERM": "明确保密义务期限、终止后持续期间及允许披露对象",
    "LIMIT_DATA_PROCESSING_PURPOSE": "将数据处理限定于合同目的、必要范围、授权人员和约定期限",
    "ADD_DATA_SECURITY_STANDARD": "明确访问控制、最小权限、传输存储保护和可审计的数据安全标准",
    "ADD_SECURITY_INCIDENT_NOTICE": "明确安全事件发现后的通知时限、处置协作、减损和责任承担",
    "ADD_DATA_RETURN_DELETION": "明确合同终止或服务结束后的数据及载体返还、删除、销毁和书面确认",
    "LIMIT_DATA_RETENTION": "限制备份和留存范围、期限及法定留存例外，禁止继续用于合同外目的",
    "RESERVE_BACKGROUND_IP": "明确双方背景知识产权、既有技术、模板、方法和工具仍归原权利人所有",
    "SEPARATE_BACKGROUND_AND_FOREGROUND_IP": "分别定义背景知识产权和项目新成果，避免交付或许可导致权利范围混同",
    "LIMIT_LICENSE_TO_CONTRACT_PURPOSE": "将许可用途严格限定为履行和使用本合同项目成果所必需的范围",
    "DEFINE_LICENSE_TERM_AND_SCOPE": "明确许可期限、地域、对象、使用方式及合同终止后的权利边界",
    "RESTRICT_SUBLICENSING": "未经权利人书面同意不得转许可、再许可或允许无关第三方使用",
    "DEFINE_POST_TERMINATION_USE": "明确合同终止后继续使用、停止使用、卸载、返还和销毁的具体规则",
    "RESTRICT_THIRD_PARTY_DISCLOSURE": "将披露对象限定于确有必要知悉且承担不低于本合同保密义务的人员",
    "ADD_RETURN_AND_DESTRUCTION": "约定合同终止或要求时返还、销毁保密资料及其复制件并书面确认",
    "DEFINE_DATA_OWNERSHIP": "明确业务数据、客户数据、输入数据和输出数据的权属及控制边界",
    "RESTRICT_DATA_SHARING": "限制向关联方、分包商、服务商及其他第三方共享或开放数据访问",
    "ADD_DATA_EXPORT_AND_RETURN": "明确数据导出格式、期限、完整性标准及服务结束后的返还流程",
    "ADD_DATA_DELETION": "明确主数据、缓存和备份的删除期限、验证方式及书面删除证明",
    "FLOW_DOWN_SECURITY_OBLIGATIONS": "要求第三方处理者承担不低于本合同的数据安全和保密义务",
    "DEFINE_SECURITY_EXIT_PROCEDURE": "明确终止后的账号停用、接口关闭、权限撤销、介质回收和安全确认",
}

_ICD_ALLOWED_CONTROL_CODES: dict[str, tuple[str, ...]] = {
    "FOREGROUND_IP_OWNERSHIP_REVIEW": (
        "DEFINE_FOREGROUND_IP_OWNERSHIP",
        "SEPARATE_BACKGROUND_AND_FOREGROUND_IP",
        "LIMIT_IP_LICENSE_SCOPE",
    ),
    "FOREGROUND_IP_OWNERSHIP_ABSENT": (
        "DEFINE_FOREGROUND_IP_OWNERSHIP",
        "SEPARATE_BACKGROUND_AND_FOREGROUND_IP",
    ),
    "BACKGROUND_IP_LICENSE_REVIEW": (
        "PRESERVE_BACKGROUND_IP",
        "RESERVE_BACKGROUND_IP",
        "SEPARATE_BACKGROUND_AND_FOREGROUND_IP",
        "LIMIT_IP_LICENSE_SCOPE",
        "LIMIT_LICENSE_TO_CONTRACT_PURPOSE",
        "DEFINE_LICENSE_TERM_AND_SCOPE",
        "RESTRICT_SUBLICENSING",
        "DEFINE_POST_TERMINATION_USE",
    ),
    "BACKGROUND_IP_LICENSE_ABSENT": (
        "PRESERVE_BACKGROUND_IP",
        "RESERVE_BACKGROUND_IP",
        "SEPARATE_BACKGROUND_AND_FOREGROUND_IP",
        "LIMIT_IP_LICENSE_SCOPE",
        "LIMIT_LICENSE_TO_CONTRACT_PURPOSE",
        "DEFINE_LICENSE_TERM_AND_SCOPE",
        "RESTRICT_SUBLICENSING",
        "DEFINE_POST_TERMINATION_USE",
    ),
    "THIRD_PARTY_IP_PROTECTION_REVIEW": (
        "ADD_THIRD_PARTY_IP_WARRANTY",
        "ADD_IP_INFRINGEMENT_REMEDIES",
    ),
    "THIRD_PARTY_IP_PROTECTION_ABSENT": (
        "ADD_THIRD_PARTY_IP_WARRANTY",
        "ADD_IP_INFRINGEMENT_REMEDIES",
    ),
    "CONFIDENTIALITY_PROTECTION_REVIEW": (
        "DEFINE_CONFIDENTIAL_INFORMATION",
        "ADD_CONFIDENTIALITY_EXCEPTIONS",
        "DEFINE_CONFIDENTIALITY_TERM",
        "RESTRICT_THIRD_PARTY_DISCLOSURE",
        "ADD_RETURN_AND_DESTRUCTION",
    ),
    "CONFIDENTIALITY_COMPLETENESS_ABSENT": (
        "DEFINE_CONFIDENTIAL_INFORMATION",
        "ADD_CONFIDENTIALITY_EXCEPTIONS",
        "DEFINE_CONFIDENTIALITY_TERM",
        "RESTRICT_THIRD_PARTY_DISCLOSURE",
        "ADD_RETURN_AND_DESTRUCTION",
    ),
    "DATA_PROCESSING_SECURITY_REVIEW": (
        "LIMIT_DATA_PROCESSING_PURPOSE",
        "DEFINE_DATA_OWNERSHIP",
        "RESTRICT_DATA_SHARING",
        "ADD_DATA_SECURITY_STANDARD",
        "ADD_SECURITY_INCIDENT_NOTICE",
        "FLOW_DOWN_SECURITY_OBLIGATIONS",
    ),
    "DATA_PROCESSING_SECURITY_ABSENT": (
        "LIMIT_DATA_PROCESSING_PURPOSE",
        "DEFINE_DATA_OWNERSHIP",
        "RESTRICT_DATA_SHARING",
        "ADD_DATA_SECURITY_STANDARD",
        "ADD_SECURITY_INCIDENT_NOTICE",
        "FLOW_DOWN_SECURITY_OBLIGATIONS",
    ),
    "DATA_RETURN_DELETION_REVIEW": (
        "ADD_DATA_RETURN_DELETION",
        "DEFINE_DATA_OWNERSHIP",
        "ADD_DATA_EXPORT_AND_RETURN",
        "ADD_DATA_DELETION",
        "LIMIT_DATA_RETENTION",
        "DEFINE_SECURITY_EXIT_PROCEDURE",
    ),
    "DATA_RETURN_DELETION_ABSENT": (
        "ADD_DATA_RETURN_DELETION",
        "DEFINE_DATA_OWNERSHIP",
        "ADD_DATA_EXPORT_AND_RETURN",
        "ADD_DATA_DELETION",
        "LIMIT_DATA_RETENTION",
        "DEFINE_SECURITY_EXIT_PROCEDURE",
    ),
}

_ICD_FINDING_TEMPLATES: dict[str, dict[str, str]] = {
    "FOREGROUND_IP_OWNERSHIP_REVIEW": {
        "title": "项目成果知识产权归属或使用边界存在风险",
        "issue": "项目成果安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能无法稳定取得或保留履约所需的成果权利；风险因素为：{severity}。",
    },
    "FOREGROUND_IP_OWNERSHIP_ABSENT": {
        "title": "合同缺少项目成果知识产权归属机制",
        "issue": "合同技术文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}对项目成果的取得、使用、修改和后续利用可能缺少明确权利基础；风险因素为：{severity}。",
    },
    "BACKGROUND_IP_LICENSE_REVIEW": {
        "title": "背景知识产权许可边界存在风险",
        "issue": "背景知识产权或许可安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能承担超范围转让或缺少必要使用权的风险；风险因素为：{severity}。",
    },
    "BACKGROUND_IP_LICENSE_ABSENT": {
        "title": "合同缺少背景知识产权和许可边界",
        "issue": "合同技术文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}既有技术与项目使用权可能发生混同或授权不足；风险因素为：{severity}。",
    },
    "THIRD_PARTY_IP_PROTECTION_REVIEW": {
        "title": "第三方知识产权保证和侵权救济存在风险",
        "issue": "第三方权利安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能因第三方索赔承担停用、替换、抗辩或赔偿暴露；风险因素为：{severity}。",
    },
    "THIRD_PARTY_IP_PROTECTION_ABSENT": {
        "title": "合同缺少第三方知识产权保证和侵权救济",
        "issue": "合同技术文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}面对第三方权利主张时可能缺少替换、修改、许可和赔偿保障；风险因素为：{severity}。",
    },
    "CONFIDENTIALITY_PROTECTION_REVIEW": {
        "title": "保密保护范围或执行机制存在风险",
        "issue": "保密安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}的商业秘密和履约信息可能因范围、期限或披露程序不清而保护不足；风险因素为：{severity}。",
    },
    "CONFIDENTIALITY_COMPLETENESS_ABSENT": {
        "title": "保密条款缺少完整保护机制",
        "issue": "合同技术文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}可能难以约束保密例外、允许披露对象、法定披露和终止后持续义务；风险因素为：{severity}。",
    },
    "DATA_PROCESSING_SECURITY_REVIEW": {
        "title": "数据处理目的或安全控制存在风险",
        "issue": "数据处理或安全安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能因处理范围、访问权限、安全标准或事件责任不清承担暴露；风险因素为：{severity}。",
    },
    "DATA_PROCESSING_SECURITY_ABSENT": {
        "title": "合同缺少数据处理和安全机制",
        "issue": "合同技术文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}可能无法控制数据用途、访问范围、安全措施和事件通知；风险因素为：{severity}。",
    },
    "DATA_RETURN_DELETION_REVIEW": {
        "title": "数据返还、删除或留存安排存在风险",
        "issue": "数据生命周期安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能在合同结束后无法收回或限制继续留存使用相关数据；风险因素为：{severity}。",
    },
    "DATA_RETURN_DELETION_ABSENT": {
        "title": "合同缺少数据返还、删除和留存闭环",
        "issue": "合同技术文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}可能无法确认数据及载体已返还、删除或仅按法定期限留存；风险因素为：{severity}。",
    },
}

_ICD_CANDIDATE_POLICIES: dict[str, dict[str, object]] = {
    candidate_type: {
        "strength": (
            "SEMANTIC_REVIEW"
            if not candidate_type.endswith("_ABSENT")
            else "SEMANTIC_REVIEW"
        ),
        "severity_rule_id": f"ICD_{candidate_type}_V1",
        "trigger_conditions": [
            "当前Candidate的文本或合法Absence Source满足该ICD检查的成立条件",
            "该安排对our_party造成知识产权、保密或数据方面的实质暴露",
        ],
        "mitigating_conditions": [
            "当前Check的其他合法Source已经提供完整、可执行且不损害our_party的保护机制",
            "该合同类型或履行内容经Candidate Evidence证明不适用本机制",
        ],
    }
    for candidate_type in _ICD_ALLOWED_CONTROL_CODES
}

_ICD_CANONICAL_ROOT_POLICIES: dict[str, dict[str, object]] = {
    candidate_type: {
        "canonical_root_type": candidate_type,
        "root_severity_rule_id": f"ICD_{candidate_type}_ROOT_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": (
            ("MISSING_CORE_MECHANISM",)
            if candidate_type.endswith("_ABSENT")
            else ()
        ),
    }
    for candidate_type in _ICD_ALLOWED_CONTROL_CODES
}
_ICD_CANONICAL_ROOT_POLICIES["FOREGROUND_IP_OWNERSHIP_ABSENT"][
    "deterministic_severity_factors"
] = ("MISSING_CORE_MECHANISM", "OWNERSHIP_AMBIGUITY")
_ICD_CANONICAL_ROOT_POLICIES["THIRD_PARTY_IP_PROTECTION_ABSENT"][
    "deterministic_severity_factors"
] = ("MISSING_CORE_MECHANISM", "THIRD_PARTY_EXPOSURE")
_ICD_CANONICAL_ROOT_POLICIES["DATA_PROCESSING_SECURITY_ABSENT"][
    "deterministic_severity_factors"
] = (
    "MISSING_CORE_MECHANISM",
    "NO_SECURITY_STANDARD",
    "MISSING_INCIDENT_NOTICE",
)
_ICD_CANONICAL_ROOT_POLICIES["DATA_RETURN_DELETION_ABSENT"][
    "deterministic_severity_factors"
] = ("MISSING_CORE_MECHANISM", "NO_RETURN_OR_DELETION")


_PO_PATTERN_RULES: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "PO-001": (
        (
            "SCOPE_EXPANSION",
            "存在开放式、单方或未封闭的服务/交付范围表述",
            (
                r"按.{0,10}要求",
                r"提出.{0,8}要求",
                r"其[他它]要求",
                r"包括但不限于",
                r"随时.{0,8}(增加|扩大|调整)",
                r"单方.{0,8}(增加|扩大|调整)",
                r"尽量满足",
            ),
        ),
        (
            "DELIVERY_SCHEDULE_REVIEW",
            "存在交付、完成或进度表述，需要核对内容、期限和责任边界",
            (r"交付", r"完成.{0,8}(项目|任务|服务|工作)", r"进度", r"工期"),
        ),
    ),
    "PO-002": (
        (
            "RIGHTS_OBLIGATIONS_IMBALANCE",
            "存在单方审批、检查、解释、拒绝或控制性权利，需要核对对等保护",
            (
                r"单方",
                r"自行",
                r"随时",
                r"有权.{0,12}(检查|决定|拒绝|暂停|要求)",
                r"最终解释",
                r"须按.{0,8}要求",
            ),
        ),
    ),
    "PO-003": (
        (
            "COOPERATION_DEPENDENCY",
            "存在配合、资料、接口、前置条件或延迟依赖，需要核对责任和顺延",
            (r"配合", r"协助", r"提供.{0,8}(资料|材料|接口|条件)", r"前置", r"依赖", r"延迟"),
        ),
    ),
    "PO-004": (
        (
            "QUALITY_STANDARD_UNMEASURABLE",
            "存在可能不可衡量的质量或服务标准，需要核对客观指标",
            (
                r"尽量",
                r"满足.{0,8}要求",
                r"达到.{0,8}目的",
                r"适当",
                r"专业.{0,6}水准",
                r"行业.{0,6}(规范|标准)",
            ),
        ),
        (
            "ACCEPTANCE_MECHANISM_REVIEW",
            "存在验收或确认表述，需要核对标准、期限、程序、整改和复验",
            (r"验收", r"视为.{0,6}(验收|合格)", r"确认.{0,8}(合格|完成)", r"复验"),
        ),
    ),
    "PO-005": (
        (
            "ASSIGNMENT_SUBCONTRACT_REVIEW",
            "存在第三方、委托、分包或转让表述，需要核对同意和持续责任",
            (r"转委托", r"分包", r"转让", r"第三方", r"委托", r"债权", r"债务"),
        ),
    ),
    "PO-006": (
        (
            "CHANGE_CONTROL_REVIEW",
            "存在需求、范围、工期或价款调整线索，需要核对书面变更和联动机制",
            (
                r"变更",
                r"调整",
                r"新增",
                r"额外",
                r"提出.{0,8}要求",
                r"其[他它]要求",
                r"按.{0,8}要求",
                r"书面确认",
            ),
        ),
    ),
    "PO-007": (
        (
            "WARRANTY_SUPPORT_REVIEW",
            "存在质保、维护、整改、修复、响应或支持线索，需要核对范围、期限和复验",
            (r"质保", r"维护", r"维修", r"整改", r"修复", r"支持", r"响应", r"解决方案", r"复验"),
        ),
    ),
}

_PO_CONTROL_CODE_TEMPLATES = {
    "DEFINE_SCOPE_BOUNDARY": "明确服务范围、交付边界及不包含事项，超出范围的工作须另行确认",
    "ADD_WRITTEN_CHANGE_PROCEDURE": "约定任何需求、范围或履行方式变更均须由双方书面确认后生效",
    "LINK_CHANGE_TO_FEE_AND_SCHEDULE": (
        "将变更与费用、工期及资源调整联动，避免由{our_party}单独承担新增成本"
    ),
    "DEFINE_DELIVERY_DEADLINE": "明确交付物、交付节点、完成期限及延迟责任",
    "DEFINE_MEASURABLE_STANDARD": "补充客观、可量化并可复核的服务或质量标准",
    "ADD_FORMAL_ACCEPTANCE_PROCEDURE": "增加正式验收申请、核验、反馈和确认程序",
    "DEFINE_ACCEPTANCE_PERIOD": "明确验收期限以及逾期未反馈的法律后果",
    "ADD_RECTIFICATION_AND_RETEST": "明确不合格后的整改期限、复验程序及整改仍不合格的处理",
    "ADD_COUNTERPARTY_COOPERATION_DUTY": (
        "明确{counterparty}提供资料、接口、设备和必要配合的内容及时限"
    ),
    "EXTEND_SCHEDULE_FOR_COUNTERPARTY_DELAY": (
        "约定因{counterparty}迟延配合导致的工期顺延和责任排除"
    ),
    "LIMIT_UNILATERAL_CONTROL": (
        "限制{counterparty}单方决定、检查、拒绝或扩大要求的范围，并设置客观条件"
    ),
    "ADD_NOTICE_REQUIREMENT": "增加合理的书面通知期限、送达方式和补救期间",
    "ADD_CONFIDENTIALITY_GUARD": "对履行或第三方参与过程中接触的信息设置必要的保密保护",
}

_PO_ALLOWED_CONTROL_CODES: dict[str, tuple[str, ...]] = {
    "SCOPE_EXPANSION": (
        "DEFINE_SCOPE_BOUNDARY",
        "ADD_WRITTEN_CHANGE_PROCEDURE",
        "LINK_CHANGE_TO_FEE_AND_SCHEDULE",
    ),
    "DELIVERY_SCHEDULE_REVIEW": (
        "DEFINE_DELIVERY_DEADLINE",
        "ADD_NOTICE_REQUIREMENT",
    ),
    "RIGHTS_OBLIGATIONS_IMBALANCE": (
        "LIMIT_UNILATERAL_CONTROL",
        "ADD_NOTICE_REQUIREMENT",
    ),
    "COOPERATION_DEPENDENCY": (
        "ADD_COUNTERPARTY_COOPERATION_DUTY",
        "EXTEND_SCHEDULE_FOR_COUNTERPARTY_DELAY",
    ),
    "COOPERATION_OBLIGATION_ABSENT": (
        "ADD_COUNTERPARTY_COOPERATION_DUTY",
        "EXTEND_SCHEDULE_FOR_COUNTERPARTY_DELAY",
    ),
    "QUALITY_STANDARD_UNMEASURABLE": (
        "DEFINE_MEASURABLE_STANDARD",
        "ADD_FORMAL_ACCEPTANCE_PROCEDURE",
    ),
    "ACCEPTANCE_MECHANISM_REVIEW": (
        "DEFINE_MEASURABLE_STANDARD",
        "ADD_FORMAL_ACCEPTANCE_PROCEDURE",
        "DEFINE_ACCEPTANCE_PERIOD",
        "ADD_RECTIFICATION_AND_RETEST",
    ),
    "ACCEPTANCE_MECHANISM_ABSENT": (
        "DEFINE_MEASURABLE_STANDARD",
        "ADD_FORMAL_ACCEPTANCE_PROCEDURE",
        "DEFINE_ACCEPTANCE_PERIOD",
        "ADD_RECTIFICATION_AND_RETEST",
    ),
    "ASSIGNMENT_SUBCONTRACT_REVIEW": (
        "ADD_NOTICE_REQUIREMENT",
        "ADD_CONFIDENTIALITY_GUARD",
    ),
    "CHANGE_CONTROL_REVIEW": (
        "ADD_WRITTEN_CHANGE_PROCEDURE",
        "LINK_CHANGE_TO_FEE_AND_SCHEDULE",
        "LIMIT_UNILATERAL_CONTROL",
    ),
    "CHANGE_CONTROL_ABSENT": (
        "ADD_WRITTEN_CHANGE_PROCEDURE",
        "LINK_CHANGE_TO_FEE_AND_SCHEDULE",
    ),
    "WARRANTY_SUPPORT_REVIEW": (
        "DEFINE_MEASURABLE_STANDARD",
        "ADD_RECTIFICATION_AND_RETEST",
        "ADD_NOTICE_REQUIREMENT",
    ),
    "WARRANTY_SUPPORT_ABSENT": (
        "DEFINE_MEASURABLE_STANDARD",
        "ADD_RECTIFICATION_AND_RETEST",
        "ADD_NOTICE_REQUIREMENT",
    ),
    "PROJECTED_IR_REVIEW": tuple(_PO_CONTROL_CODE_TEMPLATES),
    "MISSING_EXPECTED_IR": tuple(_PO_CONTROL_CODE_TEMPLATES),
}

_PO_FINDING_TEMPLATES: dict[str, dict[str, str]] = {
    "SCOPE_EXPANSION": {
        "title": "履行范围存在开放式扩张风险",
        "issue": "合同履行安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "履行范围可能在缺少同步确认和对价调整的情况下扩张，进而影响{our_party}的履约权益、成本或交易预期；风险因素为：{severity}。",
    },
    "DELIVERY_SCHEDULE_REVIEW": {
        "title": "交付期限或责任边界不够明确",
        "issue": "合同交付安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能因交付节点或延迟归责不清承担履约争议；风险因素为：{severity}。",
    },
    "RIGHTS_OBLIGATIONS_IMBALANCE": {
        "title": "权利义务和单方控制安排不平衡",
        "issue": "合同权利义务安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "单方控制权的主体、条件或救济边界不清，可能使{our_party}缺少对等程序保障；风险因素为：{severity}。",
    },
    "COOPERATION_DEPENDENCY": {
        "title": "履约依赖与配合责任边界不清",
        "issue": "合同配合安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{counterparty}配合不足可能影响{our_party}履行，但工期和责任未得到充分保护；风险因素为：{severity}。",
    },
    "COOPERATION_OBLIGATION_ABSENT": {
        "title": "相对方必要配合义务缺失",
        "issue": "合同文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}可能在缺少{counterparty}必要配合的情况下仍承担履约后果；风险因素为：{severity}。",
    },
    "QUALITY_STANDARD_UNMEASURABLE": {
        "title": "服务标准缺少可衡量指标",
        "issue": "合同质量或服务标准存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能因缺少客观、可验证的质量标准而难以主张或证明履约是否合格；风险因素为：{severity}。",
    },
    "ACCEPTANCE_MECHANISM_REVIEW": {
        "title": "验收机制存在不完整风险",
        "issue": "合同验收安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能因验收标准、期限、整改或复验机制不清而面临交付、接收或结算争议；风险因素为：{severity}。",
    },
    "ACCEPTANCE_MECHANISM_ABSENT": {
        "title": "合同缺少完整验收机制",
        "issue": "合同文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}可能因缺少明确验收程序而无法稳定确认交付、接收和结算条件；风险因素为：{severity}。",
    },
    "ASSIGNMENT_SUBCONTRACT_REVIEW": {
        "title": "转委托、分包或转让安排需要限制",
        "issue": "合同第三方参与安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能因第三方参与范围、责任延续或同意程序不清承担履约风险；风险因素为：{severity}。",
    },
    "CHANGE_CONTROL_REVIEW": {
        "title": "变更控制和费用工期联动不足",
        "issue": "合同变更安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "变更未经双方确认时，{our_party}的工作范围、价款、资源或进度权益可能受到影响；风险因素为：{severity}。",
    },
    "CHANGE_CONTROL_ABSENT": {
        "title": "合同缺少书面变更控制机制",
        "issue": "合同文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}可能无法确定新增要求对价款、资源和工期的影响；风险因素为：{severity}。",
    },
    "WARRANTY_SUPPORT_REVIEW": {
        "title": "质保、整改或支持机制不完整",
        "issue": "合同质保或支持安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}可能因支持范围、期限、整改和复验责任不清而面临质量缺陷处理及持续履约争议；风险因素为：{severity}。",
    },
    "WARRANTY_SUPPORT_ABSENT": {
        "title": "合同缺少质保、整改或支持机制",
        "issue": "合同文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}可能无法通过明确机制处理质量缺陷和后续支持；风险因素为：{severity}。",
    },
    "PROJECTED_IR_REVIEW": {
        "title": "履行安排存在待控制风险",
        "issue": "合同履行安排存在“{fact}”。Primary Evidence显示：{evidence}。",
        "impact": "{our_party}的履约权益可能受到影响；风险因素为：{severity}。",
    },
    "MISSING_EXPECTED_IR": {
        "title": "合同缺少必要履行安排",
        "issue": "合同文本检查发现“{fact}”。检查依据为：{evidence}。",
        "impact": "{our_party}可能因必要机制缺失承担履约不确定性；风险因素为：{severity}。",
    },
}

_PO_CANDIDATE_POLICIES: dict[str, dict[str, object]] = {
    "SCOPE_EXPANSION": {
        "strength": "STRONG_SIGNAL",
        "severity_rule_id": "PO_SCOPE_EXPANSION_V1",
        "trigger_conditions": [
            "相对方可以开放式或单方扩大我方履行范围",
            "新增工作缺少双方书面确认及费用、工期联动",
        ],
        "mitigating_conditions": [
            "范围封闭且变更必须双方书面确认",
            "新增工作同步调整价款和工期",
        ],
    },
    "DELIVERY_SCHEDULE_REVIEW": {
        "strength": "SEMANTIC_REVIEW",
        "severity_rule_id": "PO_DELIVERY_SCHEDULE_V1",
        "trigger_conditions": ["交付内容、期限或责任边界不明确"],
        "mitigating_conditions": ["交付物、时间节点和延迟责任完整明确"],
    },
    "RIGHTS_OBLIGATIONS_IMBALANCE": {
        "strength": "STRONG_SIGNAL",
        "severity_rule_id": "PO_UNILATERAL_CONTROL_V1",
        "trigger_conditions": [
            "相对方享有单方检查、决定、拒绝、暂停或要求权",
            "我方承担对应义务且缺少客观条件、通知或救济",
        ],
        "mitigating_conditions": [
            "单方权利属于我方而不是相对方",
            "合同提供客观标准、合理通知和有效救济",
        ],
    },
    "COOPERATION_DEPENDENCY": {
        "strength": "SEMANTIC_REVIEW",
        "severity_rule_id": "PO_COOPERATION_DEPENDENCY_V1",
        "trigger_conditions": ["相对方配合缺口会使我方承担延迟或履约后果"],
        "mitigating_conditions": ["资料、接口、时限、顺延和归责均已明确"],
    },
    "COOPERATION_OBLIGATION_ABSENT": {
        "strength": "HARD_RULE",
        "severity_rule_id": "PO_COOPERATION_ABSENT_V1",
        "trigger_conditions": ["未定位到相对方必要配合义务且我方承担履约结果"],
        "mitigating_conditions": ["合同其他条款已明确配合义务和顺延后果"],
    },
    "QUALITY_STANDARD_UNMEASURABLE": {
        "strength": "HARD_RULE",
        "severity_rule_id": "PO_QUALITY_STANDARD_V1",
        "trigger_conditions": ["质量或服务标准使用不可衡量的主观表述"],
        "mitigating_conditions": ["合同另有客观、可验证的完整指标"],
    },
    "ACCEPTANCE_MECHANISM_REVIEW": {
        "strength": "STRONG_SIGNAL",
        "severity_rule_id": "PO_ACCEPTANCE_REVIEW_V1",
        "trigger_conditions": ["验收标准、期限、程序、整改或复验机制不完整"],
        "mitigating_conditions": ["验收及复验机制完整且对我方可执行"],
    },
    "ACCEPTANCE_MECHANISM_ABSENT": {
        "strength": "HARD_RULE",
        "severity_rule_id": "PO_ACCEPTANCE_ABSENT_V1",
        "trigger_conditions": ["当前合同投影中未定位到验收机制"],
        "mitigating_conditions": ["Counter Evidence明确给出完整验收及复验条款"],
    },
    "ASSIGNMENT_SUBCONTRACT_REVIEW": {
        "strength": "SEMANTIC_REVIEW",
        "severity_rule_id": "PO_ASSIGNMENT_SUBCONTRACT_V1",
        "trigger_conditions": ["条款涉及第三方、委托、分包或权利义务转让"],
        "mitigating_conditions": [
            "文本仅描述第三方业务或维修，不构成转委托、分包或转让",
            "事先书面同意和原责任主体持续责任均已明确",
        ],
    },
    "CHANGE_CONTROL_REVIEW": {
        "strength": "HARD_RULE",
        "severity_rule_id": "PO_CHANGE_CONTROL_V1",
        "trigger_conditions": [
            "相对方可提出新增或变更要求且我方必须执行",
            "缺少双方书面确认及费用、工期联动",
        ],
        "mitigating_conditions": [
            "该权利属于我方而非相对方",
            "合同另有双方书面确认和价款、工期调整机制",
        ],
    },
    "CHANGE_CONTROL_ABSENT": {
        "strength": "HARD_RULE",
        "severity_rule_id": "PO_CHANGE_CONTROL_ABSENT_V1",
        "trigger_conditions": ["当前合同投影中未定位到书面变更及联动机制"],
        "mitigating_conditions": ["Counter Evidence明确给出完整变更机制"],
    },
    "WARRANTY_SUPPORT_REVIEW": {
        "strength": "STRONG_SIGNAL",
        "severity_rule_id": "PO_WARRANTY_SUPPORT_V1",
        "trigger_conditions": ["质保、维护、整改、响应、修复或复验边界不完整"],
        "mitigating_conditions": ["范围、期限、响应等级、整改、复验及后果完整"],
    },
    "WARRANTY_SUPPORT_ABSENT": {
        "strength": "HARD_RULE",
        "severity_rule_id": "PO_WARRANTY_SUPPORT_ABSENT_V1",
        "trigger_conditions": ["当前合同投影中未定位到质保、整改或支持机制"],
        "mitigating_conditions": ["Counter Evidence明确给出完整质保和支持机制"],
    },
    "PROJECTED_IR_REVIEW": {
        "strength": "SEMANTIC_REVIEW",
        "severity_rule_id": "PO_PROJECTED_IR_REVIEW_V1",
        "trigger_conditions": ["当前检查存在相关IR，必须完成语义裁决"],
        "mitigating_conditions": ["相关条款对我方中性、有利或保护机制完整"],
    },
    "MISSING_EXPECTED_IR": {
        "strength": "SEMANTIC_REVIEW",
        "severity_rule_id": "PO_MISSING_EXPECTED_IR_V1",
        "trigger_conditions": ["当前检查没有定位到预期IR"],
        "mitigating_conditions": ["该合同类型不适用或其他条款已提供等效机制"],
    },
}


_PO_CANONICAL_ROOT_POLICIES: dict[str, dict[str, object]] = {
    "SCOPE_EXPANSION": {
        "canonical_root_type": "CORE_SCOPE_AND_DELIVERY_IMBALANCE",
        "root_severity_rule_id": "PO_SCOPE_DELIVERY_ROOT_V1",
        "merge_group_id": "PO001_SCOPE_DELIVERY",
        "merge_compatible_candidate_types": ("DELIVERY_SCHEDULE_REVIEW",),
        "deterministic_severity_factors": (
            "UNILATERAL_CONTROL",
            "BROAD_SCOPE",
        ),
    },
    "DELIVERY_SCHEDULE_REVIEW": {
        "canonical_root_type": "CORE_SCOPE_AND_DELIVERY_IMBALANCE",
        "root_severity_rule_id": "PO_SCOPE_DELIVERY_ROOT_V1",
        "merge_group_id": "PO001_SCOPE_DELIVERY",
        "merge_compatible_candidate_types": ("SCOPE_EXPANSION",),
        "deterministic_severity_factors": (),
    },
    "RIGHTS_OBLIGATIONS_IMBALANCE": {
        "canonical_root_type": "RIGHTS_OBLIGATIONS_IMBALANCE",
        "root_severity_rule_id": "PO_UNILATERAL_CONTROL_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": ("UNILATERAL_CONTROL",),
    },
    "COOPERATION_DEPENDENCY": {
        "canonical_root_type": "COOPERATION_DEPENDENCY",
        "root_severity_rule_id": "PO_COOPERATION_DEPENDENCY_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": (),
    },
    "COOPERATION_OBLIGATION_ABSENT": {
        "canonical_root_type": "COOPERATION_OBLIGATION_ABSENT",
        "root_severity_rule_id": "PO_COOPERATION_ABSENT_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": ("MISSING_CORE_MECHANISM",),
    },
    "QUALITY_STANDARD_UNMEASURABLE": {
        "canonical_root_type": "QUALITY_STANDARD_UNMEASURABLE",
        "root_severity_rule_id": "PO_QUALITY_STANDARD_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": ("MISSING_CORE_MECHANISM",),
    },
    "ACCEPTANCE_MECHANISM_REVIEW": {
        "canonical_root_type": "ACCEPTANCE_MECHANISM_REVIEW",
        "root_severity_rule_id": "PO_ACCEPTANCE_REVIEW_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": (),
    },
    "ACCEPTANCE_MECHANISM_ABSENT": {
        "canonical_root_type": "ACCEPTANCE_MECHANISM_ABSENT",
        "root_severity_rule_id": "PO_ACCEPTANCE_ABSENT_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": ("MISSING_CORE_MECHANISM",),
    },
    "ASSIGNMENT_SUBCONTRACT_REVIEW": {
        "canonical_root_type": "ASSIGNMENT_SUBCONTRACT_REVIEW",
        "root_severity_rule_id": "PO_ASSIGNMENT_SUBCONTRACT_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": (),
    },
    "CHANGE_CONTROL_REVIEW": {
        "canonical_root_type": "CHANGE_CONTROL_REVIEW",
        "root_severity_rule_id": "PO_CHANGE_CONTROL_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": ("UNILATERAL_CONTROL",),
    },
    "CHANGE_CONTROL_ABSENT": {
        "canonical_root_type": "CHANGE_CONTROL_ABSENT",
        "root_severity_rule_id": "PO_CHANGE_CONTROL_ABSENT_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": ("MISSING_CORE_MECHANISM",),
    },
    "WARRANTY_SUPPORT_REVIEW": {
        "canonical_root_type": "WARRANTY_SUPPORT_REVIEW",
        "root_severity_rule_id": "PO_WARRANTY_SUPPORT_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": (),
    },
    "WARRANTY_SUPPORT_ABSENT": {
        "canonical_root_type": "WARRANTY_SUPPORT_ABSENT",
        "root_severity_rule_id": "PO_WARRANTY_SUPPORT_ABSENT_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": ("MISSING_CORE_MECHANISM",),
    },
    "PROJECTED_IR_REVIEW": {
        "canonical_root_type": "PROJECTED_IR_REVIEW",
        "root_severity_rule_id": "PO_PROJECTED_IR_REVIEW_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": (),
    },
    "MISSING_EXPECTED_IR": {
        "canonical_root_type": "MISSING_EXPECTED_IR",
        "root_severity_rule_id": "PO_MISSING_EXPECTED_IR_V1",
        "merge_group_id": None,
        "merge_compatible_candidate_types": (),
        "deterministic_severity_factors": (),
    },
}


_ICD_ABSENCE_CANDIDATE_TYPES = {
    "ICD-001": "FOREGROUND_IP_OWNERSHIP_ABSENT",
    "ICD-002": "BACKGROUND_IP_LICENSE_ABSENT",
    "ICD-003": "THIRD_PARTY_IP_PROTECTION_ABSENT",
    "ICD-004": "CONFIDENTIALITY_COMPLETENESS_ABSENT",
    "ICD-005": "DATA_PROCESSING_SECURITY_ABSENT",
    "ICD-006": "DATA_RETURN_DELETION_ABSENT",
}

_ICD_ABSENCE_TRIGGER_REASONS = {
    "ICD-001": "存在项目成果或交付成果场景，但未定位到成果知识产权归属和使用边界的完整机制",
    "ICD-002": "存在背景技术、软件、模板、方法或既有材料场景，但未定位到背景知识产权保留和许可边界",
    "ICD-003": "存在成果、软件或技术交付场景，但未定位到第三方知识产权保证和侵权救济闭环",
    "ICD-004": "存在保密信息或保密义务场景，但未定位到定义、例外、披露程序和期限均完整的保密机制",
    "ICD-005": "存在数据或信息处理、访问或传输场景，但未定位到数据用途、访问、安全标准和事件通知机制",
    "ICD-006": "存在数据处理、存储、访问或交付场景，但未定位到合同终止后的返还、删除和留存闭环",
}

_ICD_SCENE_PATTERNS = {
    "ICD-001": re.compile(
        r"(项目|服务|工作|开发|制作|交付).{0,16}"
        r"(成果|作品|软件|源代码|文档|方案|模型|平台)"
        r"|"
        r"(成果|作品|软件|源代码|文档|方案|模型|平台).{0,16}"
        r"(开发|制作|形成|产生|交付|提供)"
    ),
    "ICD-002": re.compile(
        r"(背景|原有|既有|预先存在|自有).{0,16}"
        r"(技术|软件|源代码|模板|方法|工具|资料|知识产权)"
    ),
    "ICD-003": re.compile(
        r"(开发|制作|形成|产生|交付|提供).{0,16}"
        r"(成果|作品|软件|源代码|技术|文档|模型|平台)"
        r"|"
        r"(成果|作品|软件|源代码|技术|文档|模型|平台).{0,16}"
        r"(开发|制作|形成|产生|交付|提供)"
    ),
    "ICD-004": re.compile(
        r"(保密|秘密|机密|商业信息|业务信息|客户信息|资料|数据).{0,16}"
        r"(披露|提供|交换|接触|知悉|使用|处理|保守|保护)"
        r"|"
        r"(披露|提供|交换|接触|知悉|使用|处理|保守|保护).{0,16}"
        r"(保密|秘密|机密|商业信息|业务信息|客户信息|资料|数据)"
    ),
    "ICD-005": re.compile(
        r"(数据|个人信息|客户信息|业务信息).{0,18}"
        r"(收集|使用|处理|访问|传输|共享|存储|导入|导出|安全)"
        r"|"
        r"(收集|使用|处理|访问|传输|共享|存储|导入|导出).{0,18}"
        r"(数据|个人信息|客户信息|业务信息)"
    ),
    "ICD-006": re.compile(
        r"(数据|个人信息|客户信息|业务信息|数据载体).{0,18}"
        r"(处理|访问|传输|共享|存储|导入|导出|交付|备份)"
        r"|"
        r"(处理|访问|传输|共享|存储|导入|导出|交付|备份).{0,18}"
        r"(数据|个人信息|客户信息|业务信息|数据载体)"
    ),
}


def _icd_request_text(
    ir_refs: dict[str, CommercialIrItem],
) -> str:
    return "\n".join(
        " ".join(filter(None, (item.subject, item.predicate, item.object)))
        for item in ir_refs.values()
    )


def _icd_scene_relevant(
    check_code: str,
    ir_refs: dict[str, CommercialIrItem],
) -> bool:
    return bool(_ICD_SCENE_PATTERNS[check_code].search(_icd_request_text(ir_refs)))


def _build_icd_candidates(
    request: GenericReviewRequest,
    ir_refs: dict[str, CommercialIrItem],
    anchor_ref_by_id: dict[str, str],
) -> list[DeterministicRiskCandidate]:
    """Build finite ICD candidates without exposing unrelated IR to the model."""
    candidates: list[DeterministicRiskCandidate] = []
    specs = {item.check_code: item for item in request.assigned_check_specs}
    for check_code, spec in specs.items():
        allowed_item_ids = {
            source.ir_item_id
            for source in request.evidence_sources
            if check_code in source.allowed_check_codes
        }
        for candidate_type, reason, _patterns in ICD_SOURCE_PATTERN_RULES[
            check_code
        ]:
            refs = sorted(
                ref
                for ref, item in ir_refs.items()
                if item.item_id in allowed_item_ids
                and item.ir_type in spec.required_ir_types
            )
            if refs:
                candidates.append(
                    _icd_candidate(
                        request,
                        check_code,
                        candidate_type,
                        reason,
                        spec.required_ir_types,
                        refs,
                        ir_refs,
                        anchor_ref_by_id,
                    )
                )

        absence_sources = [
            item
            for item in request.absence_evidence_sources
            if item.check_code == check_code
        ]
        if (
            absence_sources
            and _icd_scene_relevant(check_code, ir_refs)
        ):
            candidates.append(
                _icd_candidate(
                    request,
                    check_code,
                    _ICD_ABSENCE_CANDIDATE_TYPES[check_code],
                    _ICD_ABSENCE_TRIGGER_REASONS[check_code],
                    spec.required_ir_types,
                    [],
                    ir_refs,
                    anchor_ref_by_id,
                )
            )
    return candidates


def _icd_confidentiality_perspective_precondition(
    request: GenericReviewRequest,
    *,
    candidate_type: str,
    primary_source_ids: list[str],
) -> IcdPerspectivePrecondition | None:
    """Keep an explicit protective duty separate from mechanism completeness."""
    if candidate_type != "CONFIDENTIALITY_PROTECTION_REVIEW":
        return None
    source_by_id = {item.source_id: item for item in request.evidence_sources}
    sources = [
        source_by_id[source_id]
        for source_id in primary_source_ids
        if source_id in source_by_id
    ]
    if not sources:
        return None
    roles = contract_party_roles(
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
    )
    our_aliases = roles.aliases_for_our_party()
    counterparty_aliases = roles.aliases_for_counterparty()

    def contains_alias(text: str, aliases: tuple[str, str]) -> bool:
        return any(alias and alias in text for alias in aliases)

    def is_confidentiality_duty(source: RiskEvidenceSource) -> bool:
        text = " ".join(
            filter(
                None,
                (source.subject, source.predicate, source.object, source.quoted_text),
            )
        )
        return bool(
            re.search(r"(保密|保守|保护|不得.{0,8}(披露|泄露|公开))", text)
            and re.search(r"(秘密|保密信息|商业信息|业务信息|资料|数据)", text)
        )

    def protects_our_party(source: RiskEvidenceSource) -> bool:
        subject = source.subject or source.quoted_text
        protected = " ".join(filter(None, (source.object, source.quoted_text)))
        return bool(
            is_confidentiality_duty(source)
            and contains_alias(subject, counterparty_aliases)
            and contains_alias(protected, our_aliases)
        )

    def burdens_our_party(source: RiskEvidenceSource) -> bool:
        subject = source.subject or source.quoted_text
        return bool(
            is_confidentiality_duty(source)
            and contains_alias(subject, our_aliases)
            and not contains_alias(subject, counterparty_aliases)
        )

    counterparty_protects_our_party = all(
        protects_our_party(source) for source in sources
    )
    adverse_burden_on_our_party = any(
        burdens_our_party(source) for source in sources
    )
    model_review_required = not (
        counterparty_protects_our_party and not adverse_burden_on_our_party
    )
    decision_reason = (
        "Primary Evidence仅要求相对方保护我方保密信息，未对我方施加不利保密负担；"
        "文本条款本身不构成风险，定义、例外、期限和披露程序的完整性由独立Absence Candidate审查"
        if not model_review_required
        else "Primary Evidence不是单纯由相对方保护我方的有利安排，仍需模型裁决其文本风险"
    )
    return IcdPerspectivePrecondition(
        counterparty_protects_our_party=counterparty_protects_our_party,
        adverse_burden_on_our_party=adverse_burden_on_our_party,
        model_review_required=model_review_required,
        supporting_source_ids=[item.source_id for item in sources],
        decision_reason=decision_reason,
    )


def _icd_candidate(
    request: GenericReviewRequest,
    check_code: str,
    candidate_type: str,
    trigger_reason: str,
    required_ir_types: list[str],
    refs: list[str],
    ir_refs: dict[str, CommercialIrItem],
    anchor_ref_by_id: dict[str, str],
) -> DeterministicRiskCandidate:
    evidence_refs = sorted(
        {
            anchor_ref_by_id[anchor.anchor_id]
            for ref in refs
            for anchor in ir_refs[ref].source_anchors
        }
    )
    source_by_binding = {
        (item.ir_item_id, item.anchor_id): item.source_id
        for item in request.evidence_sources
    }
    primary_source_ids = list(
        dict.fromkeys(
            source_by_binding[(ir_refs[ref].item_id, anchor.anchor_id)]
            for ref in refs
            for anchor in ir_refs[ref].source_anchors
            if (ir_refs[ref].item_id, anchor.anchor_id) in source_by_binding
        )
    )
    if not primary_source_ids:
        primary_source_ids = [
            item.source_id
            for item in request.absence_evidence_sources
            if item.check_code == check_code
        ]
    if not primary_source_ids:
        raise DirectReviewError(
            "RISK_CANDIDATE_PRIMARY_EVIDENCE_INVALID",
            f"{check_code} Candidate has no legal text or Absence Source",
        )
    all_check_source_ids = list(
        dict.fromkeys(
            item.source_id
            for item in request.evidence_sources
            if check_code in item.allowed_check_codes
        )
    )
    other_check_sources = [
        source_id
        for source_id in all_check_source_ids
        if source_id not in set(primary_source_ids)
    ]
    policy = _ICD_CANDIDATE_POLICIES[candidate_type]
    root_policy = _ICD_CANONICAL_ROOT_POLICIES[candidate_type]
    perspective_precondition = _icd_confidentiality_perspective_precondition(
        request,
        candidate_type=candidate_type,
        primary_source_ids=primary_source_ids,
    )
    candidate_id = _stable_id(
        "risk-candidate",
        {
            "generation_id": request.generation_id,
            "batch_id": request.batch_id,
            "check_code": check_code,
            "candidate_type": candidate_type,
            "required_ir_types": required_ir_types,
            "candidate_ir_refs": refs,
            "candidate_evidence_refs": evidence_refs,
        },
    )
    return DeterministicRiskCandidate(
        candidate_id=candidate_id,
        check_code=check_code,
        candidate_type=candidate_type,
        trigger_reason=trigger_reason,
        required_ir_types=required_ir_types,
        candidate_ir_refs=refs,
        candidate_evidence_refs=evidence_refs,
        requires_model_decision=(
            perspective_precondition.model_review_required
            if perspective_precondition is not None
            else True
        ),
        candidate_strength=policy["strength"],
        criticality="REQUIRED",
        facts=[
            trigger_reason,
            f"Python确定性绑定{len(primary_source_ids)}个Primary Evidence Source",
        ],
        trigger_conditions=policy["trigger_conditions"],
        mitigating_conditions=policy["mitigating_conditions"],
        disqualifying_conditions=policy["mitigating_conditions"],
        primary_evidence_requirements=[
            (
                "TEXT_QUOTE必须直接描述当前ICD检查的权利、保密、"
                "数据或安全安排"
            )
            if refs
            else "必须使用当前Check的合法RiskAbsenceEvidenceSource"
        ],
        absence_evidence_requirements=(
            [
                "Absence Source必须列明检查范围、缺失目标和确定性扫描方法",
                "合同必须先满足当前ICD检查的业务场景前置条件",
            ]
            if not refs
            else []
        ),
        primary_evidence_source_ids=primary_source_ids,
        allowed_supporting_evidence_source_ids=other_check_sources,
        allowed_counter_evidence_source_ids=other_check_sources,
        severity_rule_id=policy["severity_rule_id"],
        canonical_root_type=root_policy["canonical_root_type"],
        root_severity_rule_id=root_policy["root_severity_rule_id"],
        merge_group_id=root_policy["merge_group_id"],
        merge_compatible_candidate_types=list(
            root_policy["merge_compatible_candidate_types"]
        ),
        core_primary_evidence_source_ids=primary_source_ids,
        context_primary_evidence_source_ids=[],
        deterministic_severity_factors=list(
            root_policy["deterministic_severity_factors"]
        ),
        allowed_severity_factors=_icd_allowed_severity_factors(
            check_code,
            candidate_type,
        ),
        icd_perspective_precondition=perspective_precondition,
    )


def _lre_matching_refs(
    request: GenericReviewRequest,
    ir_refs: dict[str, CommercialIrItem],
    *,
    check_code: str,
    pattern: str,
) -> list[str]:
    matching_item_ids = {
        source.ir_item_id
        for source in request.evidence_sources
        if check_code in source.allowed_check_codes
        and re.search(
            pattern,
            " ".join(
                filter(
                    None,
                    (
                        source.subject,
                        source.predicate,
                        source.object,
                        source.quoted_text,
                    ),
                )
            ),
        )
    }
    return sorted(
        ref for ref, item in ir_refs.items() if item.item_id in matching_item_ids
    )


def _lre_candidate(
    request: GenericReviewRequest,
    *,
    check_code: str,
    candidate_type: str,
    trigger_reason: str,
    required_ir_types: list[str],
    refs: list[str],
    ir_refs: dict[str, CommercialIrItem],
    anchor_ref_by_id: dict[str, str],
    include_absence: bool = False,
) -> DeterministicRiskCandidate:
    evidence_refs = sorted(
        {
            anchor_ref_by_id[anchor.anchor_id]
            for ref in refs
            for anchor in ir_refs[ref].source_anchors
        }
    )
    source_by_binding = {
        (item.ir_item_id, item.anchor_id): item.source_id
        for item in request.evidence_sources
    }
    all_text_source_ids = list(
        dict.fromkeys(
            source_by_binding[(ir_refs[ref].item_id, anchor.anchor_id)]
            for ref in refs
            for anchor in ir_refs[ref].source_anchors
            if (ir_refs[ref].item_id, anchor.anchor_id) in source_by_binding
        )
    )
    absence_primary_ids = (
        [
            item.source_id
            for item in request.absence_evidence_sources
            if item.check_code == check_code
        ]
        if include_absence
        else []
    )
    primary_text_limit = max(0, 20 - len(absence_primary_ids))
    text_primary_ids = all_text_source_ids[:primary_text_limit]
    primary_source_ids = list(
        dict.fromkeys([*text_primary_ids, *absence_primary_ids])
    )
    if not primary_source_ids:
        raise DirectReviewError(
            "RISK_CANDIDATE_PRIMARY_EVIDENCE_INVALID",
            f"{check_code} LRE Candidate has no legal text or Absence Source",
        )
    all_check_source_ids = list(
        dict.fromkeys([*all_text_source_ids, *absence_primary_ids])
    )
    other_source_ids = [
        source_id
        for source_id in all_check_source_ids
        if source_id not in set(primary_source_ids)
    ]
    policy = _LRE_CANDIDATE_POLICIES[candidate_type]
    root_policy = dict(_LRE_CANONICAL_ROOT_POLICIES[candidate_type])
    if candidate_type == "BROAD_BREACH_TRIGGER_REVIEW":
        selected_text = "\n".join(
            source.quoted_text
            for source in request.evidence_sources
            if source.source_id in set(primary_source_ids)
        )
        if re.search(
            r"(全部赔偿|包括但不限于|间接损失|可预期利益|所有损失)",
            selected_text,
        ):
            root_policy.update(
                canonical_root_type="UNBOUNDED_LIABILITY_EXPOSURE",
                root_severity_rule_id="LRE_UNBOUNDED_LIABILITY_ROOT_V1",
                merge_group_id="LRE_UNBOUNDED_LIABILITY",
                merge_compatible_candidate_types=(
                    "OVERBROAD_LOSS_SCOPE_REVIEW",
                    "LIABILITY_CAP_ABSENT",
                    "OVERBROAD_INDEMNITY_REVIEW",
                ),
            )
    candidate_id = _stable_id(
        "risk-candidate",
        {
            "generation_id": request.generation_id,
            "batch_id": request.batch_id,
            "check_code": check_code,
            "candidate_type": candidate_type,
            "required_ir_types": required_ir_types,
            "candidate_ir_refs": refs,
            "candidate_evidence_refs": evidence_refs,
            "absence_source_ids": absence_primary_ids,
        },
    )
    core_primary_ids = text_primary_ids or absence_primary_ids
    context_primary_ids = [
        source_id
        for source_id in absence_primary_ids
        if source_id not in set(core_primary_ids)
    ]
    return DeterministicRiskCandidate(
        candidate_id=candidate_id,
        check_code=check_code,
        candidate_type=candidate_type,
        trigger_reason=trigger_reason,
        required_ir_types=required_ir_types,
        candidate_ir_refs=refs,
        candidate_evidence_refs=evidence_refs,
        requires_model_decision=True,
        candidate_strength=policy["strength"],
        criticality="REQUIRED",
        facts=[
            trigger_reason,
            f"Python确定性绑定{len(primary_source_ids)}个Primary Evidence Source",
        ],
        trigger_conditions=policy["trigger_conditions"],
        mitigating_conditions=policy["mitigating_conditions"],
        disqualifying_conditions=policy["mitigating_conditions"],
        primary_evidence_requirements=[
            "TEXT_QUOTE必须直接支持当前责任、救济或退出风险根因"
        ],
        absence_evidence_requirements=(
            [
                "Absence Source必须列明检查范围、缺失目标和确定性扫描方法",
                "不得仅凭单条义务或费用文本推断机制缺失",
            ]
            if include_absence
            else []
        ),
        primary_evidence_source_ids=primary_source_ids,
        allowed_supporting_evidence_source_ids=other_source_ids,
        allowed_counter_evidence_source_ids=other_source_ids,
        severity_rule_id=policy["severity_rule_id"],
        canonical_root_type=root_policy["canonical_root_type"],
        root_severity_rule_id=root_policy["root_severity_rule_id"],
        merge_group_id=root_policy["merge_group_id"],
        merge_compatible_candidate_types=list(
            root_policy["merge_compatible_candidate_types"]
        ),
        core_primary_evidence_source_ids=core_primary_ids,
        context_primary_evidence_source_ids=context_primary_ids,
        deterministic_severity_factors=list(
            root_policy["deterministic_severity_factors"]
        ),
        allowed_severity_factors=_lre_allowed_severity_factors(
            check_code,
            candidate_type,
        ),
    )


def _build_lre_candidates(
    request: GenericReviewRequest,
    ir_refs: dict[str, CommercialIrItem],
    anchor_ref_by_id: dict[str, str],
) -> list[DeterministicRiskCandidate]:
    """Build the finite, source-bound LRE Candidate set."""
    candidates: list[DeterministicRiskCandidate] = []
    specs = {item.check_code: item for item in request.assigned_check_specs}
    roles = contract_party_roles(
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
    )

    def only_our_exposure(refs: list[str]) -> list[str]:
        relevant_refs: list[str] = []
        for ref in refs:
            item = ir_refs[ref]
            subject = item.subject or ""
            is_our = text_names_role(subject, roles.aliases_for_our_party())
            is_counterparty_only = (
                text_names_role(subject, roles.aliases_for_counterparty())
                and not is_our
            )
            if not is_counterparty_only:
                relevant_refs.append(ref)
        return relevant_refs

    def add(
        check_code: str,
        candidate_type: str,
        reason: str,
        pattern: str,
        *,
        absence: bool = False,
        require_text: bool = True,
    ) -> None:
        if check_code not in specs:
            return
        refs = _lre_matching_refs(
            request,
            ir_refs,
            check_code=check_code,
            pattern=pattern,
        )
        if candidate_type in {
            "BROAD_BREACH_TRIGGER_REVIEW",
            "OVERBROAD_LOSS_SCOPE_REVIEW",
            "CUMULATIVE_REMEDIES_REVIEW",
            "LIABILITY_CAP_ABSENT",
            "LIABILITY_CAP_BYPASS_REVIEW",
            "OVERBROAD_INDEMNITY_REVIEW",
        }:
            refs = only_our_exposure(refs)
        if check_code == "LRE-005":
            termination_refs = [
                ref
                for ref in refs
                if ir_refs[ref].ir_type == "termination_terms"
            ]
            if termination_refs:
                refs = termination_refs
        if require_text and not refs:
            return
        if absence and not any(
            source.check_code == check_code
            for source in request.absence_evidence_sources
        ):
            return
        candidates.append(
            _lre_candidate(
                request,
                check_code=check_code,
                candidate_type=candidate_type,
                trigger_reason=reason,
                required_ir_types=specs[check_code].required_ir_types,
                refs=refs,
                ir_refs=ir_refs,
                anchor_ref_by_id=anchor_ref_by_id,
                include_absence=absence,
            )
        )

    add(
        "LRE-001",
        "BROAD_BREACH_TRIGGER_REVIEW",
        "合同存在重大违约或责任触发表述，须核对条件是否客观、具体并与责任程度相称",
        LRE_BROAD_BREACH_TRIGGER_PATTERN,
    )
    add(
        "LRE-002",
        "OVERBROAD_LOSS_SCOPE_REVIEW",
        "损失赔偿明确覆盖间接损失、可预期利益或开放费用范围",
        r"(全部赔偿|包括但不限于|间接损失|可预期利益|律师费|调查费|公证费)",
    )
    add(
        "LRE-002",
        "CUMULATIVE_REMEDIES_REVIEW",
        "合同允许违约金、赔偿或其他责任同时主张，须核对是否造成重复受偿",
        r"(同时.{0,12}主张|违约金.{0,20}(并|同时).{0,12}赔偿|扣分.{0,16}违约金)",
    )
    add(
        "LRE-003",
        "LIABILITY_CAP_ABSENT",
        "合同存在开放赔偿责任，但未定位到适用于全部责任的累计责任总上限",
        r"(全部赔偿|包括但不限于|间接损失|可预期利益|同时.{0,8}主张)",
        absence=True,
    )
    if "LRE-003" in specs:
        cap_refs = _lre_matching_refs(
            request,
            ir_refs,
            check_code="LRE-003",
            pattern=r"(责任|赔偿|违约金).{0,18}(上限|限额|最高|不超过)",
        )
        bypass_refs = _lre_matching_refs(
            request,
            ir_refs,
            check_code="LRE-003",
            pattern=r"(全部赔偿|所有损失|不受.{0,10}(上限|限制)|同时.{0,8}主张)",
        )
        cap_refs = only_our_exposure(cap_refs)
        bypass_refs = only_our_exposure(bypass_refs)
        if cap_refs and bypass_refs:
            candidates.append(
                _lre_candidate(
                    request,
                    check_code="LRE-003",
                    candidate_type="LIABILITY_CAP_BYPASS_REVIEW",
                    trigger_reason="责任上限与可能绕过上限的赔偿或累计责任条款同时存在",
                    required_ir_types=specs["LRE-003"].required_ir_types,
                    refs=sorted(set(cap_refs + bypass_refs)),
                    ir_refs=ir_refs,
                    anchor_ref_by_id=anchor_ref_by_id,
                )
            )
    add(
        "LRE-004",
        "OVERBROAD_INDEMNITY_REVIEW",
        "赔偿覆盖开放损失、第三方责任或全部费用，须核对过错、因果和索赔程序边界",
        r"(全部赔偿|包括但不限于|任何法律责任|第三方.{0,12}(赔偿|索赔)|律师费|调查费)",
    )
    add(
        "LRE-005",
        "TERMINATION_RIGHTS_REVIEW",
        "合同约定双方解除权及触发条件，须核对是否对等并具有合理整改机会",
        r"(解除|终止|改正|整改)",
    )
    add(
        "LRE-006",
        "TERMINATION_SETTLEMENT_REVIEW",
        "终止条款明示排除结算、返还或设置不合理的持续责任",
        r"((解除|终止)后.{0,24}(继续使用|不予返还|无偿移交|仍由.{0,6}承担|不予结算|不支付|费用不退)"
        r"|已完成.{0,16}(不结算|不支付)|预付款.{0,16}(不退|不返还))",
    )
    add(
        "LRE-006",
        "TERMINATION_SETTLEMENT_ABSENT",
        "持续履行或已发生成本但未定位到终止结算、返还和退出义务机制",
        r"$^",
        absence=True,
        require_text=False,
    )
    add(
        "LRE-007",
        "FORCE_MAJEURE_MECHANISM_ABSENT",
        "持续履行合同未定位到不可抗力定义、通知、减损及费用进度后果",
        r"$^",
        absence=True,
        require_text=False,
    )
    add(
        "LRE-008",
        "DISPUTE_RESOLUTION_ABSENT",
        "未定位到适用法律及单一明确的诉讼或仲裁和管辖机制",
        r"$^",
        absence=True,
        require_text=False,
    )
    return candidates


def _build_po_candidates(
    request: GenericReviewRequest,
    ir_refs: dict[str, CommercialIrItem],
    anchor_ref_by_id: dict[str, str],
) -> list[DeterministicRiskCandidate]:
    """Generate deterministic PO hypotheses; only the model may decide risk."""
    candidates: list[DeterministicRiskCandidate] = []
    specs = {item.check_code: item for item in request.assigned_check_specs}
    for check_code, spec in specs.items():
        matched_for_check = False
        role_filtered_for_check = False
        allowed_item_ids = {
            source.ir_item_id
            for source in request.evidence_sources
            if check_code in source.allowed_check_codes
        }
        for candidate_type, reason, patterns in PO_SOURCE_PATTERN_RULES[check_code]:
            refs = [
                ref
                for ref in _matching_po_ir_refs(
                    ir_refs,
                    anchor_ref_by_id,
                    patterns,
                )
                if ir_refs[ref].ir_type in spec.required_ir_types
                and ir_refs[ref].item_id in allowed_item_ids
            ]
            if not refs:
                continue
            if not _po_candidate_has_adverse_role_binding(
                request,
                candidate_type=candidate_type,
                refs=refs,
                ir_refs=ir_refs,
            ):
                role_filtered_for_check = True
                continue
            candidates.append(
                _po_candidate(
                    request,
                    check_code,
                    candidate_type,
                    reason,
                    spec.required_ir_types,
                    refs,
                    ir_refs,
                    anchor_ref_by_id,
                )
            )
            matched_for_check = True

        if role_filtered_for_check and not matched_for_check:
            continue

        missing_types = set(spec.required_ir_types) - {
            item.ir_type for item in ir_refs.values()
        }
        if check_code == "PO-003" and not matched_for_check:
            candidates.append(
                _po_candidate(
                    request,
                    check_code,
                    "COOPERATION_OBLIGATION_ABSENT",
                    "未定位到明确配合义务，须核对是否导致履约依赖和延迟归责缺口",
                    spec.required_ir_types,
                    [],
                    ir_refs,
                    anchor_ref_by_id,
                )
            )
            matched_for_check = True
        if check_code == "PO-004" and "acceptance_terms" in missing_types:
            candidates.append(
                _po_candidate(
                    request,
                    check_code,
                    "ACCEPTANCE_MECHANISM_ABSENT",
                    "投影中没有验收条款，须核对合同是否真实缺少验收标准、期限、程序及复验机制",
                    spec.required_ir_types,
                    [],
                    ir_refs,
                    anchor_ref_by_id,
                )
            )
            matched_for_check = True
        if check_code == "PO-006" and not matched_for_check:
            candidates.append(
                _po_candidate(
                    request,
                    check_code,
                    "CHANGE_CONTROL_ABSENT",
                    "未定位到明确变更机制，须核对范围、费用和工期是否缺少书面联动程序",
                    spec.required_ir_types,
                    [],
                    ir_refs,
                    anchor_ref_by_id,
                )
            )
            matched_for_check = True
        if check_code == "PO-007" and not matched_for_check:
            candidates.append(
                _po_candidate(
                    request,
                    check_code,
                    "WARRANTY_SUPPORT_ABSENT",
                    "未定位到质保、维护、整改或支持机制，须核对是否属于真实缺失",
                    spec.required_ir_types,
                    [],
                    ir_refs,
                    anchor_ref_by_id,
                )
            )
            matched_for_check = True
        if not matched_for_check:
            refs = sorted(
                ref
                for ref, item in ir_refs.items()
                if item.ir_type in spec.required_ir_types
            )
            candidates.append(
                _po_candidate(
                    request,
                    check_code,
                    "PROJECTED_IR_REVIEW" if refs else "MISSING_EXPECTED_IR",
                    (
                        "存在本检查所需IR，须按判定规则和Evidence完成裁决"
                        if refs
                        else "未发现本检查预期IR，须核对是否属于真实缺失"
                    ),
                    spec.required_ir_types,
                    refs,
                    ir_refs,
                    anchor_ref_by_id,
                )
            )
    return candidates


def _matching_po_ir_refs(
    ir_refs: dict[str, CommercialIrItem],
    anchor_ref_by_id: dict[str, str],
    patterns: tuple[str, ...],
) -> list[str]:
    del anchor_ref_by_id  # Kept in the signature to make source linkage explicit.
    return sorted(
        ref
        for ref, item in ir_refs.items()
        if any(
            re.search(pattern, " ".join(filter(None, (item.subject, item.predicate, item.object))))
            for pattern in patterns
        )
    )


_PO_DIRECTIONAL_CANDIDATE_TYPES = {
    "SCOPE_EXPANSION",
    "RIGHTS_OBLIGATIONS_IMBALANCE",
    "CHANGE_CONTROL_REVIEW",
}


def _po_item_burdens_our_party(
    request: GenericReviewRequest,
    item: CommercialIrItem,
) -> bool:
    roles = contract_party_roles(
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
    )
    subject = item.subject or ""
    text = " ".join(filter(None, (item.subject, item.predicate, item.object)))
    our_subject = text_names_role(subject, roles.aliases_for_our_party())
    counterparty_subject = text_names_role(
        subject, roles.aliases_for_counterparty()
    )
    obligation_like = item.ir_type in {
        "obligations",
        "prohibitions",
        "delivery_terms",
        "payment_terms",
        "termination_terms",
    } or bool(re.search(r"(应|须|必须|不得|负责|承担|保证|完成|交付)", text))
    control_like = item.ir_type == "rights" or bool(
        re.search(r"(有权|可自行|单方|随时.{0,8}(要求|检查|调整|决定))", text)
    )
    return bool(
        (our_subject and obligation_like)
        or (counterparty_subject and control_like)
    )


def _po_candidate_has_adverse_role_binding(
    request: GenericReviewRequest,
    *,
    candidate_type: str,
    refs: list[str],
    ir_refs: dict[str, CommercialIrItem],
) -> bool:
    if candidate_type not in _PO_DIRECTIONAL_CANDIDATE_TYPES:
        return True
    return any(
        _po_item_burdens_our_party(request, ir_refs[ref])
        for ref in refs
    )


def _po_candidate(
    request: GenericReviewRequest,
    check_code: str,
    candidate_type: str,
    trigger_reason: str,
    required_ir_types: list[str],
    refs: list[str],
    ir_refs: dict[str, CommercialIrItem],
    anchor_ref_by_id: dict[str, str],
) -> DeterministicRiskCandidate:
    evidence_refs = sorted(
        {
            anchor_ref_by_id[anchor.anchor_id]
            for ref in refs
            for anchor in ir_refs[ref].source_anchors
        }
    )
    source_by_binding = {
        (item.ir_item_id, item.anchor_id): item.source_id
        for item in request.evidence_sources
    }
    primary_source_ids = list(
        dict.fromkeys(
            source_by_binding[(ir_refs[ref].item_id, anchor.anchor_id)]
            for ref in refs
            for anchor in ir_refs[ref].source_anchors
            if (ir_refs[ref].item_id, anchor.anchor_id) in source_by_binding
        )
    )
    if not primary_source_ids:
        primary_source_ids = [
            item.source_id
            for item in request.absence_evidence_sources
            if item.check_code == check_code
        ]
    policy = _PO_CANDIDATE_POLICIES[candidate_type]
    root_policy = _PO_CANONICAL_ROOT_POLICIES[candidate_type]
    all_check_source_ids = [
        item.source_id
        for item in request.evidence_sources
        if check_code in item.allowed_check_codes
    ]
    all_check_source_ids = list(dict.fromkeys(all_check_source_ids))
    allowed_severity_factors = _po_allowed_severity_factors(
        check_code,
        candidate_type,
    )
    legal_factor_absence_ids = [
        source.source_id
        for source in request.absence_evidence_sources
        if check_code == "PO-006"
        and source.check_code == check_code
        and (
            (
                "NO_EFFECTIVE_REMEDY" in allowed_severity_factors
                and _po_valid_remedy_absence_source(source)
            )
            or (
                "MISSING_CORE_MECHANISM" in allowed_severity_factors
                and candidate_type.endswith("_ABSENT")
            )
        )
    ]
    supporting_source_ids = [
        source_id
        for source_id in [
            *all_check_source_ids,
            *legal_factor_absence_ids,
        ]
        if source_id not in set(primary_source_ids)
    ]
    counter_source_ids = _po_allowed_counter_source_ids(
        request,
        check_code=check_code,
        candidate_type=candidate_type,
        primary_source_ids=primary_source_ids,
        all_check_source_ids=all_check_source_ids,
    )
    core_primary_source_ids, context_primary_source_ids = (
        _po_partition_primary_source_ids(
            request,
            candidate_type=candidate_type,
            primary_source_ids=primary_source_ids,
        )
    )
    po003_precondition = (
        _po003_candidate_precondition(
            request,
            candidate_type=candidate_type,
            primary_source_ids=primary_source_ids,
        )
        if check_code == "PO-003"
        else None
    )
    candidate_id = _stable_id(
        "risk-candidate",
        {
            "generation_id": request.generation_id,
            "batch_id": request.batch_id,
            "check_code": check_code,
            "candidate_type": candidate_type,
            "required_ir_types": required_ir_types,
            "candidate_ir_refs": refs,
            "candidate_evidence_refs": evidence_refs,
        },
    )
    return DeterministicRiskCandidate(
        candidate_id=candidate_id,
        check_code=check_code,
        candidate_type=candidate_type,
        trigger_reason=trigger_reason,
        required_ir_types=required_ir_types,
        candidate_ir_refs=refs,
        candidate_evidence_refs=evidence_refs,
        requires_model_decision=(
            po003_precondition.model_review_required
            if po003_precondition is not None
            else True
        ),
        candidate_strength=policy["strength"],
        criticality="REQUIRED",
        facts=[
            trigger_reason,
            (
                f"Python确定性绑定{len(primary_source_ids)}个Primary Evidence Source"
            ),
        ],
        trigger_conditions=policy["trigger_conditions"],
        mitigating_conditions=policy["mitigating_conditions"],
        primary_evidence_source_ids=primary_source_ids,
        allowed_supporting_evidence_source_ids=supporting_source_ids,
        allowed_counter_evidence_source_ids=counter_source_ids,
        severity_rule_id=policy["severity_rule_id"],
        canonical_root_type=root_policy["canonical_root_type"],
        root_severity_rule_id=root_policy["root_severity_rule_id"],
        merge_group_id=root_policy["merge_group_id"],
        merge_compatible_candidate_types=list(
            root_policy["merge_compatible_candidate_types"]
        ),
        core_primary_evidence_source_ids=core_primary_source_ids,
        context_primary_evidence_source_ids=context_primary_source_ids,
        deterministic_severity_factors=list(
            dict.fromkeys(
                (
                    *root_policy["deterministic_severity_factors"],
                    *_po_evidence_deterministic_severity_factors(
                        request,
                        candidate_type=candidate_type,
                        core_primary_source_ids=core_primary_source_ids,
                    ),
                )
            )
        ),
        allowed_severity_factors=allowed_severity_factors,
        po003_precondition=po003_precondition,
    )


def _po003_valid_absence_source(
    source: RiskAbsenceEvidenceSource,
) -> bool:
    scope = " ".join(
        (
            source.checked_scope,
            source.verification_method,
            source.missing_target,
        )
    )
    cooperation = re.search(
        r"(配合|资料|材料|设备|场地|接口|审批|确认|验收|反馈|人员|"
        r"技术支持|协调第三方)",
        scope,
    )
    necessity = re.search(r"(必要|前提|依赖|履行所需|不可或缺)", scope)
    consequence = re.search(
        r"(延期|延迟|工期|费用|违约|责任|验收|付款|顺延|豁免)",
        scope,
    )
    absence = re.search(r"(缺少|缺失|未约定|未定位|不存在)", scope)
    return bool(cooperation and necessity and consequence and absence)


def _po003_candidate_precondition(
    request: GenericReviewRequest,
    *,
    candidate_type: str,
    primary_source_ids: list[str],
) -> Po003CandidatePrecondition:
    text_sources = {
        source.source_id: source
        for source in request.evidence_sources
        if source.source_id in set(primary_source_ids)
    }
    absence_sources = {
        source.source_id: source
        for source in request.absence_evidence_sources
        if source.source_id in set(primary_source_ids)
    }
    if candidate_type == "COOPERATION_OBLIGATION_ABSENT":
        valid_absence_ids = [
            source_id
            for source_id, source in absence_sources.items()
            if _po003_valid_absence_source(source)
        ]
        valid = bool(valid_absence_ids)
        unmet = [] if valid else [
            "合法Absence Source未同时证明必要配合类型、履行依赖和不合理后果"
        ]
        return Po003CandidatePrecondition(
            counterparty_cooperation_required=valid,
            performance_depends_on_cooperation=valid,
            adverse_consequence_to_our_party=valid,
            relief_or_adjustment_missing=valid,
            model_review_required=valid,
            supporting_source_ids=valid_absence_ids,
            unmet_conditions=unmet,
        )

    roles = contract_party_roles(
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
    )
    counterparty_alias = roles.counterparty_role
    cooperation_action = re.compile(
        r"(配合|协助|提供.{0,12}(资料|材料|设备|场地|接口|人员|技术支持)|"
        r"(审批|确认|验收|反馈)|协调.{0,8}第三方)"
    )
    dependency = re.compile(
        r"(依赖|前提|在.{0,16}(提供|确认|审批|验收|反馈).{0,12}(后|方可)|"
        r"收到.{0,16}后|待.{0,16}(完成|确认|提供))"
    )
    adverse = re.compile(
        r"(导致|造成|致使|因此|从而|若.{0,16}(未|不)).{0,24}"
        r"(延期|延迟|工期|费用|违约|责任|验收|付款|扣款|罚款|赔偿|拒付)"
        r"|"
        r"(延期|延迟|工期|费用|违约|责任|验收|付款|扣款|罚款|赔偿|拒付)"
        r".{0,24}(仍由我方|由我方承担|不予顺延|不得顺延|不能免责)"
    )
    relief_missing = re.compile(
        r"(不予顺延|不得顺延|无权.{0,12}(免责|调整|补救)|"
        r"仍由我方承担|我方仍承担)"
    )
    values = {
        source_id: _po_severity_source_text(source)
        for source_id, source in text_sources.items()
    }
    counterparty_source_ids = [
        source_id
        for source_id, source in text_sources.items()
        if (
            counterparty_alias in (source.subject or "")
            or request.counterparty in (source.subject or "")
        )
        and cooperation_action.search(values[source_id])
    ]
    combined = "；".join(values.values())
    counterparty_required = bool(counterparty_source_ids)
    depends = bool(dependency.search(combined))
    adverse_to_us = bool(adverse.search(combined))
    adjustment_missing = bool(relief_missing.search(combined))
    model_review_required = bool(
        counterparty_required
        and (depends or adverse_to_us)
        and adverse_to_us
    )
    unmet: list[str] = []
    if not counterparty_required:
        unmet.append("没有相对方必要配合义务Evidence")
    if not depends:
        unmet.append("没有我方履行依赖该配合的Evidence")
    if not adverse_to_us:
        unmet.append("没有配合不足导致我方承担不合理后果的Evidence")
    return Po003CandidatePrecondition(
        counterparty_cooperation_required=counterparty_required,
        performance_depends_on_cooperation=depends,
        adverse_consequence_to_our_party=adverse_to_us,
        relief_or_adjustment_missing=adjustment_missing,
        model_review_required=model_review_required,
        supporting_source_ids=counterparty_source_ids,
        unmet_conditions=unmet,
    )


def _po_partition_primary_source_ids(
    request: GenericReviewRequest,
    *,
    candidate_type: str,
    primary_source_ids: list[str],
) -> tuple[list[str], list[str]]:
    core_patterns: dict[str, tuple[str, ...]] = {
        "SCOPE_EXPANSION": (
            r"按.{0,10}要求",
            r"提出.{0,8}要求",
            r"其[他它]要求",
            r"包括但不限于",
            r"随时.{0,8}(增加|扩大|调整)",
            r"单方.{0,8}(增加|扩大|调整)",
            r"尽量满足",
        ),
        "DELIVERY_SCHEDULE_REVIEW": (
            r"交付",
            r"完成.{0,8}(项目|任务|服务|工作)",
        ),
    }
    patterns = core_patterns.get(candidate_type)
    if patterns is None:
        return list(primary_source_ids), []
    sources = {item.source_id: item for item in request.evidence_sources}
    core = [
        source_id
        for source_id in primary_source_ids
        if source_id in sources
        and any(
            re.search(
                pattern,
                " ".join(
                    filter(
                        None,
                        (
                            sources[source_id].subject,
                            sources[source_id].predicate,
                            sources[source_id].object,
                            sources[source_id].quoted_text,
                        ),
                    )
                ),
            )
            for pattern in patterns
        )
    ]
    if not core:
        raise DirectReviewError(
            "RISK_CANDIDATE_CORE_EVIDENCE_INVALID",
            f"{candidate_type} has no deterministic Core Primary Evidence",
        )
    core_set = set(core)
    context = [
        source_id for source_id in primary_source_ids if source_id not in core_set
    ]
    return core, context


def _po_evidence_deterministic_severity_factors(
    request: GenericReviewRequest,
    *,
    candidate_type: str,
    core_primary_source_ids: list[str],
) -> list[str]:
    """Derive only literal, evidence-provable severity factors.

    These factors must not depend on a model interpretation.  In particular,
    a delivery Candidate that is already backed by Core Primary Evidence
    containing an explicit schedule expression always carries
    ``SCHEDULE_IMPACT`` even if a model omits that factor in one run.
    """

    evidence_factor_patterns: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
        "DELIVERY_SCHEDULE_REVIEW": (
            (
                "SCHEDULE_IMPACT",
                re.compile(
                    r"(时间节点|履行期限|交付期限|完成期限|合同期限|工期|进度|"
                    r"按时|及时|延迟|延期|逾期|工作日|日内|日前|"
                    r"交付时间|完成时间|交付日|完成日)"
                ),
            ),
        ),
    }
    factor_patterns = evidence_factor_patterns.get(candidate_type, ())
    if not factor_patterns:
        return []
    sources = {item.source_id: item for item in request.evidence_sources}
    core_texts = [
        " ".join(
            filter(
                None,
                (
                    source.subject,
                    source.predicate,
                    source.object,
                    source.quoted_text,
                ),
            )
        )
        for source_id in core_primary_source_ids
        if (source := sources.get(source_id)) is not None
    ]
    return [
        factor_code
        for factor_code, pattern in factor_patterns
        if any(pattern.search(text) for text in core_texts)
    ]


def _po_allowed_counter_source_ids(
    request: GenericReviewRequest,
    *,
    check_code: str,
    candidate_type: str,
    primary_source_ids: list[str],
    all_check_source_ids: list[str],
) -> list[str]:
    source_by_id = {
        item.source_id: item
        for item in request.evidence_sources
        if check_code in item.allowed_check_codes
    }
    primary = [
        source_id
        for source_id in primary_source_ids
        if source_id in source_by_id
    ]
    if candidate_type == "QUALITY_STANDARD_UNMEASURABLE":
        objective_pattern = re.compile(
            r"(验收|合格|准确率|覆盖率|可用率|响应时限|服务级别|SLA)"
            r".{0,30}(\d|百分之)|"
            r"(\d|百分之).{0,30}"
            r"(验收|合格|准确率|覆盖率|可用率|响应时限|服务级别|SLA)",
            re.IGNORECASE,
        )
        return [
            source_id
            for source_id in all_check_source_ids
            if source_id in source_by_id
            and objective_pattern.search(source_by_id[source_id].quoted_text)
        ]
    if candidate_type in {
        "ACCEPTANCE_MECHANISM_REVIEW",
        "ACCEPTANCE_MECHANISM_ABSENT",
    }:
        acceptance_pattern = re.compile(
            r"验收.{0,40}(标准|期限|程序|整改|复验|合格)|"
            r"(标准|期限|程序|整改|复验|合格).{0,40}验收"
        )
        return [
            source_id
            for source_id in all_check_source_ids
            if source_id in source_by_id
            and (
                source_by_id[source_id].ir_type == "acceptance_terms"
                or acceptance_pattern.search(source_by_id[source_id].quoted_text)
            )
        ]
    if candidate_type in {
        "CHANGE_CONTROL_REVIEW",
        "CHANGE_CONTROL_ABSENT",
    }:
        bilateral_pattern = re.compile(
            r"双方.{0,20}(书面|确认).{0,30}(变更|调整)|"
            r"(变更|调整).{0,30}双方.{0,20}(书面|确认)"
        )
        return list(
            dict.fromkeys(
                [
                    *primary,
                    *(
                        source_id
                        for source_id in all_check_source_ids
                        if source_id in source_by_id
                        and bilateral_pattern.search(
                            source_by_id[source_id].quoted_text
                        )
                    ),
                ]
            )
        )
    if candidate_type in {
        "COOPERATION_OBLIGATION_ABSENT",
        "WARRANTY_SUPPORT_ABSENT",
    }:
        expected_ir_types = {
            "COOPERATION_OBLIGATION_ABSENT": {"obligations"},
            "WARRANTY_SUPPORT_ABSENT": {
                "obligations",
                "dates",
                "acceptance_terms",
            },
        }[candidate_type]
        return [
            source_id
            for source_id in all_check_source_ids
            if source_id in source_by_id
            and source_by_id[source_id].ir_type in expected_ir_types
        ]
    return primary


def _generic_repair_type(
    error: DirectReviewError,
    po_catalog: PoEvidenceCatalog | None,
) -> Literal["SCHEMA_REPAIR", "EVIDENCE_SELECTION_REPAIR"]:
    if po_catalog is not None and error.code in {
        "RISK_EVIDENCE_SOURCE_NOT_ALLOWED",
        "RISK_EVIDENCE_SOURCE_UNKNOWN",
        "RISK_EVIDENCE_SOURCE_MISSING",
    }:
        return "EVIDENCE_SELECTION_REPAIR"
    return "SCHEMA_REPAIR"


def _generic_repair_payload(
    *,
    repair_type: Literal["SCHEMA_REPAIR", "EVIDENCE_SELECTION_REPAIR"],
    invalid_content: str,
    invalid_reason: str,
    expected_codes: tuple[str, ...],
    po_catalog: PoEvidenceCatalog | None,
) -> dict[str, Any]:
    common = {
        "task": repair_type,
        "first_raw_json": invalid_content,
        "exact_validation_error": invalid_reason,
        "required_check_codes": list(expected_codes),
        "allowed_top_level": ["check_results"],
        "required_check_fields": [
            "check_code",
            "status",
            "decision_note",
            "findings",
        ],
    }
    if repair_type == "EVIDENCE_SELECTION_REPAIR":
        if po_catalog is None:
            raise DirectReviewError(
                "RISK_REPAIR_MODE_INVALID",
                "Evidence Selection Repair is available only to Source-backed PO",
            )
        common["allowed_evidence_source_ids_by_check"] = {
            check_code: list(source_ids)
            for check_code, source_ids in (
                po_catalog.allowed_source_ids_by_check.items()
            )
        }
        common["constraints"] = [
            "只修改非法、未知、缺失或越界的evidence_source_ids",
            "不得新增、删除、改序或改写Check和Finding",
            "不得改变status、decision_note、check_code、category、risk_type、risk_level",
            "不得改变title、issue、impact_to_our_party或suggestion的核心含义",
            "每个Source必须从对应check_code允许列表逐字选择",
            "不得构造、猜测或改写source_id",
            "顶层只能是check_results",
        ]
        return common
    source_constraint = (
        "PO Finding必须保留原evidence_source_ids及顺序"
        if po_catalog is not None
        else "每条Evidence必须补齐evidence_type且保留原ir_ref/evidence_ref及顺序"
    )
    common["constraints"] = [
        "只修复JSON结构和Schema错误，不重新审查合同",
        "保留全部check_code、status、Finding和Evidence",
        "不得新增、删除、改挂或改写风险",
        "不得改变风险等级、Evidence或decision_note",
        "不得新增、删除或改变assessment_type和external_verification_required",
        "reason_code由Python生成，模型删除该字段即可",
        "FVA-002必须保留首轮assessment_type和external_verification_required",
        source_constraint,
        "顶层只能是check_results",
    ]
    return common


def _parse_generic_output(
    content: str,
    expected_codes: tuple[str, ...],
) -> GenericParsedOutput:
    raw_object: dict[str, Any] | None = None
    try:
        parsed = parse_json_output(content)
        if not parsed.ok or not isinstance(parsed.structured, dict):
            raise ValueError("model did not return one complete JSON object")
        raw_object = parsed.structured
        normalization = SchemaNormalizationRecord(applied=False)
        try:
            response = GenericModelResponseRaw.model_validate(raw_object)
        except ValidationError as original_error:
            normalized = _normalize_generic_schema(raw_object, expected_codes)
            if normalized is None:
                raise original_error
            response = GenericModelResponseRaw.model_validate(normalized)
            normalization = SchemaNormalizationRecord(
                applied=True,
                normalization_type="TOP_LEVEL_CHECKS_TO_CHECK_RESULTS",
            )
        codes = [item.check_code for item in response.check_results]
        if tuple(codes) != expected_codes:
            raise DirectReviewError(
                "RISK_CHECK_COVERAGE_INVALID",
                "Direct Review must return assigned Check codes once in input order",
                repairable=True,
                structured_output=raw_object,
            )
        return GenericParsedOutput(response, raw_object, normalization)
    except DirectReviewError:
        raise
    except (ValueError, TypeError, ValidationError, json.JSONDecodeError) as exc:
        raise DirectReviewError(
            "RISK_DIRECT_SCHEMA_INVALID",
            f"Generic Direct Review output is invalid: {exc}",
            repairable=True,
            structured_output=raw_object,
        ) from exc


def _normalize_generic_schema(
    value: dict[str, Any],
    expected_codes: tuple[str, ...],
) -> dict[str, Any] | None:
    if set(value) != {"checks"} or not isinstance(value["checks"], list):
        return None
    try:
        checks = [
            GenericModelCheckResultRaw.model_validate(item)
            for item in value["checks"]
        ]
    except (TypeError, ValidationError):
        return None
    if tuple(item.check_code for item in checks) != expected_codes:
        return None
    normalized = {"check_results": value["checks"]}
    try:
        GenericModelResponseRaw.model_validate(normalized)
    except ValidationError:
        return None
    return normalized


def _materialize_generic(
    request: GenericReviewRequest,
    response: GenericModelResponseRaw,
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
) -> tuple[
    list[CheckCoverageResult],
    list[FindingDraft],
    int,
    list[FvaAssessmentResult],
    int,
    int,
    list[DeterministicFindingEnrichment],
]:
    specs = {item.check_code: item for item in request.assigned_check_specs}
    anchor_ref_by_id = {
        item.anchor_id: ref for ref, item in anchor_refs.items()
    }
    candidates = _build_generic_candidates(
        request,
        ir_refs,
        anchor_ref_by_id,
    )
    candidates_by_id = {item.candidate_id: item for item in candidates}
    if len(candidates_by_id) != len(candidates):
        raise DirectReviewError(
            "RISK_CANDIDATE_DUPLICATED",
            "Deterministic candidate IDs must be unique",
        )
    po_catalog: PoEvidenceCatalog | None = None
    if request.unit_id == "performance_obligations":
        po_catalog = _po_evidence_catalog(
            request,
            candidates,
            ir_refs,
            anchor_refs,
        )
    expected_codes = tuple(specs)
    by_code: dict[str, GenericModelCheckResultRaw] = {}
    ignored_reason_codes = 0
    for check in response.check_results:
        if check.check_code in by_code:
            raise DirectReviewError(
                "RISK_CHECK_DUPLICATED",
                f"Duplicate Check result: {check.check_code}",
                repairable=True,
                structured_output=response.model_dump(mode="json", exclude_unset=True),
            )
        by_code[check.check_code] = check
        if "reason_code" in check.model_fields_set:
            ignored_reason_codes += 1
    if tuple(by_code) != expected_codes:
        raise DirectReviewError(
            "RISK_CHECK_COVERAGE_INVALID",
            "Batch Check coverage is incomplete or out of order",
            repairable=True,
            structured_output=response.model_dump(mode="json", exclude_unset=True),
        )

    coverage: list[CheckCoverageResult] = []
    findings: list[FindingDraft] = []
    fva_assessments: list[FvaAssessmentResult] = []
    deterministic_enrichments: list[DeterministicFindingEnrichment] = []
    evidence_binding_normalization_count = 0
    ignored_model_link_fields_count = 0
    for check_code in expected_codes:
        check = by_code[check_code]
        local_ids = []
        for model_finding in check.findings:
            spec = specs[check_code]
            resolution = _resolve_finding_fields(
                parent_check_code=check_code,
                spec=spec,
                value=model_finding,
                candidates_by_id=candidates_by_id,
            )
            (
                finding,
                binding_normalization_count,
                ignored_link_fields_count,
            ) = _generic_finding(
                request,
                resolution.value,
                ir_refs,
                anchor_refs,
                po_catalog,
            )
            deterministic_enrichments.append(
                DeterministicFindingEnrichment(
                    finding_local_id=finding.finding_local_id,
                    check_code=finding.check_code,
                    candidate_ids=resolution.value.candidate_ids,
                    check_code_source="PARENT_CHECK",
                    category_source="REGISTRY",
                    risk_type_source=resolution.risk_type_source,
                    check_code_enriched=resolution.check_code_enriched,
                    category_enriched=resolution.category_enriched,
                    risk_type_enriched=resolution.risk_type_enriched,
                    ignored_model_check_code=resolution.ignored_model_check_code,
                    ignored_model_category=resolution.ignored_model_category,
                )
            )
            evidence_binding_normalization_count += binding_normalization_count
            ignored_model_link_fields_count += ignored_link_fields_count
            _validate_domain_safety(finding)
            findings.append(finding)
            local_ids.append(finding.finding_local_id)
        if check.findings and check.status != "REVIEWED":
            raise DirectReviewError(
                "RISK_CHECK_STATUS_INVALID",
                "A Check with Findings must be REVIEWED",
                repairable=True,
            )
        if check_code == "FVA-002":
            fva_assessment = _validate_fva002_assessment(
                check,
                [
                    item
                    for item in findings
                    if item.check_code == "FVA-002"
                ],
                request,
            )
            fva_assessments.append(fva_assessment)
        if check.status == "REVIEWED":
            if (
                check_code == "FVA-002"
                and check.assessment_type == "EXTERNAL_VERIFICATION_REQUIRED"
            ):
                reason_code = "INSUFFICIENT_EVIDENCE"
            else:
                reason_code = (
                    "RISK_IDENTIFIED" if check.findings else "NO_RISK_IDENTIFIED"
                )
        elif check.status == "NOT_APPLICABLE":
            reason_code = "NOT_APPLICABLE"
        else:
            reason_code = "CHECK_FAILED"
        coverage.append(
            CheckCoverageResult(
                check_code=check_code,
                status=check.status,
                reason_code=reason_code,
                decision_note=check.decision_note,
                finding_local_ids=local_ids,
            )
        )
    finding_ids = [item.finding_local_id for item in findings]
    evidence_ids = [
        evidence.evidence_local_id
        for finding in findings
        for evidence in finding.evidence_candidates
    ]
    if len(finding_ids) != len(set(finding_ids)) or len(evidence_ids) != len(
        set(evidence_ids)
    ):
        raise DirectReviewError(
            "RISK_FINDING_DUPLICATED",
            "Direct Review returned duplicate material Findings or Evidence",
            repairable=True,
        )
    return (
        coverage,
        findings,
        ignored_reason_codes,
        fva_assessments,
        evidence_binding_normalization_count,
        ignored_model_link_fields_count,
        deterministic_enrichments,
    )


def _resolve_finding_fields(
    *,
    parent_check_code: str,
    spec: GenericCheckSpec,
    value: GenericModelFindingDraft,
    candidates_by_id: dict[str, DeterministicRiskCandidate],
) -> FindingFieldResolution:
    if value.check_code is not None and value.check_code != parent_check_code:
        raise DirectReviewError(
            "RISK_FINDING_CHECK_CONFLICT",
            "Finding check_code conflicts with its parent Check",
        )

    registry_category = spec.allowed_categories[0]
    if value.category is not None and value.category != registry_category:
        raise DirectReviewError(
            "RISK_FINDING_CATEGORY_CONFLICT",
            "Finding category conflicts with the Registry mapping",
        )

    if len(value.candidate_ids) != len(set(value.candidate_ids)):
        raise DirectReviewError(
            "RISK_CANDIDATE_DUPLICATED",
            "Finding candidate_ids must not contain duplicates",
        )
    selected_candidates: list[DeterministicRiskCandidate] = []
    for candidate_id in value.candidate_ids:
        candidate = candidates_by_id.get(candidate_id)
        if candidate is None:
            raise DirectReviewError(
                "RISK_CANDIDATE_UNKNOWN",
                "Finding references an unknown deterministic candidate",
            )
        if candidate.check_code != parent_check_code:
            raise DirectReviewError(
                "RISK_CANDIDATE_CHECK_CONFLICT",
                "Finding candidate belongs to a different Check",
            )
        selected_candidates.append(candidate)

    allowed_risk_types = tuple(spec.allowed_risk_types)
    if value.risk_type is not None and value.risk_type not in allowed_risk_types:
        raise DirectReviewError(
            "RISK_FINDING_TYPE_INVALID",
            "Finding risk_type is not allowed by its CheckSpec",
            repairable=True,
        )

    candidate_type_count = len(
        {item.candidate_type for item in selected_candidates}
    )
    candidate_risk_type: str | None = None
    if selected_candidates and candidate_type_count == 1:
        candidate_type = selected_candidates[0].candidate_type
        if candidate_type in allowed_risk_types:
            candidate_risk_type = candidate_type
        elif len(allowed_risk_types) == 1:
            # Current frozen PO Registry uses a domain candidate type and one
            # formal risk type per Check. The Registry mapping is authoritative.
            candidate_risk_type = allowed_risk_types[0]

    if (
        value.risk_type is not None
        and candidate_risk_type is not None
        and value.risk_type != candidate_risk_type
    ):
        raise DirectReviewError(
            "RISK_TYPE_CONFLICT",
            "Model risk_type conflicts with the selected deterministic candidate",
        )

    if value.risk_type is not None:
        risk_type = value.risk_type
        risk_type_source: Literal[
            "MODEL", "CANDIDATE", "UNIQUE_ALLOWED_TYPE"
        ] = "MODEL"
    elif candidate_risk_type is not None:
        risk_type = candidate_risk_type
        risk_type_source = "CANDIDATE"
    elif not selected_candidates and len(allowed_risk_types) == 1:
        risk_type = allowed_risk_types[0]
        risk_type_source = "UNIQUE_ALLOWED_TYPE"
    else:
        reason = (
            "Selected candidates have different candidate_type values"
            if candidate_type_count > 1
            else "Check has multiple allowed risk types and no unique candidate"
        )
        raise DirectReviewError(
            "RISK_TYPE_REQUIRED",
            f"{reason}; the model must select risk_type explicitly",
            repairable=True,
        )

    return FindingFieldResolution(
        value=value.model_copy(
            update={
                "check_code": parent_check_code,
                "category": registry_category,
                "risk_type": risk_type,
            }
        ),
        risk_type_source=risk_type_source,
        check_code_enriched=value.check_code is None,
        category_enriched=value.category is None,
        risk_type_enriched=value.risk_type is None,
        ignored_model_check_code=value.check_code is not None,
        ignored_model_category=value.category is not None,
    )


def _validate_fva002_assessment(
    check: GenericModelCheckResultRaw,
    findings: list[FindingDraft],
    request: GenericReviewRequest,
) -> FvaAssessmentResult:
    assessment = check.assessment_type
    external_required = check.external_verification_required
    if assessment is None or external_required is None:  # Raw validator guard
        raise DirectReviewError(
            "RISK_FVA002_ASSESSMENT_MISSING",
            "FVA-002 assessment fields are required",
            repairable=True,
        )
    if check.status != "REVIEWED":
        raise DirectReviewError(
            "RISK_FVA002_STATUS_INVALID",
            "FVA-002 tri-state assessment must use status=REVIEWED",
            repairable=True,
        )
    if any(word in check.decision_note for word in FVA002_OUT_OF_SCOPE_WORDS):
        raise DirectReviewError(
            "RISK_FVA002_SCOPE_LEAKAGE",
            "FVA-002 cannot use personnel qualification, employment, or "
            "performance-capability material",
        )
    fva002_policy = next(
        (
            item
            for item in request.check_evidence_policies
            if item.check_code == "FVA-002"
        ),
        None,
    )
    source_by_id = {item.source_id: item for item in request.evidence_sources}
    textual_conflict_visible = fva002_policy is not None and any(
        re.search(
            r"(无权|越权|无授权|授权范围不足|"
            r"签署主体.{0,12}(不一致|冲突)|"
            r"代表人.{0,12}(不一致|冲突))",
            source_by_id[source_id].quoted_text,
        )
        for source_id in fva002_policy.allowed_evidence_source_ids
        if source_id in source_by_id
    )
    if (
        fva002_policy is not None
        and textual_conflict_visible != (assessment == "TEXTUAL_AUTHORITY_RISK")
    ):
        raise DirectReviewError(
            "RISK_FVA002_PRECONDITION_MISMATCH",
            "FVA-002 assessment conflicts with the deterministic textual-authority gate",
            repairable=True,
        )
    if assessment == "TEXTUAL_AUTHORITY_RISK":
        if external_required or not findings:
            raise DirectReviewError(
                "RISK_FVA002_STATE_INCONSISTENT",
                "TEXTUAL_AUTHORITY_RISK requires source-backed Findings and no "
                "external-verification flag",
                repairable=True,
            )
        if any(
            all(
                item.evidence_type == "ABSENCE"
                for item in finding.evidence_candidates
            )
            for finding in findings
        ):
            raise DirectReviewError(
                "RISK_FVA002_TEXT_EVIDENCE_REQUIRED",
                "TEXTUAL_AUTHORITY_RISK requires current-contract text Evidence",
                repairable=True,
            )
    elif assessment == "EXTERNAL_VERIFICATION_REQUIRED":
        if not external_required or findings:
            raise DirectReviewError(
                "RISK_FVA002_STATE_INCONSISTENT",
                "EXTERNAL_VERIFICATION_REQUIRED requires no Finding and "
                "external_verification_required=true",
                repairable=True,
            )
        if not any(
            word in check.decision_note
            for word in FVA002_EXTERNAL_MATERIAL_WORDS
        ):
            raise DirectReviewError(
                "RISK_FVA002_EXTERNAL_MATERIAL_UNSPECIFIED",
                "External-verification decision must name the material to verify",
                repairable=True,
            )
    elif assessment == "NO_VISIBLE_ISSUE":
        if external_required or findings:
            raise DirectReviewError(
                "RISK_FVA002_STATE_INCONSISTENT",
                "NO_VISIBLE_ISSUE requires no Finding and no external-verification flag",
                repairable=True,
            )
        if any(
            phrase in check.decision_note
            for phrase in ("已核验授权", "已确认授权", "已查验授权")
        ):
            raise DirectReviewError(
                "RISK_FVA_EXTERNAL_FACT_ASSERTED",
                "NO_VISIBLE_ISSUE cannot claim that external authorization was verified",
                repairable=True,
            )
    return FvaAssessmentResult(
        check_code="FVA-002",
        assessment_type=assessment,
        external_verification_required=external_required,
    )


def _generic_finding(
    request: GenericReviewRequest,
    value: GenericModelFindingDraft,
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
    po_catalog: PoEvidenceCatalog | None,
    *,
    po_allowed_source_ids: list[str] | None = None,
) -> tuple[FindingDraft, int, int]:
    binding_normalization_count = 0
    ignored_link_fields_count = 0
    po_source_ids: list[str] = []
    if po_catalog is not None:
        (
            po_source_ids,
            binding_normalization_count,
            ignored_link_fields_count,
        ) = _resolve_po_evidence_source_ids(
            value,
            po_catalog,
            ir_refs,
            anchor_refs,
            allowed_source_ids=po_allowed_source_ids,
        )
        source_keys = po_source_ids
    else:
        if value.evidence_source_ids:
            raise DirectReviewError(
                "RISK_EVIDENCE_SOURCE_NOT_ALLOWED",
                "This Review Unit does not accept Evidence Source IDs",
                repairable=True,
            )
        if not value.evidence:
            raise DirectReviewError(
                "RISK_EVIDENCE_SOURCE_MISSING",
                "Every substantive Finding requires Evidence",
                repairable=True,
            )
        source_keys = [
            f"{item.ir_ref or '-'}:{item.evidence_ref or '-'}:{item.evidence_type}"
            for item in value.evidence
        ]
    finding_id = _stable_id(
        "finding",
        {
            "plan_id": request.plan_id,
            "unit_id": request.unit_id,
            "check_code": value.check_code,
            "category": value.category,
            "risk_type": value.risk_type,
            "sources": source_keys,
        },
    )
    if po_catalog is None:
        evidence = [
            _generic_evidence(
                finding_id,
                index,
                item,
                ir_refs,
                anchor_refs,
            )
            for index, item in enumerate(value.evidence, 1)
        ]
    else:
        evidence = [
            _po_evidence_candidate(
                finding_id,
                index,
                source_id,
                po_catalog,
            )
            for index, source_id in enumerate(po_source_ids, 1)
        ]
    finding = FindingDraft(
        finding_local_id=finding_id,
        source_unit_id=request.unit_id,
        domain=request.unit_id,
        check_code=value.check_code,
        category=value.category,
        risk_type=value.risk_type,
        risk_level=value.risk_level,
        title=value.title,
        issue=value.issue,
        impact_to_our_party=value.impact_to_our_party,
        suggestion=value.suggestion,
        perspective=request.perspective,
        our_party=request.our_party,
        counterparty=request.counterparty,
        evidence_candidates=evidence,
    )
    return (
        finding,
        binding_normalization_count,
        ignored_link_fields_count,
    )


def _resolve_po_evidence_source_ids(
    value: GenericModelFindingDraft,
    catalog: PoEvidenceCatalog,
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
    *,
    allowed_source_ids: list[str] | None = None,
) -> tuple[list[str], int, int]:
    allowed = set(
        allowed_source_ids
        if allowed_source_ids is not None
        else catalog.allowed_source_ids_by_check.get(value.check_code, ())
    )
    if value.evidence_source_ids and value.evidence:
        raise DirectReviewError(
            "RISK_EVIDENCE_SOURCE_NOT_ALLOWED",
            "PO Finding cannot mix Source IDs with legacy IR/Anchor Evidence",
            repairable=True,
        )
    normalization_count = 0
    ignored_model_link_fields_count = 0
    selected: list[str] = []
    if value.evidence_source_ids:
        selected = list(value.evidence_source_ids)
    elif value.evidence:
        selected_ir_refs = {
            item.ir_ref for item in value.evidence if item.ir_ref is not None
        }
        selected_ir_anchor_ids = {
            anchor.anchor_id
            for ir_ref in selected_ir_refs
            if ir_ref in ir_refs
            for anchor in ir_refs[ir_ref].source_anchors
        }
        for legacy in value.evidence:
            ignored_model_link_fields_count += sum(
                field is not None
                for field in (
                    legacy.ir_ref,
                    legacy.evidence_ref,
                    legacy.checked_scope,
                    legacy.verification_note,
                )
            )
            if legacy.evidence_type == "ABSENCE":
                allowed_absence = [
                    source_id
                    for source_id in allowed
                    if source_id in catalog.absence_sources
                ]
                if len(allowed_absence) != 1:
                    raise DirectReviewError(
                        "RISK_EVIDENCE_SOURCE_NOT_ALLOWED",
                        "Legacy ABSENCE Evidence does not resolve to one allowed Source",
                        repairable=True,
                    )
                selected.append(allowed_absence[0])
                normalization_count += 1
                continue
            item = ir_refs.get(legacy.ir_ref or "")
            excerpt = anchor_refs.get(legacy.evidence_ref or "")
            exact = [
                source.source_id
                for source in catalog.evidence_sources.values()
                if item is not None
                and excerpt is not None
                and source.ir_item_id == item.item_id
                and source.anchor_id == excerpt.anchor_id
                and source.source_id in allowed
            ]
            if len(exact) == 1:
                selected.append(exact[0])
                normalization_count += 1
                continue
            unique_for_ir = [
                source.source_id
                for source in catalog.evidence_sources.values()
                if item is not None
                and source.ir_item_id == item.item_id
                and source.source_id in allowed
            ]
            incompatible_anchor = (
                excerpt is not None
                and excerpt.anchor_id not in selected_ir_anchor_ids
            )
            if len(unique_for_ir) == 1 and incompatible_anchor:
                selected.append(unique_for_ir[0])
                normalization_count += 1
                continue
            raise DirectReviewError(
                "RISK_EVIDENCE_SOURCE_NOT_ALLOWED",
                "Legacy IR/Anchor Evidence cannot be deterministically bound "
                "to one allowed Source",
                repairable=True,
            )
    if not selected:
        raise DirectReviewError(
            "RISK_EVIDENCE_SOURCE_NOT_ALLOWED",
            "Every PO Finding must select at least one Evidence Source",
            repairable=True,
        )
    deduplicated = list(dict.fromkeys(selected))
    normalization_count += len(selected) - len(deduplicated)
    invalid = [source_id for source_id in deduplicated if source_id not in allowed]
    if invalid:
        raise DirectReviewError(
            "RISK_EVIDENCE_SOURCE_NOT_ALLOWED",
            "PO Finding selected an unknown, cross-Batch or disallowed Evidence Source",
            repairable=True,
        )
    return (
        deduplicated,
        normalization_count,
        ignored_model_link_fields_count,
    )


def _po_evidence_candidate(
    finding_id: str,
    index: int,
    source_id: str,
    catalog: PoEvidenceCatalog,
) -> EvidenceCandidate:
    source = catalog.evidence_sources.get(source_id)
    absence = catalog.absence_sources.get(source_id)
    evidence_id = _stable_id(
        "evidence",
        {
            "finding_id": finding_id,
            "index": index,
            "source_id": source_id,
        },
    )
    if source is not None:
        return EvidenceCandidate(
            evidence_local_id=evidence_id,
            finding_local_id=finding_id,
            evidence_type=source.evidence_type,
            source_ir_item_id=source.ir_item_id,
            anchor_id=source.anchor_id,
            block_id=source.block_id,
            page_number=source.page_number,
            char_start=source.char_start,
            char_end=source.char_end,
            quoted_text=source.quoted_text,
            quoted_text_hash=source.quoted_text_hash,
        )
    if absence is not None:
        return EvidenceCandidate(
            evidence_local_id=evidence_id,
            finding_local_id=finding_id,
            evidence_type="ABSENCE",
            checked_scope=absence.checked_scope,
            verification_note=absence.verification_method,
        )
    raise DirectReviewError(
        "RISK_EVIDENCE_SOURCE_NOT_ALLOWED",
        "Evidence Source cannot be resolved",
        repairable=True,
    )


def _selected_po_source_ids(
    findings: list[FindingDraft],
    catalog: PoEvidenceCatalog | None,
) -> list[str]:
    if catalog is None:
        return []
    text_by_binding = {
        (
            item.ir_item_id,
            item.anchor_id,
            item.evidence_type,
        ): item.source_id
        for item in catalog.evidence_sources.values()
    }
    absence_by_scope = {
        (item.checked_scope, item.verification_method): item.source_id
        for item in catalog.absence_sources.values()
    }
    result = []
    for finding in findings:
        for evidence in finding.evidence_candidates:
            if evidence.evidence_type == "ABSENCE":
                source_id = absence_by_scope.get(
                    (evidence.checked_scope, evidence.verification_note)
                )
            else:
                source_id = text_by_binding.get(
                    (
                        evidence.source_ir_item_id,
                        evidence.anchor_id,
                        evidence.evidence_type,
                    )
                )
            if source_id is None:
                raise DirectReviewError(
                    "RISK_EVIDENCE_SOURCE_NOT_ALLOWED",
                    "Final Evidence cannot be mapped back to one Source ID",
                )
            result.append(source_id)
    return list(dict.fromkeys(result))


def _generic_evidence(
    finding_id: str,
    index: int,
    value: GenericModelEvidenceDraft,
    ir_refs: dict[str, CommercialIrItem],
    anchor_refs: dict[str, CommercialSourceExcerpt],
) -> EvidenceCandidate:
    evidence_id = _stable_id(
        "evidence",
        {
            "finding_id": finding_id,
            "index": index,
            "type": value.evidence_type,
            "ir_ref": value.ir_ref,
            "evidence_ref": value.evidence_ref,
        },
    )
    if value.evidence_type == "ABSENCE":
        return EvidenceCandidate(
            evidence_local_id=evidence_id,
            finding_local_id=finding_id,
            evidence_type="ABSENCE",
            checked_scope=value.checked_scope,
            verification_note=value.verification_note,
        )
    item = ir_refs.get(value.ir_ref or "")
    excerpt = anchor_refs.get(value.evidence_ref or "")
    if item is None:
        raise DirectReviewError(
            "RISK_EVIDENCE_IR_UNKNOWN",
            "Evidence references an unknown IR item",
            repairable=True,
        )
    if excerpt is None:
        raise DirectReviewError(
            "RISK_EVIDENCE_ANCHOR_UNKNOWN",
            "Evidence references an unknown Anchor",
            repairable=True,
        )
    if excerpt.anchor_id not in {anchor.anchor_id for anchor in item.source_anchors}:
        raise DirectReviewError(
            "RISK_EVIDENCE_LINK_INVALID",
            "Evidence Anchor does not belong to the referenced IR item",
            repairable=True,
        )
    return EvidenceCandidate(
        evidence_local_id=evidence_id,
        finding_local_id=finding_id,
        evidence_type=value.evidence_type,
        source_ir_item_id=item.item_id,
        anchor_id=excerpt.anchor_id,
        block_id=excerpt.block_id,
        page_number=excerpt.page_number,
        char_start=excerpt.char_start,
        char_end=excerpt.char_end,
        quoted_text=excerpt.quoted_text,
        quoted_text_hash=excerpt.quoted_text_hash,
    )


def _validate_domain_safety(finding: FindingDraft) -> None:
    if finding.check_code == "FVA-005" and (
        finding.category != "OTHER"
        or finding.risk_type != "MANDATORY_RULE_OR_VALIDITY_RISK"
    ):
        raise DirectReviewError(
            "RISK_FVA005_TYPE_INVALID",
            "FVA-005 must use the frozen OTHER validity-risk mapping",
            repairable=True,
        )
    if finding.check_code == "FVA-002":
        combined = "".join(
            (
                finding.title,
                finding.issue,
                finding.impact_to_our_party,
                finding.suggestion,
            )
        )
        unsupported_assertions = (
            "确定无权",
            "确认无权",
            "必然无效",
            "当然无效",
            "主体不适格",
            "确定未授权",
            "现实中无权",
            "现实中未授权",
        )
        if any(word in combined for word in unsupported_assertions):
            raise DirectReviewError(
                "RISK_FVA_EXTERNAL_FACT_ASSERTED",
                "FVA-002 cannot assert unseen external authorization facts",
                repairable=True,
            )
    if finding.check_code.startswith("PO-"):
        _validate_po_domain_safety(finding)
    if finding.check_code.startswith("ICD-"):
        _validate_icd_domain_safety(finding)


def _validate_po_domain_safety(finding: FindingDraft) -> None:
    # Domain ownership is determined by the assigned CheckSpec, risk_type and
    # already validated Evidence Sources. Remediation wording in suggestion
    # (for example a confidentiality guard around an inspection right) must
    # not create a new risk root.
    core_risk = "".join((finding.title, finding.issue))
    forbidden_groups = {
        "AUTHORITY": (
            "法定代表人",
            "授权委托",
            "签署效力",
            "主体资格",
            "盖章效力",
            "无权代理",
        ),
        "EXTERNAL_QUALIFICATION": (
            "劳动合同",
            "社会保险",
            "社保",
            "上岗资质",
            "人员资质",
            "履约资质",
        ),
        "IP_CONFIDENTIALITY_DATA": (
            "知识产权",
            "著作权",
            "专利权",
            "保密义务",
            "保密信息",
            "个人信息",
            "数据安全",
            "数据返还",
        ),
        "LIABILITY_REMEDIES_EXIT": (
            "赔偿上限",
            "责任上限",
            "间接损失",
            "解除权",
            "终止权",
            "争议解决",
            "管辖法院",
            "仲裁机构",
        ),
    }
    for boundary, terms in forbidden_groups.items():
        if any(term in core_risk for term in terms):
            raise DirectReviewError(
                "RISK_PO_DOMAIN_LEAKAGE",
                f"PO Finding crossed into {boundary}",
                repairable=True,
            )
    commercial_terms = ("发票", "税费", "税率", "付款期限", "预付款", "保证金", "结算账户")
    change_terms = ("变更", "调整", "新增", "额外", "范围", "工期")
    if any(term in core_risk for term in commercial_terms) and not (
        finding.check_code == "PO-006"
        and any(term in core_risk for term in change_terms)
    ):
        raise DirectReviewError(
            "RISK_PO_DOMAIN_LEAKAGE",
            "PO Finding crossed into pure payment, invoice, or tax review",
            repairable=True,
        )


def _validate_icd_domain_safety(finding: FindingDraft) -> None:
    """Keep the ICD risk root in IP, confidentiality, data, or security."""
    core_risk = "".join((finding.title, finding.issue))
    required_terms = (
        "知识产权",
        "著作权",
        "成果权",
        "许可",
        "保密",
        "秘密",
        "数据",
        "信息安全",
        "第三方权利",
    )
    if not any(term in core_risk for term in required_terms):
        raise DirectReviewError(
            "RISK_ICD_DOMAIN_LEAKAGE",
            "ICD Finding does not contain an ICD legal root",
        )
    forbidden_terms = (
        "法定代表人",
        "授权委托",
        "签署效力",
        "主体资格",
        "盖章效力",
        "发票",
        "税费",
        "税率",
        "付款期限",
        "预付款",
        "保证金",
        "验收标准",
        "验收程序",
        "赔偿上限",
        "责任上限",
        "解除权失衡",
        "终止权失衡",
        "劳动合同",
        "社会保险",
        "社保",
        "人员资质",
    )
    if any(term in core_risk for term in forbidden_terms):
        raise DirectReviewError(
            "RISK_ICD_DOMAIN_LEAKAGE",
            "ICD Finding crossed into another Review Unit",
        )
    unsupported_assertions = (
        "已经违法",
        "必然违法",
        "必然受到行政处罚",
        "已经发生数据泄露",
        "确定构成数据泄露",
    )
    if any(term in core_risk for term in unsupported_assertions):
        raise DirectReviewError(
            "RISK_ICD_EXTERNAL_FACT_ASSERTED",
            "ICD Finding cannot assert an unseen violation or security incident",
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _generic_repair_snapshot(
    value: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    checks = value.get("check_results")
    if checks is None:
        checks = value.get("checks")
    if not isinstance(checks, list):
        return None
    snapshot: dict[str, Any] = {}
    for item in checks:
        if not isinstance(item, dict) or not isinstance(
            item.get("check_code"), str
        ):
            return None
        check_code = item["check_code"]
        findings = item.get("findings", [])
        if check_code in snapshot or not isinstance(findings, list):
            return None
        protected_fields = {
            key: item[key]
            for key in ("status", "decision_note")
            if key in item
        }
        if check_code == "FVA-002":
            protected_fields["assessment_type"] = item.get("assessment_type")
            protected_fields["external_verification_required"] = item.get(
                "external_verification_required"
            )
        protected_findings = []
        for finding in findings:
            if not isinstance(finding, dict):
                return None
            evidence = finding.get("evidence", [])
            if not isinstance(evidence, list) or any(
                not isinstance(item, dict) for item in evidence
            ):
                return None
            protected_findings.append(
                {
                    key: (
                        [dict(item) for item in evidence]
                        if key == "evidence"
                        else finding.get(key)
                    )
                    for key in (
                        "check_code",
                        "category",
                        "candidate_ids",
                        "risk_type",
                        "risk_level",
                        "title",
                        "issue",
                        "impact_to_our_party",
                        "suggestion",
                        "evidence",
                        "evidence_source_ids",
                    )
                }
            )
        snapshot[check_code] = {
            "protected_check_fields": protected_fields,
            "findings": protected_findings,
        }
    return snapshot


def _validate_generic_semantic_preservation(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    repair_type: Literal[
        "SCHEMA_REPAIR",
        "EVIDENCE_SELECTION_REPAIR",
    ] = "SCHEMA_REPAIR",
) -> None:
    if before is None or after is None:
        raise DirectReviewError(
            "RISK_REPAIR_SEMANTICS_UNVERIFIABLE",
            "Repair output cannot be accepted because semantic preservation "
            "is unverifiable",
        )
    if set(before) != set(after):
        raise DirectReviewError(
            "RISK_REPAIR_SEMANTICS_CHANGED",
            "Repair changed the set of Check codes",
        )
    for check_code in sorted(before):
        previous = before[check_code]
        current = after[check_code]
        if (
            previous["protected_check_fields"]
            != current["protected_check_fields"]
        ):
            raise DirectReviewError(
                "RISK_REPAIR_SEMANTICS_CHANGED",
                f"Repair changed protected judgment fields for {check_code}",
            )
        previous_findings = previous["findings"]
        current_findings = current["findings"]
        if len(previous_findings) != len(current_findings):
            raise DirectReviewError(
                "RISK_REPAIR_SEMANTICS_CHANGED",
                f"Repair changed Finding count for {check_code}",
            )
        for previous_finding, current_finding in zip(
            previous_findings,
            current_findings,
            strict=True,
        ):
            excluded = {"evidence"}
            if repair_type == "EVIDENCE_SELECTION_REPAIR":
                excluded.add("evidence_source_ids")
            previous_business = {
                key: value
                for key, value in previous_finding.items()
                if key not in excluded
                and key not in {"check_code", "category", "risk_type"}
            }
            current_business = {
                key: value
                for key, value in current_finding.items()
                if key not in excluded
                and key not in {"check_code", "category", "risk_type"}
            }
            if previous_business != current_business:
                raise DirectReviewError(
                    "RISK_REPAIR_SEMANTICS_CHANGED",
                    f"Repair changed Finding substance for {check_code}",
                )
            for deterministic_field in ("check_code", "category", "risk_type"):
                previous_value = previous_finding.get(deterministic_field)
                current_value = current_finding.get(deterministic_field)
                if previous_value is not None and previous_value != current_value:
                    raise DirectReviewError(
                        "RISK_REPAIR_SEMANTICS_CHANGED",
                        f"Repair changed explicit {deterministic_field} for "
                        f"{check_code}",
                    )
                if previous_value is None and current_value is None:
                    continue
            if (
                repair_type == "SCHEMA_REPAIR"
                and previous_finding.get("evidence_source_ids")
                != current_finding.get("evidence_source_ids")
            ):
                raise DirectReviewError(
                    "RISK_REPAIR_SEMANTICS_CHANGED",
                    f"Schema Repair changed Evidence Source for {check_code}",
                )
            previous_evidence = previous_finding["evidence"]
            current_evidence = current_finding["evidence"]
            if len(previous_evidence) != len(current_evidence):
                raise DirectReviewError(
                    "RISK_REPAIR_SEMANTICS_CHANGED",
                    f"Repair changed Evidence count for {check_code}",
                )
            for previous_item, current_item in zip(
                previous_evidence,
                current_evidence,
                strict=True,
            ):
                for field in (
                    "ir_ref",
                    "evidence_ref",
                    "checked_scope",
                    "verification_note",
                ):
                    if previous_item.get(field) != current_item.get(field):
                        raise DirectReviewError(
                            "RISK_REPAIR_SEMANTICS_CHANGED",
                            f"Repair changed Evidence source for {check_code}",
                        )
                previous_type = previous_item.get("evidence_type")
                current_type = current_item.get("evidence_type")
                if previous_type is not None and previous_type != current_type:
                    raise DirectReviewError(
                        "RISK_REPAIR_SEMANTICS_CHANGED",
                        f"Repair changed Evidence type for {check_code}",
                    )
                if previous_type is None:
                    source_backed = (
                        previous_item.get("ir_ref") is not None
                        and previous_item.get("evidence_ref") is not None
                    )
                    allowed = (
                        {"TEXT_QUOTE", "CONTEXT"}
                        if source_backed
                        else {"ABSENCE"}
                    )
                    if current_type not in allowed:
                        raise DirectReviewError(
                            "RISK_REPAIR_SEMANTICS_CHANGED",
                            f"Repair did not apply a compatible structural "
                            f"Evidence type for {check_code}",
                        )


def _generic_validation_errors(error: Exception | None) -> dict[str, str | None]:
    values: dict[str, str | None] = {
        "provider": None,
        "raw_pydantic": None,
        "final_pydantic": None,
        "check_coverage": None,
        "evidence": None,
        "domain_safety": None,
        "semantic_preservation": None,
        "other": None,
    }
    if error is None:
        return values
    message = f"{getattr(error, 'code', type(error).__name__)}: {error}"
    if not isinstance(error, DirectReviewError):
        values["provider"] = message
        return values
    if error.code == "RISK_DIRECT_SCHEMA_INVALID":
        values["raw_pydantic"] = message
    elif error.code in {
        "RISK_CHECK_COVERAGE_INVALID",
        "RISK_CHECK_DUPLICATED",
        "RISK_CHECK_STATUS_INVALID",
        "RISK_REQUIRED_CHECK_FAILED",
    }:
        values["check_coverage"] = message
    elif error.code.startswith("RISK_EVIDENCE_"):
        values["evidence"] = message
    elif error.code in {
        "RISK_FINDING_CHECK_INVALID",
        "RISK_FINDING_CHECK_CONFLICT",
        "RISK_FINDING_CATEGORY_CONFLICT",
        "RISK_FINDING_TYPE_INVALID",
        "RISK_TYPE_CONFLICT",
        "RISK_TYPE_REQUIRED",
        "RISK_CANDIDATE_UNKNOWN",
        "RISK_CANDIDATE_CHECK_CONFLICT",
        "RISK_CANDIDATE_DUPLICATED",
        "RISK_FINDING_DUPLICATED",
    }:
        values["final_pydantic"] = message
    elif error.code.startswith("RISK_FVA"):
        values["domain_safety"] = message
    elif error.code.startswith("RISK_REPAIR_SEMANTICS_"):
        values["semantic_preservation"] = message
    else:
        values["other"] = message
    return values


def _generic_semantic_summary(
    snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    checks: dict[str, Any] = {}
    for check_code, value in sorted(snapshot.items()):
        findings = value["findings"]
        checks[check_code] = {
            "protected_check_fields": value["protected_check_fields"],
            "finding_count": len(findings),
            "evidence_count": sum(
                len(item.get("evidence") or []) for item in findings
            ),
            "findings": [
                {
                    "check_code": item.get("check_code"),
                    "category": item.get("category"),
                    "candidate_ids": item.get("candidate_ids") or [],
                    "risk_type": item.get("risk_type"),
                    "risk_level": item.get("risk_level"),
                    "title": item.get("title"),
                    "evidence": item.get("evidence") or [],
                    "evidence_source_ids": item.get("evidence_source_ids") or [],
                }
                for item in findings
            ],
        }
    return {"checks": checks}


def _generic_semantic_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if before is None and after is None:
        return None
    before_checks = before or {}
    after_checks = after or {}
    changed = [
        check_code
        for check_code in sorted(set(before_checks) | set(after_checks))
        if before_checks.get(check_code) != after_checks.get(check_code)
    ]
    return {
        "added_check_codes": sorted(set(after_checks) - set(before_checks)),
        "removed_check_codes": sorted(set(before_checks) - set(after_checks)),
        "changed_check_codes": changed,
        "semantically_equal": before == after,
    }


def _emit_generic_attempt_artifact(
    sink: GenericAttemptArtifactSink | None,
    *,
    request: GenericReviewRequest,
    repair_no: int,
    started_at: str,
    completion: LlmCompletionResult | None,
    raw_object: dict[str, Any] | None,
    normalization: SchemaNormalizationRecord,
    error: Exception | None,
    repair_type: Literal[
        "SCHEMA_REPAIR",
        "EVIDENCE_SELECTION_REPAIR",
    ] | None,
    repair_reason: str | None,
    before_snapshot: dict[str, Any] | None,
    after_snapshot: dict[str, Any] | None,
    semantic_preservation_passed: bool | None,
    accepted: bool,
    acceptance_reason: str,
    input_diagnostics: dict[str, Any] | None = None,
) -> None:
    if sink is None:
        return
    sink(
        GenericAttemptArtifact(
            unit_id=request.unit_id,
            batch_id=request.batch_id,
            attempt_no=request.attempt_no,
            attempt_type="INITIAL" if repair_no == 0 else "REPAIR",
            repair_type=repair_type if repair_no else None,
            repair_no=repair_no,
            started_at=started_at,
            completed_at=_utc_now(),
            raw_response=completion.content if completion is not None else None,
            extracted_raw_object=raw_object,
            deterministic_normalization=normalization.model_dump(mode="json"),
            validation_errors=_generic_validation_errors(error),
            prompt_tokens=(
                completion.prompt_tokens if completion is not None else None
            ),
            cached_tokens=(
                completion.cached_tokens if completion is not None else None
            ),
            completion_tokens=(
                completion.completion_tokens if completion is not None else None
            ),
            total_tokens=(
                completion.total_tokens if completion is not None else None
            ),
            time_to_first_token_ms=(
                completion.time_to_first_token_ms
                if completion is not None
                else None
            ),
            model_duration_ms=(
                completion.model_duration_ms if completion is not None else None
            ),
            trace_id=completion.trace_id if completion is not None else None,
            provider_request_id=(
                completion.provider_request_id if completion is not None else None
            ),
            finish_reason=(
                completion.finish_reason if completion is not None else None
            ),
            repair_reason=repair_reason,
            before_summary=_generic_semantic_summary(before_snapshot),
            after_summary=_generic_semantic_summary(after_snapshot),
            semantic_diff=_generic_semantic_diff(
                before_snapshot,
                after_snapshot,
            ),
            semantic_preservation_passed=semantic_preservation_passed,
            accepted=accepted,
            acceptance_reason=acceptance_reason,
            input_diagnostics=input_diagnostics,
        )
    )


def _generic_metric(value: LlmCompletionResult, unit_id: str) -> LlmCallMetric:
    if value.review_unit_id != unit_id:
        raise DirectReviewError(
            "RISK_USAGE_ATTRIBUTION_INVALID",
            "LLM usage is attributed to a different Review Unit",
        )
    return LlmCallMetric(
        review_unit_id=unit_id,
        repair_no=value.repair_no,
        prompt_tokens=value.prompt_tokens,
        cached_tokens=value.cached_tokens,
        completion_tokens=value.completion_tokens,
        total_tokens=value.total_tokens,
        time_to_first_token_ms=value.time_to_first_token_ms,
        model_duration_ms=value.model_duration_ms,
        trace_id=value.trace_id,
        provider_request_id=value.provider_request_id,
        finish_reason=value.finish_reason,
    )


def _apply_prompt_budget(
    context: Any,
    result: ReviewBatchResult,
) -> ReviewBatchResult:
    budgets = [
        evaluate_prompt_budget(
            unit_id=result.unit_id,
            batch_id=result.batch_id,
            estimated_business_context_tokens=context.estimated_input_tokens,
            provider_prompt_tokens=metric.prompt_tokens,
            provider_cached_tokens=metric.cached_tokens,
        )
        for metric in result.call_metrics
    ]
    budget = summarize_prompt_budgets(budgets)
    if budget.budget_status == "HARD_LIMIT_EXCEEDED":
        raise DirectReviewError(
            "RISK_PROMPT_TOKEN_HARD_LIMIT_EXCEEDED",
            (
                f"{result.unit_id}/{result.batch_id} provider prompt token count "
                f"{budget.provider_prompt_tokens} exceeded hard limit "
                f"{budget.hard_limit_tokens}"
            ),
        )
    warnings = list(result.warnings)
    if budget.budget_status == "SOFT_WARNING":
        warnings.append("RISK_PROMPT_TOKEN_TARGET_EXCEEDED")
    elif budget.budget_status == "PROVIDER_USAGE_UNAVAILABLE":
        warnings.append("RISK_PROMPT_TOKEN_USAGE_UNAVAILABLE")
    return result.model_copy(
        update={
            "warnings": list(dict.fromkeys(warnings)),
            "prompt_budget": budget,
        }
    )


def _commercial_batch(
    context: BaseModel | dict[str, Any],
    result: ReviewUnitResult,
) -> ReviewBatchResult:
    payload = context.model_dump(mode="json") if isinstance(context, BaseModel) else context
    return ReviewBatchResult(
        unit_id=result.unit_id,
        domain=result.domain,
        batch_id=payload["batch_id"],
        status=result.status,
        check_results=result.check_results,
        findings=result.findings,
        warnings=result.warnings,
        model_call_count=result.model_call_count,
        repair_count=result.repair_count,
        tool_call_count=result.tool_call_count,
        prompt_tokens=result.prompt_tokens,
        cached_tokens=result.cached_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        duration_ms=result.duration_ms,
        trace_ids=result.trace_ids,
        call_metrics=result.call_metrics,
        repair_reasons=result.repair_reasons,
        schema_normalization_applied=result.schema_normalization_applied,
        schema_normalization_type=result.schema_normalization_type,
        attempt_diagnostics=result.attempt_diagnostics,
        reason_code_enrichment_count=result.reason_code_enrichment_count,
        reason_code_rule_version=result.reason_code_rule_version,
        ignored_model_reason_code_count=result.ignored_model_reason_code_count,
    )


def _failed_batch_result(
    context: Any,
    error: BaseException,
    *,
    duration_ms: int,
) -> ReviewBatchResult:
    """Preserve unaffected review output when one executable Batch fails.

    Identity, ownership and Generation gates run before execution and remain
    bundle-fatal.  Once those gates pass, a provider/model/check failure is
    represented at Batch/Check scope so other validated Batches can still be
    materialized.
    """

    code = getattr(error, "code", error.__class__.__name__)
    message = str(error) or error.__class__.__name__
    note = f"{code}: {message}"[:1000]
    check_results = [
        CheckCoverageResult(
            check_code=spec.check_code,
            status="FAILED",
            reason_code="CHECK_FAILED",
            decision_note=note,
            finding_local_ids=[],
        )
        for spec in context.check_specs
    ]
    return ReviewBatchResult(
        unit_id=_value(context.unit_id),
        domain=_value(context.unit_id),
        batch_id=context.batch_id,
        status="FAILED",
        check_results=check_results,
        findings=[],
        warnings=[note],
        model_call_count=0,
        repair_count=0,
        tool_call_count=0,
        duration_ms=max(duration_ms, 0),
        trace_ids=[],
        call_metrics=[],
        attempt_diagnostics=[],
        reason_code_enrichment_count=len(check_results),
        reason_code_rule_version="1.0",
        ignored_model_reason_code_count=0,
    )


@dataclass(slots=True)
class _BatchExecutionFailure(Exception):
    unit_id: str
    batch_id: str
    error: BaseException


def _bundle_id(
    plan: RiskReviewPlan,
    *,
    contract_hash: str,
    fixture_id: str,
) -> str:
    return _stable_id(
        "risk-bundle",
        {
            "plan_id": plan.plan_id,
            "plan_hash": plan.plan_hash,
            "contract_hash": contract_hash,
            "fixture_id": fixture_id,
        },
    )


def _bundle_trace_id(
    plan: RiskReviewPlan,
    framework_run_id: str | None,
) -> str:
    return _stable_id(
        "bundle-trace",
        {
            "plan_id": plan.plan_id,
            "framework_run_id": framework_run_id,
        },
    )


def _record_commercial_attempts(
    *,
    batch_id: str,
    result: ReviewUnitResult,
    sink: GenericAttemptArtifactSink | None,
) -> None:
    if sink is None:
        return
    completed_at = datetime.now(timezone.utc)
    for diagnostic in result.attempt_diagnostics:
        attempt_completed_at = completed_at
        attempt_started_at = attempt_completed_at - timedelta(
            milliseconds=diagnostic.model_duration_ms
        )
        accepted = diagnostic.validation_error is None
        sink(
            GenericAttemptArtifact(
                unit_id="commercial_financial",
                batch_id=batch_id,
                attempt_no=diagnostic.repair_no + 1,
                attempt_type=(
                    "INITIAL" if diagnostic.repair_no == 0 else "REPAIR"
                ),
                repair_type=(
                    None
                    if diagnostic.repair_no == 0
                    else "SCHEMA_REPAIR"
                ),
                repair_no=diagnostic.repair_no,
                started_at=attempt_started_at.isoformat(),
                completed_at=attempt_completed_at.isoformat(),
                raw_response=diagnostic.raw_content,
                extracted_raw_object=None,
                deterministic_normalization={
                    "schema_normalization_applied": (
                        diagnostic.schema_normalization_applied
                    ),
                    "schema_normalization_type": (
                        diagnostic.schema_normalization_type
                    ),
                },
                validation_errors={
                    "schema": diagnostic.validation_error,
                },
                prompt_tokens=diagnostic.prompt_tokens,
                cached_tokens=diagnostic.cached_tokens,
                completion_tokens=diagnostic.completion_tokens,
                total_tokens=diagnostic.total_tokens,
                time_to_first_token_ms=diagnostic.time_to_first_token_ms,
                model_duration_ms=diagnostic.model_duration_ms,
                trace_id=diagnostic.trace_id,
                provider_request_id=diagnostic.provider_request_id,
                finish_reason=diagnostic.finish_reason,
                repair_reason=diagnostic.validation_error,
                semantic_preservation_passed=(
                    diagnostic.semantic_preservation_passed
                ),
                accepted=accepted,
                acceptance_reason=(
                    "Commercial Direct Reviewer accepted the strict response"
                    if accepted
                    else (
                        diagnostic.validation_error
                        or "Commercial Direct Reviewer rejected the response"
                    )[:500]
                ),
            )
        )


def _validate_base_bundle_inputs(
    plan: RiskReviewPlan,
    *,
    contract_hash: str,
    schema_version: str,
    fixture_id: str,
    allow_dynamic_batch_count: bool = False,
) -> tuple[list[Any], list[Any], BaseBundleIdentity]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", contract_hash):
        raise DirectReviewError(
            "RISK_BASE_CONTRACT_HASH_INVALID",
            "Base Bundle requires one lowercase SHA-256 contract hash",
        )
    if schema_version != "1.0":
        raise DirectReviewError(
            "RISK_BASE_SCHEMA_VERSION_MISMATCH",
            "Base Bundle only accepts schema_version=1.0",
        )
    if not fixture_id:
        raise DirectReviewError(
            "RISK_BASE_FIXTURE_ID_MISSING",
            "Base Bundle requires one explicit fixture or runtime input identity",
        )
    base_contexts = [
        item for item in plan.contexts if _value(item.unit_id) in BASE_UNIT_IDS
    ]
    if not allow_dynamic_batch_count and len(base_contexts) != 7:
        raise DirectReviewError(
            "RISK_BASE_BATCH_COVERAGE_INVALID",
            "Base Bundle requires exactly seven executable Batch Contexts",
        )
    base_units = [
        item for item in plan.review_units if _value(item.unit_id) in BASE_UNIT_IDS
    ]
    if tuple(_value(item.unit_id) for item in base_units) != BASE_UNIT_IDS:
        raise DirectReviewError(
            "RISK_BASE_UNIT_COVERAGE_INVALID",
            "Base Bundle requires the five frozen base Units in order",
        )
    if tuple(_value(item) for item in plan.required_unit_ids)[:5] != BASE_UNIT_IDS:
        raise DirectReviewError(
            "RISK_BASE_UNIT_COVERAGE_INVALID",
            "Plan required_unit_ids do not contain the frozen five base Units",
        )
    unit_by_id = {_value(item.unit_id): item for item in base_units}
    ordered_batch_ids = [
        batch_id for unit in base_units for batch_id in unit.batch_ids
    ]
    context_by_batch = {item.batch_id: item for item in base_contexts}
    if set(ordered_batch_ids) != set(context_by_batch) or (
        not allow_dynamic_batch_count and len(context_by_batch) != 7
    ):
        raise DirectReviewError(
            "RISK_BASE_BATCH_COVERAGE_INVALID",
            "Base Unit Batch IDs do not match Plan Contexts",
        )
    first = context_by_batch[ordered_batch_ids[0]]
    expected_identity = {
        "review_id": plan.review_id,
        "document_id": plan.document_id,
        "generation_id": plan.generation_id,
        "attempt_no": plan.attempt_no,
        "plan_id": plan.plan_id,
        "perspective": _value(plan.perspective),
        "contract_type": plan.contract_type,
        "review_attitude": plan.review_attitude,
        "our_party": first.our_party,
        "counterparty": first.counterparty,
    }
    for batch_id in ordered_batch_ids:
        context = context_by_batch[batch_id]
        actual = {
            "review_id": context.review_id,
            "document_id": context.document_id,
            "generation_id": context.generation_id,
            "attempt_no": context.attempt_no,
            "plan_id": context.plan_id,
            "perspective": _value(context.perspective),
            "contract_type": context.contract_type,
            "review_attitude": context.review_attitude,
            "our_party": context.our_party,
            "counterparty": context.counterparty,
        }
        if actual != expected_identity:
            raise DirectReviewError(
                "RISK_BASE_IDENTITY_MISMATCH",
                f"Batch {batch_id} does not share the authoritative Bundle identity",
            )
        unit = unit_by_id.get(_value(context.unit_id))
        if unit is None or batch_id not in unit.batch_ids:
            raise DirectReviewError(
                "RISK_BASE_BATCH_OWNERSHIP_INVALID",
                f"Batch {batch_id} does not belong to its declared Unit",
            )
        unit_codes = {item.check_code for item in unit.check_specs}
        context_codes = [item.check_code for item in context.check_specs]
        if (
            len(context_codes) != len(set(context_codes))
            or not set(context_codes).issubset(unit_codes)
        ):
            raise DirectReviewError(
                "RISK_BASE_CHECK_OWNERSHIP_INVALID",
                f"Batch {batch_id} contains an unknown, duplicate or cross-Unit Check",
            )
        if any(
            source.generation_id != plan.generation_id
            for source in (
                *context.evidence_sources,
                *context.absence_evidence_sources,
            )
        ):
            raise DirectReviewError(
                "RISK_BASE_EVIDENCE_GENERATION_MISMATCH",
                f"Batch {batch_id} contains an Evidence Source from another Generation",
            )
        context_source_ids = {
            item.source_id
            for item in (
                *context.evidence_sources,
                *context.absence_evidence_sources,
            )
        }
        if any(
            not set(
                (
                    *policy.allowed_evidence_source_ids,
                    *policy.allowed_absence_source_ids,
                )
            ).issubset(context_source_ids)
            for policy in context.check_evidence_policies
        ):
            raise DirectReviewError(
                "RISK_BASE_EVIDENCE_SOURCE_INVALID",
                f"Batch {batch_id} Evidence policy references an unknown Source",
            )
    return (
        base_contexts,
        base_units,
        BaseBundleIdentity(
            review_id=plan.review_id,
            document_id=plan.document_id,
            generation_id=plan.generation_id,
            attempt_no=plan.attempt_no,
            contract_hash=contract_hash,
            schema_version="1.0",
            perspective=_value(plan.perspective),
            our_party=first.our_party,
            counterparty=first.counterparty,
            review_attitude=plan.review_attitude,
            fixture_id=fixture_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
        ),
    )


def _evidence_overlap_key(value: EvidenceCandidate) -> str:
    if value.evidence_type == "ABSENCE":
        return "ABSENCE:" + hashlib.sha256(
            f"{value.checked_scope}\n{value.verification_note}".encode("utf-8")
        ).hexdigest()
    return (
        f"TEXT:{value.block_id}:{value.char_start}:{value.char_end}:"
        f"{value.quoted_text_hash}"
    )


def _validate_cross_unit_findings(
    units: list[BaseReviewUnitResult],
    identity: BaseBundleIdentity,
) -> list[CrossUnitOverlapCandidate]:
    findings = [
        (unit.unit_id, finding)
        for unit in units
        for finding in unit.findings
    ]
    finding_ids = [finding.finding_local_id for _unit, finding in findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise DirectReviewError(
            "RISK_BASE_CROSS_UNIT_FINDING_DUPLICATED",
            "The same formal Finding identity was produced by multiple Units",
        )
    seen_checks: dict[str, str] = {}
    for unit, finding in findings:
        previous = seen_checks.setdefault(finding.check_code, unit)
        if previous != unit:
            raise DirectReviewError(
                "RISK_BASE_CROSS_UNIT_CHECK_DUPLICATED",
                f"{finding.check_code} was emitted by more than one Unit",
            )
        if (
            finding.perspective != identity.perspective
            or finding.our_party != identity.our_party
            or finding.counterparty != identity.counterparty
        ):
            raise DirectReviewError(
                "RISK_BASE_FINDING_PERSPECTIVE_MISMATCH",
                "A formal Finding does not match the authoritative Bundle perspective",
            )
        if not finding.evidence_candidates:
            raise DirectReviewError(
                "RISK_BASE_FINDING_EVIDENCE_MISSING",
                "Every formal Finding must contain validated Evidence",
            )
        if any(
            item.finding_local_id != finding.finding_local_id
            for item in finding.evidence_candidates
        ):
            raise DirectReviewError(
                "RISK_BASE_FINDING_EVIDENCE_MISMATCH",
                "A formal Evidence item points to another Finding",
            )
    overlaps: list[CrossUnitOverlapCandidate] = []
    for index, (left_unit, left) in enumerate(findings):
        left_keys = {_evidence_overlap_key(item) for item in left.evidence_candidates}
        for right_unit, right in findings[index + 1 :]:
            if left_unit == right_unit:
                continue
            shared = sorted(
                left_keys
                & {_evidence_overlap_key(item) for item in right.evidence_candidates}
            )
            if shared:
                overlaps.append(
                    CrossUnitOverlapCandidate(
                        left_unit_id=left_unit,
                        left_finding_id=left.finding_local_id,
                        left_check_code=left.check_code,
                        right_unit_id=right_unit,
                        right_finding_id=right.finding_local_id,
                        right_check_code=right.check_code,
                        shared_evidence_keys=shared,
                    )
                )
    return overlaps


async def execute_base_risk_review_bundle(
    plan: RiskReviewPlan,
    *,
    tenant_id: str,
    model_id: str,
    contract_hash: str,
    fixture_id: str,
    schema_version: Literal["1.0"] = "1.0",
    framework_run_id: str | None = None,
    generic_reviewer: GenericBaseDirectReviewer | None = None,
    commercial_reviewer: CommercialFinancialDirectReviewer | None = None,
    attempt_artifact_sink: GenericAttemptArtifactSink | None = None,
    cancel_event: asyncio.Event | None = None,
    batch_timeout_seconds: float = 60.0,
    allow_dynamic_batch_count: bool = False,
) -> BaseRiskReviewBundle:
    generic = generic_reviewer or GenericBaseDirectReviewer()
    commercial = commercial_reviewer or CommercialFinancialDirectReviewer()
    bundle_id = _bundle_id(
        plan,
        contract_hash=contract_hash,
        fixture_id=fixture_id,
    )
    trace_id = _bundle_trace_id(plan, framework_run_id)
    try:
        base_contexts, base_units, identity = _validate_base_bundle_inputs(
            plan,
            contract_hash=contract_hash,
            schema_version=schema_version,
            fixture_id=fixture_id,
            allow_dynamic_batch_count=allow_dynamic_batch_count,
        )
    except DirectReviewError as exc:
        raise BaseBundleExecutionError(
            BaseBundleFailure(
                bundle_id=bundle_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                error_code=exc.code,
                error_message=str(exc),
                completed_batch_count=0,
                cancelled_batch_count=0,
                in_flight_batch_count=0,
                trace_id=trace_id,
            )
        ) from exc
    context_by_batch = {item.batch_id: item for item in base_contexts}
    ordered_batch_ids = [
        batch_id for unit in base_units for batch_id in unit.batch_ids
    ]

    semaphore = asyncio.Semaphore(
        min(plan.max_concurrency, len(ordered_batch_ids))
    )
    active = 0
    peak = 0
    queue_started = time.perf_counter()
    first_started: float | None = None
    started_at: dict[str, float] = {}
    finished_at: dict[str, float] = {}
    states = {batch_id: "QUEUED" for batch_id in ordered_batch_ids}
    results: dict[str, ReviewBatchResult] = {}

    async def run_batch(batch_id: str) -> ReviewBatchResult:
        nonlocal active, peak, first_started
        context = context_by_batch[batch_id]
        async with semaphore:
            if cancel_event is not None and cancel_event.is_set():
                states[batch_id] = "CANCELLED"
                raise _BatchExecutionFailure(
                    _value(context.unit_id),
                    batch_id,
                    DirectReviewError(
                        "RISK_BASE_BUNDLE_CANCELLED",
                        "Base Bundle execution was cancelled",
                    ),
                )
            if first_started is None:
                first_started = time.perf_counter()
            started_at[batch_id] = time.perf_counter()
            states[batch_id] = "RUNNING"
            active += 1
            peak = max(peak, active)
            try:
                async def invoke() -> ReviewBatchResult:
                    if _value(context.unit_id) == "commercial_financial":
                        request: CommercialReviewRequest = (
                            commercial_request_from_context(context)
                        )
                        result = await commercial.review(
                            request,
                            tenant_id=tenant_id,
                            model_id=model_id,
                            framework_run_id=framework_run_id,
                        )
                        _record_commercial_attempts(
                            batch_id=batch_id,
                            result=result,
                            sink=attempt_artifact_sink,
                        )
                        return _commercial_batch(context, result)
                    request = generic_request_from_context(
                        context,
                        allow_absence_only_evidence_catalog=(
                            allow_dynamic_batch_count
                        ),
                    )
                    return await generic.review(
                        request,
                        tenant_id=tenant_id,
                        model_id=model_id,
                        framework_run_id=framework_run_id,
                        attempt_artifact_sink=attempt_artifact_sink,
                    )

                result = await asyncio.wait_for(
                    invoke(),
                    timeout=batch_timeout_seconds,
                )
                result = _apply_prompt_budget(context, result)
                results[batch_id] = result
                states[batch_id] = "COMPLETED"
                return result
            except asyncio.CancelledError:
                states[batch_id] = "CANCELLED"
                raise
            except TimeoutError as exc:
                states[batch_id] = "FAILED"
                failure = DirectReviewError(
                    "RISK_BASE_BATCH_TIMEOUT",
                    f"Batch {batch_id} exceeded the Bundle hard timeout",
                )
                result = _failed_batch_result(
                    context,
                    failure,
                    duration_ms=round(
                        (time.perf_counter() - started_at[batch_id]) * 1000
                    ),
                )
                results[batch_id] = result
                return result
            except _BatchExecutionFailure:
                raise
            except Exception as exc:
                states[batch_id] = "FAILED"
                result = _failed_batch_result(
                    context,
                    exc,
                    duration_ms=round(
                        (time.perf_counter() - started_at[batch_id]) * 1000
                    ),
                )
                results[batch_id] = result
                return result
            finally:
                finished_at[batch_id] = time.perf_counter()
                active -= 1

    tasks = {
        batch_id: asyncio.create_task(run_batch(batch_id))
        for batch_id in ordered_batch_ids
    }
    gather_future = asyncio.gather(
        *(tasks[batch_id] for batch_id in ordered_batch_ids)
    )
    cancel_waiter: asyncio.Task[bool] | None = None
    try:
        if cancel_event is None:
            completed = await gather_future
        else:
            cancel_waiter = asyncio.create_task(cancel_event.wait())
            done, _pending = await asyncio.wait(
                {gather_future, cancel_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_waiter in done and cancel_event.is_set() and not gather_future.done():
                raise DirectReviewError(
                    "RISK_BASE_BUNDLE_CANCELLED",
                    "Base Bundle execution was cancelled",
                )
            completed = await gather_future
    except BaseException as exc:
        in_flight_at_failure = sum(
            state == "RUNNING" for state in states.values()
        )
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        failed_unit_id = None
        failed_batch_id = None
        cause: BaseException = exc
        if isinstance(exc, _BatchExecutionFailure):
            failed_unit_id = exc.unit_id
            failed_batch_id = exc.batch_id
            cause = exc.error
        error_code = getattr(cause, "code", "RISK_BASE_BUNDLE_FAILED")
        failure = BaseBundleFailure(
            bundle_id=bundle_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            failed_unit_id=failed_unit_id,
            failed_batch_id=failed_batch_id,
            error_code=error_code,
            error_message=str(cause) or cause.__class__.__name__,
            completed_batch_count=sum(
                state == "COMPLETED" for state in states.values()
            ),
            cancelled_batch_count=sum(
                state == "CANCELLED" for state in states.values()
            ),
            in_flight_batch_count=in_flight_at_failure,
            trace_id=trace_id,
            completed_batch_ids=[
                batch_id
                for batch_id in ordered_batch_ids
                if states[batch_id] == "COMPLETED"
            ],
            cancelled_batch_ids=[
                batch_id
                for batch_id in ordered_batch_ids
                if states[batch_id] == "CANCELLED"
            ],
            diagnostic_batch_results=[
                results[batch_id]
                for batch_id in ordered_batch_ids
                if batch_id in results
            ],
        )
        raise BaseBundleExecutionError(failure) from cause
    finally:
        if cancel_waiter is not None and not cancel_waiter.done():
            cancel_waiter.cancel()
            await asyncio.gather(cancel_waiter, return_exceptions=True)

    if completed and all(item.status == "FAILED" for item in completed):
        first_failure = completed[0]
        first_diagnostic = (
            first_failure.warnings[0]
            if first_failure.warnings
            else "RISK_BASE_ALL_BATCHES_FAILED: All Base Review Batches failed"
        )
        first_code, _, first_message = first_diagnostic.partition(":")
        raise BaseBundleExecutionError(
            BaseBundleFailure(
                bundle_id=bundle_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                failed_unit_id=first_failure.unit_id,
                failed_batch_id=first_failure.batch_id,
                error_code=first_code or "RISK_BASE_ALL_BATCHES_FAILED",
                error_message=(
                    first_message.strip()
                    or "All Base Review Batches failed"
                ),
                completed_batch_count=0,
                cancelled_batch_count=0,
                in_flight_batch_count=0,
                trace_id=trace_id,
                diagnostic_batch_results=completed,
            )
        )

    by_batch = {item.batch_id: item for item in completed}
    unit_results: list[BaseReviewUnitResult] = []
    try:
        for unit in base_units:
            unit_results.append(_merge_unit_result(unit, by_batch))
        overlaps = _validate_cross_unit_findings(unit_results, identity)
    except BaseException as exc:
        cause = exc
        error_code = getattr(cause, "code", "RISK_BASE_UNIT_MERGE_FAILED")
        failed_unit_id = None
        if "unit" in locals():
            failed_unit_id = _value(unit.unit_id)
        raise BaseBundleExecutionError(
            BaseBundleFailure(
                bundle_id=bundle_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                failed_unit_id=failed_unit_id,
                error_code=error_code,
                error_message=str(cause) or cause.__class__.__name__,
                completed_batch_count=len(ordered_batch_ids),
                cancelled_batch_count=0,
                in_flight_batch_count=0,
                trace_id=trace_id,
                completed_batch_ids=list(ordered_batch_ids),
                diagnostic_batch_results=[
                    by_batch[batch_id] for batch_id in ordered_batch_ids
                ],
            )
        ) from cause

    wall_duration_ms = round((time.perf_counter() - queue_started) * 1000)
    queue_duration_ms = round(
        ((first_started or queue_started) - queue_started) * 1000
    )
    batch_metrics = [
        BaseBundleBatchMetric(
            unit_id=by_batch[batch_id].unit_id,
            batch_id=batch_id,
            status=by_batch[batch_id].status,
            start_offset_ms=round(
                (started_at[batch_id] - queue_started) * 1000
            ),
            wall_duration_ms=round(
                (finished_at[batch_id] - started_at[batch_id]) * 1000
            ),
            time_to_first_token_ms=(
                by_batch[batch_id].call_metrics[0].time_to_first_token_ms
                if by_batch[batch_id].call_metrics
                else None
            ),
            prompt_tokens=by_batch[batch_id].prompt_tokens,
            cached_tokens=by_batch[batch_id].cached_tokens,
            completion_tokens=by_batch[batch_id].completion_tokens,
            total_tokens=by_batch[batch_id].total_tokens,
            prompt_budget=by_batch[batch_id].prompt_budget,
            model_call_count=by_batch[batch_id].model_call_count,
            repair_count=by_batch[batch_id].repair_count,
            tool_call_count=by_batch[batch_id].tool_call_count,
        )
        for batch_id in ordered_batch_ids
    ]
    unit_metrics = []
    for unit_result in unit_results:
        unit_batch_metrics = [
            item for item in batch_metrics if item.unit_id == unit_result.unit_id
        ]
        unit_started = min(
            started_at[item.batch_id] for item in unit_batch_metrics
        )
        unit_finished = max(
            finished_at[item.batch_id] for item in unit_batch_metrics
        )
        unit_metrics.append(
            BaseBundleUnitMetric(
                unit_id=unit_result.unit_id,
                status=unit_result.status,
                check_count=len(unit_result.check_results),
                candidate_count=len(unit_result.candidate_decisions),
                root_count=len(unit_result.canonical_risk_roots),
                finding_count=len(unit_result.findings),
                evidence_count=sum(
                    len(item.evidence_candidates)
                    for item in unit_result.findings
                ),
                wall_duration_ms=round(
                    (unit_finished - unit_started) * 1000
                ),
            )
        )
    slowest = max(completed, key=lambda item: (item.duration_ms, item.batch_id))
    slowest_unit = max(
        unit_metrics,
        key=lambda item: (item.wall_duration_ms, item.unit_id),
    )
    prompt_budgets = [
        item.prompt_budget
        for item in completed
        if item.prompt_budget is not None
    ]
    provider_prompt_values = [
        item.provider_prompt_tokens
        for item in prompt_budgets
        if item.provider_prompt_tokens is not None
    ]
    batches_over_target = [
        item.batch_id
        for item in prompt_budgets
        if item.budget_status == "SOFT_WARNING"
    ]
    batches_over_hard_limit = [
        item.batch_id
        for item in completed
        if (
            item.prompt_budget is not None
            and item.prompt_budget.budget_status == "HARD_LIMIT_EXCEEDED"
        )
        or any(
            warning.startswith("RISK_PROMPT_TOKEN_HARD_LIMIT_EXCEEDED")
            for warning in item.warnings
        )
    ]
    failed_batch_ids = [
        item.batch_id for item in completed if item.status == "FAILED"
    ]
    bundle_status: Literal["COMPLETED", "PARTIAL_FAILED"] = (
        "PARTIAL_FAILED"
        if any(item.status != "COMPLETED" for item in unit_results)
        else "COMPLETED"
    )
    return BaseRiskReviewBundle(
        bundle_id=bundle_id,
        identity=identity,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        status=bundle_status,
        units=unit_results,
        batch_results=[by_batch[item] for item in ordered_batch_ids],
        cross_unit_overlap_candidates=overlaps,
        metrics=BaseBundleMetrics(
            queue_duration_ms=queue_duration_ms,
            wall_duration_ms=wall_duration_ms,
            peak_concurrency=peak,
            batch_count=len(ordered_batch_ids),
            model_call_count=sum(item.model_call_count for item in completed),
            repair_count=sum(item.repair_count for item in completed),
            prompt_tokens=_sum_optional(item.prompt_tokens for item in completed),
            cached_tokens=_sum_optional(item.cached_tokens for item in completed),
            completion_tokens=_sum_optional(
                item.completion_tokens for item in completed
            ),
            total_tokens=_sum_optional(item.total_tokens for item in completed),
            prompt_budget_warning_count=len(batches_over_target),
            prompt_budget_hard_failure_count=len(batches_over_hard_limit),
            max_provider_prompt_tokens=(
                max(provider_prompt_values) if provider_prompt_values else None
            ),
            batches_over_target=batches_over_target,
            batches_over_hard_limit=batches_over_hard_limit,
            slowest_batch_id=slowest.batch_id,
            slowest_batch_duration_ms=slowest.duration_ms,
            slowest_unit_id=slowest_unit.unit_id,
            slowest_unit_duration_ms=slowest_unit.wall_duration_ms,
            batch_metrics=batch_metrics,
            unit_metrics=unit_metrics,
            failed_batch_ids=failed_batch_ids,
        ),
    )


def _merge_lre_cross_batch_roots(
    findings: list[FindingDraft],
    canonical_roots: list[CanonicalRiskRoot],
) -> tuple[list[FindingDraft], list[CanonicalRiskRoot], dict[str, str]]:
    """Merge only Registry-approved LRE roots that crossed a Batch boundary."""
    findings_by_id = {item.finding_local_id: item for item in findings}
    grouped: dict[tuple[str, str], list[CanonicalRiskRoot]] = {}
    passthrough: list[CanonicalRiskRoot] = []
    for root in canonical_roots:
        if (
            root.domain == "liability_remedies_exit"
            and root.root_type == "UNBOUNDED_LIABILITY_EXPOSURE"
        ):
            grouped.setdefault(
                (root.root_type, root.root_severity_rule_id),
                [],
            ).append(root)
        else:
            passthrough.append(root)

    replacement_by_finding_id: dict[str, str] = {}
    merged_findings: list[FindingDraft] = [
        item
        for item in findings
        if all(
            item.finding_local_id != root.finding_local_id
            for roots in grouped.values()
            if len(roots) > 1
            for root in roots
        )
    ]
    merged_roots = list(passthrough)
    for roots in grouped.values():
        roots = sorted(roots, key=lambda item: (item.check_code, item.root_id))
        if len(roots) == 1:
            merged_roots.append(roots[0])
            continue
        source_findings = [
            findings_by_id[root.finding_local_id] for root in roots
        ]
        if len(
            {
                (
                    item.perspective,
                    item.our_party,
                    item.counterparty,
                    item.source_unit_id,
                )
                for item in source_findings
            }
        ) != 1:
            raise DirectReviewError(
                "RISK_UNIT_ROOT_MERGE_INVALID",
                "Cross-Batch LRE roots disagree on party perspective or domain",
            )
        source_candidate_ids = list(
            dict.fromkeys(
                candidate_id
                for root in roots
                for candidate_id in root.source_candidate_ids
            )
        )
        core_primary_ids = list(
            dict.fromkeys(
                source_id
                for root in roots
                for source_id in root.core_primary_evidence_source_ids
            )
        )
        context_primary_ids = [
            source_id
            for source_id in dict.fromkeys(
                source_id
                for root in roots
                for source_id in root.context_primary_evidence_source_ids
            )
            if source_id not in set(core_primary_ids)
        ]
        primary_ids = [*core_primary_ids, *context_primary_ids]
        supporting_ids = [
            source_id
            for source_id in dict.fromkeys(
                source_id
                for root in roots
                for source_id in root.supporting_evidence_source_ids
            )
            if source_id not in set(primary_ids)
        ]
        severity_factors = _merge_po_severity_factors(
            *(root.severity_factors for root in roots)
        )
        risk_level = _po_root_risk_level(
            roots[0].root_severity_rule_id,
            severity_factors,
            [root.risk_level for root in roots],
        )
        control_codes = list(
            dict.fromkeys(
                code
                for root in roots
                for code in root.recommended_control_codes
            )
        )
        check_code = (
            "LRE-003"
            if any(root.check_code == "LRE-003" for root in roots)
            else roots[0].check_code
        )
        root_id = _stable_id(
            "risk-root",
            {
                "domain": "liability_remedies_exit",
                "root_type": "UNBOUNDED_LIABILITY_EXPOSURE",
                "root_severity_rule_id": roots[0].root_severity_rule_id,
                "core_primary_evidence_source_ids": sorted(core_primary_ids),
                "source_candidate_ids": sorted(source_candidate_ids),
            },
        )
        finding_id = _stable_id(
            "finding",
            {
                "root_id": root_id,
                "check_code": check_code,
                "risk_type": "UNBOUNDED_LIABILITY_EXPOSURE",
                "primary_evidence_source_ids": primary_ids,
            },
        )
        evidence_by_identity: dict[tuple[object, ...], EvidenceCandidate] = {}
        for finding in source_findings:
            for evidence in finding.evidence_candidates:
                identity = (
                    evidence.evidence_type,
                    evidence.source_ir_item_id,
                    evidence.anchor_id,
                    evidence.block_id,
                    evidence.char_start,
                    evidence.char_end,
                    evidence.checked_scope,
                    evidence.verification_note,
                )
                evidence_by_identity.setdefault(identity, evidence)
        evidence_candidates = []
        for index, (identity, evidence) in enumerate(
            sorted(evidence_by_identity.items(), key=lambda item: repr(item[0])),
            1,
        ):
            evidence_candidates.append(
                evidence.model_copy(
                    update={
                        "evidence_local_id": _stable_id(
                            "evidence",
                            {
                                "finding_id": finding_id,
                                "index": index,
                                "identity": identity,
                            },
                        ),
                        "finding_local_id": finding_id,
                    }
                )
            )
        representative = source_findings[0]
        merged_finding = representative.model_copy(
            update={
                "finding_local_id": finding_id,
                "check_code": check_code,
                "category": "LIABILITY",
                "risk_type": "UNBOUNDED_LIABILITY_EXPOSURE",
                "risk_level": risk_level,
                "title": "赔偿范围开放且累计责任缺少有效上限",
                "issue": (
                    "合同中的开放损失赔偿、第三方或全部费用责任，与缺少累计责任"
                    "总上限共同形成同一责任暴露；各来源条款已按同一Canonical "
                    "Risk Root确定性归并。"
                ),
                "impact_to_our_party": (
                    f"{representative.our_party}可能承担缺少金额边界的直接、间接或"
                    "第三方责任，并存在多种责任机制叠加扩大损失的风险。"
                ),
                "suggestion": (
                    "将赔偿范围限制为直接、可预见且已证明的损失；设置明确的累计"
                    "责任上限及计算基数；封闭责任上限例外，并防止违约金、赔偿、"
                    "退款等责任重复受偿。"
                ),
                "evidence_candidates": evidence_candidates,
            }
        )
        merged_findings.append(merged_finding)
        merged_roots.append(
            CanonicalRiskRoot(
                root_id=root_id,
                domain="liability_remedies_exit",
                check_code=check_code,
                risk_type="UNBOUNDED_LIABILITY_EXPOSURE",
                root_type="UNBOUNDED_LIABILITY_EXPOSURE",
                source_candidate_ids=source_candidate_ids,
                primary_evidence_source_ids=primary_ids,
                core_primary_evidence_source_ids=core_primary_ids,
                context_primary_evidence_source_ids=context_primary_ids,
                supporting_evidence_source_ids=supporting_ids,
                severity_factors=severity_factors,
                recommended_control_codes=control_codes,
                root_severity_rule_id=roots[0].root_severity_rule_id,
                risk_level=risk_level,
                finding_local_id=finding_id,
            )
        )
        replacement_by_finding_id.update(
            {root.finding_local_id: finding_id for root in roots}
        )
    return merged_findings, merged_roots, replacement_by_finding_id


def _replace_finding_ids(
    values: list[str],
    replacements: dict[str, str],
) -> list[str]:
    return list(dict.fromkeys(replacements.get(value, value) for value in values))


def _merge_unit_result(unit, by_batch: dict[str, ReviewBatchResult]) -> BaseReviewUnitResult:
    batches = [by_batch[batch_id] for batch_id in unit.batch_ids]
    expected_codes = tuple(item.check_code for item in unit.check_specs)
    raw_checks = [item for batch in batches for item in batch.check_results]
    checks_by_code = {item.check_code: item for item in raw_checks}
    if (
        len(checks_by_code) != len(raw_checks)
        or set(checks_by_code) != set(expected_codes)
    ):
        raise DirectReviewError(
            "RISK_UNIT_CHECK_COVERAGE_INVALID",
            f"{_value(unit.unit_id)} Batch merge lost or duplicated a Check",
        )
    checks = [checks_by_code[check_code] for check_code in expected_codes]
    findings = [item for batch in batches for item in batch.findings]
    canonical_roots = [
        item for batch in batches for item in batch.canonical_risk_roots
    ]
    findings, canonical_roots, finding_id_replacements = (
        _merge_lre_cross_batch_roots(findings, canonical_roots)
        if _value(unit.unit_id) == "liability_remedies_exit"
        else (findings, canonical_roots, {})
    )
    if finding_id_replacements:
        checks = [
            item.model_copy(
                update={
                    "finding_local_ids": _replace_finding_ids(
                        item.finding_local_ids,
                        finding_id_replacements,
                    )
                }
            )
            for item in checks
        ]
    root_ids = [item.root_id for item in canonical_roots]
    if len(root_ids) != len(set(root_ids)):
        raise DirectReviewError(
            "RISK_UNIT_ROOT_DUPLICATED",
            f"{_value(unit.unit_id)} contains a duplicate Canonical Risk Root",
        )
    canonical_keys = [_canonical_risk_key(item) for item in findings]
    if len(canonical_keys) != len(set(canonical_keys)):
        raise DirectReviewError(
            "RISK_UNIT_FINDING_DUPLICATED",
            f"{_value(unit.unit_id)} contains a duplicate same-root Finding",
        )
    if any(item.source_unit_id != _value(unit.unit_id) for item in findings):
        raise DirectReviewError(
            "RISK_UNIT_FINDING_CONTAMINATED",
            "A Finding crossed its assigned Review Unit",
        )
    return BaseReviewUnitResult(
        unit_id=_value(unit.unit_id),
        domain=_value(unit.domain),
        status=(
            "FAILED"
            if all(item.status == "FAILED" for item in checks)
            else (
                "PARTIAL_FAILED"
                if any(
                    item.status == "FAILED"
                    or item.reason_code == "INSUFFICIENT_EVIDENCE"
                    for item in checks
                )
                else "COMPLETED"
            )
        ),
        batch_ids=list(unit.batch_ids),
        check_results=checks,
        findings=sorted(
            findings,
            key=lambda item: (
                item.check_code,
                item.risk_type,
                item.finding_local_id,
            ),
        ),
        warnings=sorted({warning for batch in batches for warning in batch.warnings}),
        model_call_count=sum(item.model_call_count for item in batches),
        repair_count=sum(item.repair_count for item in batches),
        schema_repair_count=sum(item.schema_repair_count for item in batches),
        evidence_selection_repair_count=sum(
            item.evidence_selection_repair_count for item in batches
        ),
        evidence_binding_normalization_count=sum(
            item.evidence_binding_normalization_count for item in batches
        ),
        ignored_model_link_fields_count=sum(
            item.ignored_model_link_fields_count for item in batches
        ),
        deterministic_enrichment_count=sum(
            item.deterministic_enrichment_count for item in batches
        ),
        check_code_enrichment_count=sum(
            item.check_code_enrichment_count for item in batches
        ),
        category_enrichment_count=sum(
            item.category_enrichment_count for item in batches
        ),
        risk_type_enrichment_count=sum(
            item.risk_type_enrichment_count for item in batches
        ),
        ignored_model_check_code_count=sum(
            item.ignored_model_check_code_count for item in batches
        ),
        ignored_model_category_count=sum(
            item.ignored_model_category_count for item in batches
        ),
        deterministic_enrichments=[
            enrichment.model_copy(
                update={
                    "finding_local_id": finding_id_replacements.get(
                        enrichment.finding_local_id,
                        enrichment.finding_local_id,
                    )
                }
            )
            for batch in batches
            for enrichment in batch.deterministic_enrichments
        ],
        candidate_decisions=[
            decision
            for batch in batches
            for decision in batch.candidate_decisions
        ],
        canonical_risk_roots=sorted(
            canonical_roots,
            key=lambda item: (
                item.check_code,
                item.root_type,
                item.root_id,
            ),
        ),
        check_decisions=[
            decision.model_copy(
                update={
                    "finding_local_ids": _replace_finding_ids(
                        decision.finding_local_ids,
                        finding_id_replacements,
                    )
                }
            )
            for batch in batches
            for decision in batch.check_decisions
        ],
        supporting_primary_overlap_count=sum(
            batch.supporting_primary_overlap_count for batch in batches
        ),
        decision_summary_perspective_warning_count=sum(
            batch.decision_summary_perspective_warning_count for batch in batches
        ),
        proposed_severity_factor_count=sum(
            batch.proposed_severity_factor_count for batch in batches
        ),
        accepted_severity_factor_count=sum(
            batch.accepted_severity_factor_count for batch in batches
        ),
        rejected_severity_factor_count=sum(
            batch.rejected_severity_factor_count for batch in batches
        ),
        selected_evidence_source_ids=list(
            dict.fromkeys(
                source_id
                for item in batches
                for source_id in item.selected_evidence_source_ids
            )
        ),
        prompt_tokens=_sum_optional(item.prompt_tokens for item in batches),
        cached_tokens=_sum_optional(item.cached_tokens for item in batches),
        completion_tokens=_sum_optional(item.completion_tokens for item in batches),
        total_tokens=_sum_optional(item.total_tokens for item in batches),
        duration_ms=max(item.duration_ms for item in batches),
        trace_ids=[trace for batch in batches for trace in batch.trace_ids],
        call_metrics=[
            metric for batch in batches for metric in batch.call_metrics
        ],
        fva_assessments=[
            assessment
            for batch in batches
            for assessment in batch.fva_assessments
        ],
    )


def _canonical_risk_key(finding: FindingDraft) -> tuple:
    anchor_ids = sorted(
        {
            item.anchor_id
            for item in finding.evidence_candidates
            if item.anchor_id is not None
        }
    )
    return (
        finding.check_code,
        finding.risk_type,
        finding.risk_level,
        tuple(anchor_ids),
    )


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


def bundle_duration_summary(values: list[BaseRiskReviewBundle]) -> dict[str, int]:
    if not values:
        raise ValueError("At least one Bundle is required")
    durations = [item.metrics.wall_duration_ms for item in values]
    return {
        "min": min(durations),
        "median": round(statistics.median(durations)),
        "max": max(durations),
    }
