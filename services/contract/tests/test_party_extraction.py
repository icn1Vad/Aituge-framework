from __future__ import annotations

from contract.ir.models import IRParty, SourceAnchor
from contract.party import extract_party_candidates, extract_party_evidence
from contract.parser.models import ParsedContractBlock
from services.contract.capabilities.register import _unique_party_name


def _block(block_no: int, text: str, *, page_number: int | None = 1) -> ParsedContractBlock:
    return ParsedContractBlock(
        block_id=f"block-{block_no}",
        block_no=block_no,
        block_type="paragraph",
        text=text,
        page_number=page_number,
        paragraph_no=block_no,
        char_start=0,
        char_end=len(text),
        heading_path=[],
        metadata={},
    )


def _party(role: str, name: str, *pages: int) -> IRParty:
    return IRParty(
        role=role,
        name=name,
        source_anchors=[
            SourceAnchor(
                anchor_id=f"anchor-{role}-{page}",
                block_id=f"block-{page}",
                page_number=page,
                char_start=0,
                char_end=len(name),
            )
            for page in pages
        ],
    )


def _party_without_page(role: str, name: str, anchor_no: int) -> IRParty:
    return IRParty(
        role=role,
        name=name,
        source_anchors=[
            SourceAnchor(
                anchor_id=f"anchor-{role}-{anchor_no}",
                block_id=f"block-{anchor_no}",
                page_number=None,
                char_start=0,
                char_end=len(name),
            )
        ],
    )


def test_extracts_chinese_aliases_and_exact_source_anchors() -> None:
    block = _block(1, "委托方（甲方）：某某科技有限公司 受托方（乙方）：某某服务中心")

    candidates = extract_party_candidates([block])

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "某某科技有限公司"),
        ("PARTY_B", "某某服务中心"),
    ]
    for candidate in candidates:
        anchor = candidate.source_anchors[0]
        assert anchor.block_id == block.block_id
        assert anchor.page_number == 1
        assert block.text[anchor.char_start : anchor.char_end] == candidate.name


def test_extracts_construction_employer_and_contractor() -> None:
    evidence = extract_party_evidence(
        [
            _block(1, "发包人（全称）：淮安市淮阴区南陈集镇人民政府"),
            _block(2, "承包人（全称）：江苏建发市政工程有限公司"),
        ]
    )

    assert evidence.declared_roles == frozenset({"PARTY_A", "PARTY_B"})
    assert [(item.role, item.name) for item in evidence.candidates] == [
        ("PARTY_A", "淮安市淮阴区南陈集镇人民政府"),
        ("PARTY_B", "江苏建发市政工程有限公司"),
    ]


def test_extracts_paired_role_labels_without_colons() -> None:
    block = _block(
        1,
        "甲方（出租方）苏杨，乙方（承租方）西安市雁塔区丈八街道办事处",
    )

    candidates = extract_party_candidates([block])

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "苏杨"),
        ("PARTY_B", "西安市雁塔区丈八街道办事处"),
    ]
    for candidate in candidates:
        anchor = candidate.source_anchors[0]
        assert block.text[anchor.char_start : anchor.char_end] == candidate.name


def test_does_not_treat_unqualified_party_prose_as_paired_declaration() -> None:
    block = _block(1, "甲方应按时交付，乙方应按时付款。")

    assert extract_party_candidates([block]) == []


def test_extracts_wrapped_labels_and_advisory_role() -> None:
    samples = (
        "【甲方】：甲公司 【乙方】：乙公司",
        "[甲方]：甲公司 [乙方]：乙公司",
        "委托方：甲公司 顾问方：乙公司",
    )
    for text in samples:
        candidates = extract_party_candidates([_block(1, text)])
        assert [(item.role, item.name) for item in candidates] == [
            ("PARTY_A", "甲公司"),
            ("PARTY_B", "乙公司"),
        ]


