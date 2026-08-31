from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

from contract.application.idempotency import canonical_json
from contract.legal_evidence.models import LegalDomain, LegalEvidenceBundle

LEGAL_EVIDENCE_CHECK_BINDING_VERSION = "legal-check-binding-v1"


@dataclass(frozen=True, slots=True)
class LegalEvidenceCheckProfile:
    """High-precision, fail-closed mapping from legal text to one base check."""

    check_code: str
    domain: LegalDomain
    required_term_groups: tuple[tuple[str, ...], ...]
    excluded_terms: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        normalized = _normalize(text)
        if any(_normalize(term) in normalized for term in self.excluded_terms):
            return False
        return all(
            any(_normalize(term) in normalized for term in group)
            for group in self.required_term_groups
        )


def _profile(
    domain: LegalDomain,
    check_code: str,
    *required_term_groups: tuple[str, ...],
    excluded_terms: tuple[str, ...] = (),
) -> LegalEvidenceCheckProfile:
    return LegalEvidenceCheckProfile(
        check_code=check_code,
        domain=domain,
        required_term_groups=required_term_groups,
        excluded_terms=excluded_terms,
    )


LEGAL_EVIDENCE_CHECK_PROFILES = (
    _profile(
        "formation_validity_authority",
        "FVA-001",
        ("当事人名称", "法人名称", "主体身份", "身份证明", "统一社会信用代码"),
    ),
    _profile(
        "formation_validity_authority",
        "FVA-002",
        ("主体资格", "法定代表人", "代表权限", "代理权限", "授权委托书", "无权代理"),
    ),
    _profile(
        "formation_validity_authority",
        "FVA-003",
        ("签字盖章", "签名盖章", "签字或者盖章", "签名或者盖章", "电子签名", "签署形式"),
    ),
    _profile(
        "formation_validity_authority",
        "FVA-004",
        ("生效条件", "附生效条件", "附生效期限", "批准登记手续", "办理批准手续"),
    ),
    _profile(
        "formation_validity_authority",
        "FVA-005",
        ("效力性强制性规定", "强制性规定", "禁止性规定", "合同无效", "公序良俗"),
    ),
    _profile(
        "commercial_financial",
        "CF-001",
        ("价款总额", "合同总价", "计价标准", "计费标准", "价格计算", "价款计算"),
    ),
    _profile(
        "commercial_financial",
        "CF-002",
        ("付款期限", "支付期限", "付款条件", "支付条件", "付款节点", "分期付款"),
    ),
    _profile(
        "commercial_financial",
        "CF-003",
        ("增值税发票", "发票开具", "开具发票", "纳税义务", "税费承担", "适用税率"),
    ),
    _profile(
        "commercial_financial",
        "CF-004",
        ("价款抵销", "债务抵销", "单方扣款", "价款扣减", "价格调整", "结算调整"),
    ),
    _profile(
        "commercial_financial",
        "CF-005",
        ("预付款", "履约保证金", "履约保函", "履约担保", "保证金返还"),
    ),
    _profile(
        "commercial_financial",
        "CF-006",
        ("支付账户", "收款账户", "银行账户", "支付路径", "支付币种", "外汇支付"),
    ),
    _profile(
        "commercial_financial",
        "CF-007",
        ("交付期限", "交付时间", "交付地点", "交付方式", "交付物清单"),
    ),
    _profile(
        "commercial_financial",
        "CF-008",
        ("验收标准", "验收程序", "验收期限", "视为验收", "验收合格"),
    ),
    _profile(
        "performance_obligations",
        "PO-001",
        ("义务范围", "服务范围", "工作范围", "履行范围", "合同标的范围"),
    ),
    _profile(
        "performance_obligations",
        "PO-002",
        ("单方决定", "单方解释", "单方控制", "任意变更", "权利义务不对等"),
    ),
    _profile(
        "performance_obligations",
        "PO-003",
        ("配合义务", "协助义务", "前置条件", "先决条件", "依赖条件"),
    ),
    _profile(
        "performance_obligations",
        "PO-004",
        ("服务标准", "服务级别", "响应时限", "响应时间", "质量标准"),
    ),
    _profile(
        "performance_obligations",
        "PO-005",
        ("转委托", "合同分包", "违法分包", "合同转让", "权利义务一并转让"),
    ),
    _profile(
        "performance_obligations",
        "PO-006",
        ("需求变更", "范围变更", "变更程序", "变更价款", "工程签证"),
    ),
    _profile(
        "performance_obligations",
        "PO-007",
        ("质量保证期", "质保期", "维修义务", "整改期限", "复验", "售后服务"),
    ),
    _profile(
        "ip_confidentiality_data",
        "ICD-001",
        ("项目成果归属", "知识产权归属", "著作权归属", "专利申请权", "职务成果"),
    ),
    _profile(
        "ip_confidentiality_data",
        "ICD-002",
        ("背景知识产权", "既有知识产权", "许可范围", "使用许可", "再许可", "许可期限"),
    ),
    _profile(
        "ip_confidentiality_data",
        "ICD-003",
        ("第三方知识产权", "不侵权保证", "侵权赔偿", "权利瑕疵担保", "知识产权侵权"),
    ),
    _profile(
        "ip_confidentiality_data",
        "ICD-004",
        ("保密信息", "保密义务", "保密期限", "保密例外", "保密披露"),
    ),
    _profile(
        "ip_confidentiality_data",
        "ICD-005",
        ("个人信息处理", "数据处理", "数据安全", "网络安全", "数据访问权限"),
    ),
    _profile(
        "ip_confidentiality_data",
        "ICD-006",
        ("数据权属", "数据返还", "数据删除", "数据留存", "数据销毁"),
    ),
    _profile(
        "liability_remedies_exit",
        "LRE-001",
        ("构成违约", "违约情形", "违约行为", "违约责任成立", "违约责任承担"),
    ),
    _profile(
        "liability_remedies_exit",
        "LRE-002",
        ("违约金", "损失赔偿额", "违约损失", "调整违约金", "约定的违约金"),
    ),
    _profile(
        "liability_remedies_exit",
        "LRE-003",
        ("责任限额", "责任上限", "免责条款", "间接损失", "可得利益损失"),
    ),
    _profile(
        "liability_remedies_exit",
        "LRE-004",
        ("第三方索赔", "补偿责任", "损害赔偿责任", "赔偿请求", "追偿权"),
    ),
    _profile(
        "liability_remedies_exit",
        "LRE-005",
        ("单方解除", "合同解除权", "解除合同", "合同终止权", "单方终止"),
    ),
    _profile(
        "liability_remedies_exit",
        "LRE-006",
        ("合同终止后", "合同解除后", "解除后的结算", "终止后的结算", "终止后继续有效"),
    ),
    _profile(
        "liability_remedies_exit",
        "LRE-007",
        ("不可抗力", "情势变更", "风险负担", "不可抗力事件"),
    ),
    _profile(
        "liability_remedies_exit",
        "LRE-008",
        ("仲裁协议", "仲裁委员会", "管辖法院", "协议管辖", "适用法律", "争议解决"),
    ),
    _profile(
        "cross_clause_consistency",
        "CCC-001",
        ("金额不一致", "期限不一致", "日期不一致", "比例不一致", "数额相互矛盾"),
    ),
    _profile(
        "cross_clause_consistency",
        "CCC-002",
        ("权利义务", "履行条件"),
        ("相互冲突", "相互矛盾", "前后不一致"),
    ),
    _profile(
        "cross_clause_consistency",
        "CCC-003",
        ("附件效力", "条款优先顺序", "文件优先顺序", "引用条款错误", "合同组成部分"),
    ),
    _profile(
        "cross_clause_consistency",
        "CCC-004",
        ("付款与验收", "支付与验收", "违约与解除", "付款与解除", "验收与解除"),
    ),
    _profile(
        "cross_clause_consistency",
        "CCC-005",
        ("术语不一致", "名称不一致", "定义不一致", "主体名称不一致"),
    ),
    _profile(
        "missing_ambiguity_completeness",
        "MAC-001",
        ("应当具备下列条款", "应当包括下列条款", "应当载明", "必要条款缺失"),
    ),
    _profile(
        "missing_ambiguity_completeness",
        "MAC-002",
        ("关键商务要素缺失", "执行机制缺失", "价款约定不明", "交付约定不明", "验收约定不明"),
    ),
    _profile(
        "missing_ambiguity_completeness",
        "MAC-003",
        ("术语含义不明", "定义不明", "存在歧义", "有两种以上解释", "通常理解予以解释"),
    ),
    _profile(
        "missing_ambiguity_completeness",
        "MAC-004",
        ("履行期限不明确", "履行地点不明确", "履行方式不明确", "质量要求不明确", "违约后果不明确"),
    ),
    _profile(
        "missing_ambiguity_completeness",
        "MAC-005",
        ("空白条款", "待定条款", "占位符", "引用错误", "断裂引用"),
    ),
    _profile(
        "missing_ambiguity_completeness",
        "MAC-006",
        ("未约定违约责任", "未约定解除权", "未约定争议解决", "必要救济缺失", "退出机制缺失"),
    ),
)


