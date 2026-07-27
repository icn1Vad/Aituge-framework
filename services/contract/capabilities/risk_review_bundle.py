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
    "ICD-003": "检查第三方权刭u�9����k�w��ENCE_IR_UNKNOWN",
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