def test_records_explicit_empty_role_without_treating_prose_as_declaration() -> None:
    evidence = extract_party_evidence([_block(1, "甲方（盖章）： 乙方：乙公司")])
    assert evidence.declared_roles == frozenset({"PARTY_A", "PARTY_B"})
    assert [(item.role, item.name) for item in evidence.candidates] == [("PARTY_B", "乙公司")]
    assert extract_party_evidence([_block(2, "甲方应交付，乙方应付款")]).declared_roles == frozenset()


def test_extracts_english_party_labels_from_one_block() -> None:
    block = _block(1, "Party A: Acme Holdings Ltd.; Party B: Beta Services LLC")

    candidates = extract_party_candidates([block])

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "Acme Holdings Ltd."),
        ("PARTY_B", "Beta Services LLC"),
    ]


def test_deduplicates_repeated_signature_names_and_keeps_all_anchors() -> None:
    candidates = extract_party_candidates(
        [
            _block(1, "甲方：某某有限公司"),
            _block(2, "甲方（盖章）：某某有限公司", page_number=3),
            _block(3, "乙方（盖章）："),
            _block(4, "乙方：（签字或盖章）"),
        ]
    )

    assert len(candidates) == 1
    assert candidates[0].role == "PARTY_A"
    assert candidates[0].name == "某某有限公司"
    assert [anchor.page_number for anchor in candidates[0].source_anchors] == [1, 3]


def test_strips_nested_entity_name_field_from_signature_party() -> None:
    block = _block(1, "乙方：单位名称：西安帝融商业运营管理有限公司", page_number=6)

    candidates = extract_party_candidates([block])

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_B", "西安帝融商业运营管理有限公司"),
    ]
    anchor = candidates[0].source_anchors[0]
    assert block.text[anchor.char_start : anchor.char_end] == candidates[0].name


def test_ignores_signature_stamp_ocr_code_but_keeps_declared_party() -> None:
    candidates = extract_party_candidates(
        [
            _block(1, "甲方：西安市中医医院"),
            _block(2, "甲方：西安市中医医院", page_number=2),
            _block(3, "甲方：（公章）网京120353地址：西安市凤城八路69号", page_number=6),
            _block(4, "乙方：西安帝融商业运营管理有限公司"),
        ]
    )

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "西安市中医医院"),
        ("PARTY_B", "西安帝融商业运营管理有限公司"),
    ]
    assert _unique_party_name(candidates, "PARTY_A") == "西安市中医医院"


def test_keeps_signed_organization_name_that_contains_digits() -> None:
    candidates = extract_party_candidates([_block(1, "甲方：（公章）西安第120工程有限公司")])
    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "（公章）西安第120工程有限公司")
    ]


def test_extracts_descriptive_roles_explicitly_aliased_as_parties() -> None:
    candidates = extract_party_candidates(
        [
            _block(1, "保证人(以下称甲方):上海市中小微企业政策性融资担保基金管理中心"),
            _block(2, "贷款人(以下称乙方):某某银行股份有限公司"),
        ]
    )

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "上海市中小微企业政策性融资担保基金管理中心"),
        ("PARTY_B", "某某银行股份有限公司"),
    ]


def test_records_blank_finance_lease_identity_fields_as_not_stated() -> None:
    evidence = extract_party_evidence(
        [_block(1, "1、出租人/注册地址/法定代表人 | 2、承租人/注册地址/法定代表人")]
    )

    assert evidence.declared_roles == frozenset({"PARTY_A", "PARTY_B"})
    assert evidence.candidates == []


def test_ignores_blank_finance_lease_contact_and_signature_fields() -> None:
    evidence = extract_party_evidence(
        [
            _block(1, "致出租人：收件人：地址：邮政编码：电话：传真：电子邮件："),
            _block(2, "致承租人：收件人：地址：邮政编码：电话：传真：电子邮箱："),
            _block(3, "出租人：（公章） 授权代表： 日期："),
            _block(4, "承租人：日期：授权代表：（公章）"),
        ]
    )

    assert evidence.declared_roles == frozenset({"PARTY_A", "PARTY_B"})
    assert evidence.candidates == []