_EXPECTED_CHECK_CODES = frozenset(
    f"{prefix}-{number:03d}"
    for prefix, count in (
        ("FVA", 5),
        ("CF", 8),
        ("PO", 7),
        ("ICD", 6),
        ("LRE", 8),
        ("CCC", 5),
        ("MAC", 6),
    )
    for number in range(1, count + 1)
)


def _normalize(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace()
    )


def _validate_profiles(profiles: tuple[LegalEvidenceCheckProfile, ...]) -> None:
    codes = [profile.check_code for profile in profiles]
    if len(codes) != len(set(codes)) or frozenset(codes) != _EXPECTED_CHECK_CODES:
        raise RuntimeError("Legal evidence binding profiles must cover exactly 45 checks")
    for profile in profiles:
        if not profile.required_term_groups:
            raise RuntimeError(f"{profile.check_code} has no binding terms")
        for group in profile.required_term_groups:
            normalized = [_normalize(term) for term in group]
            if any(not term for term in normalized) or len(normalized) != len(set(normalized)):
                raise RuntimeError(f"{profile.check_code} has invalid binding terms")


_validate_profiles(LEGAL_EVIDENCE_CHECK_PROFILES)


class LegalEvidenceCheckBinder:
    """Bind a domain retrieval result to checks without another model call."""

    def __init__(
        self,
        profiles: tuple[
            LegalEvidenceCheckProfile, ...
        ] = LEGAL_EVIDENCE_CHECK_PROFILES,
        *,
        profile_version: str = LEGAL_EVIDENCE_CHECK_BINDING_VERSION,
    ) -> None:
        normalized_version = profile_version.strip()
        if not normalized_version:
            raise ValueError("profile_version must not be empty")
        _validate_profiles(profiles)
        self.profile_version = normalized_version
        self._profiles = {profile.check_code: profile for profile in profiles}

    def bind(self, bundle: LegalEvidenceBundle) -> LegalEvidenceBundle:
        issue_by_id = {issue.issue_id: issue for issue in bundle.issues}
        bound_evidence = []
        for evidence in bundle.evidence:
            eligible_codes: set[str] = set()
            eligible_domains: dict[str, LegalDomain] = {}
            for issue_id in evidence.issue_ids:
                issue = issue_by_id.get(issue_id)
                if issue is None:
                    continue
                for check_code in issue.check_codes:
                    eligible_codes.add(check_code)
                    eligible_domains[check_code] = issue.domain
            searchable_text = "\n".join(
                (
                    evidence.unit.title,
                    *evidence.unit.heading_path,
                    evidence.unit.content,
                )
            )
            check_codes = sorted(
                check_code
                for check_code in eligible_codes
                if (
                    (profile := self._profiles.get(check_code)) is not None
                    and profile.domain == eligible_domains.get(check_code)
                    and profile.matches(searchable_text)
                )
            )
            cautions = set(evidence.cautions)
            if evidence.unit.metadata_verification_status != "VERIFIED":
                cautions.add("LEGAL_METADATA_UNVERIFIED")
            if evidence.unit.validity_status in {None, "UNKNOWN"}:
                cautions.add("LEGAL_VALIDITY_UNVERIFIED")
            bound_evidence.append(
                evidence.model_copy(
                    update={
                        "check_codes": check_codes,
                        "cautions": sorted(cautions),
                    }
                )
            )

        mapped_check_codes = sorted(
            {
                check_code
                for evidence in bound_evidence
                for check_code in evidence.check_codes
            }
        )
        eligible_check_codes = {
            check_code for issue in bundle.issues for check_code in issue.check_codes
        }
        unmapped_check_codes = sorted(eligible_check_codes - set(mapped_check_codes))
        mapped_issue_ids = {
            issue_id
            for evidence in bound_evidence
            if evidence.check_codes
            for issue_id in evidence.issue_ids
        }
        unmapped_issue_ids = sorted(set(issue_by_id) - mapped_issue_ids)
        if not mapped_check_codes:
            binding_status = "NONE"
        elif not unmapped_check_codes:
            binding_status = "COMPLETE"
        else:
            binding_status = "PARTIAL"
        unverified_evidence_ids = sorted(
            evidence.evidence_id
            for evidence in bound_evidence
            if evidence.check_codes
            and (
                evidence.unit.metadata_verification_status != "VERIFIED"
                or evidence.unit.validity_status in {None, "UNKNOWN"}
            )
        )

        payload = bundle.model_dump(mode="json", exclude={"bundle_hash"})
        payload["binding_profile_version"] = self.profile_version
        payload["binding_status"] = binding_status
        payload["mapped_check_codes"] = mapped_check_codes
        payload["unmapped_check_codes"] = unmapped_check_codes
        payload["unmapped_issue_ids"] = unmapped_issue_ids
        payload["unverified_evidence_ids"] = unverified_evidence_ids
        if payload["status"] == "READY" and (
            binding_status != "COMPLETE" or unverified_evidence_ids
        ):
            payload["status"] = "DEGRADED"
        payload["evidence"] = [
            evidence.model_dump(mode="json") for evidence in bound_evidence
        ]
        bundle_hash = "sha256:" + hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        return LegalEvidenceBundle(bundle_hash=bundle_hash, **payload)