def test_records_spaced_parenthetical_party_markers_as_not_stated() -> None:
    evidence = extract_party_evidence(
        [
            _block(1, "委 托 人: (甲 方)"),
            _block(2, "受 托 人： (乙 方)"),
        ]
    )

    assert evidence.declared_roles == frozenset({"PARTY_A", "PARTY_B"})
    assert evidence.candidates == []


def test_records_blank_shipping_roles_as_not_stated() -> None:
    evidence = extract_party_evidence(
        [_block(1, "托运人 | 全称 | | 承运人 | 全称 |")]
    )

    assert evidence.declared_roles == frozenset({"PARTY_A", "PARTY_B"})
    assert evidence.candidates == []


def test_records_blank_personal_information_contract_roles_as_not_stated() -> None:
    evidence = extract_party_evidence(
        [
            _block(1, "个人信息处理者："),
            _block(2, "地址："),
            _block(3, "境外接收方："),
            _block(4, "地址："),
        ]
    )

    assert evidence.declared_roles == frozenset({"PARTY_A", "PARTY_B"})
    assert evidence.candidates == []


def test_stops_empty_party_fields_before_identity_and_third_party_labels() -> None:
    evidence = extract_party_evidence(
        [
            _block(1, "甲方：证件号码：乙方：证件号码：丙方（借款人）：证件号码："),
            _block(2, "乙方：丙方（签字或公章）："),
        ]
    )

    assert evidence.declared_roles == frozenset({"PARTY_A", "PARTY_B"})
    assert evidence.candidates == []


def test_extracts_two_party_names_without_consuming_third_party_fields() -> None:
    candidates = extract_party_candidates(
        [_block(1, "甲方：张三 证件号码：123 乙方：李四 证件号码：456 丙方（担保人）：王五")]
    )

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "张三"),
        ("PARTY_B", "李四"),
    ]


def test_ignores_bare_signature_seal_placeholders() -> None:
    candidates = extract_party_candidates(
        [
            _block(1, "甲方：杭锦后旗公安局"),
            _block(2, "乙方：巴彦淖尔市诚馨物业服务有限公司"),
            _block(3, "甲方：（章）"),
            _block(4, "乙方：（章）"),
        ]
    )

    assert [(item.role, item.name) for item in candidates] == [
        ("PARTY_A", "杭锦后旗公安局"),
        ("PARTY_B", "巴彦淖尔市诚馨物业服务有限公司"),
    ]


def test_reconciles_repeated_full_name_with_late_ocr_truncation() -> None:
    candidates = [
        _party("PARTY_B", "西安帝融商业运营管理有限公司", 1, 2),
        _party("PARTY_B", "西安帝融商业运营管", 6),
    ]

    assert _unique_party_name(candidates, "PARTY_B") == "西安帝融商业运营管理有限公司"


def test_keeps_distinct_complete_legal_entities_unresolved() -> None:
    candidates = [
        _party("PARTY_B", "西安帝融商业运营管理有限公司", 1, 2),
        _party("PARTY_B", "西安帝融商业运营管理有限责任公司", 6),
    ]
    assert _unique_party_name(candidates, "PARTY_B") is None


def test_reconciles_truncation_with_single_full_name_evidence() -> None:
    candidates = [
        _party_without_page("PARTY_B", "西安帝融商业运营管理有限公司", 1),
        _party_without_page("PARTY_B", "西安帝融商业运营管", 2),
    ]
    assert _unique_party_name(candidates, "PARTY_B") == "西安帝融商业运营管理有限公司"


def test_does_not_apply_enterprise_prefix_rule_to_natural_people() -> None:
    candidates = [
        _party("PARTY_A", "张三", 1),
        _party("PARTY_A", "张三丰", 2),
    ]
    assert _unique_party_name(candidates, "PARTY_A") is None
