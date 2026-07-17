from proof.domain.policy_grouping import infer_policy_category


def test_policy_group_inference_uses_business_title() -> None:
    assert infer_policy_category("采购管理办法（试行）") == "procurement_supply"
    assert infer_policy_category("供应商管理制度") == "procurement_supply"
    assert infer_policy_category("对外投资管理办法") == "external_investment"
    assert infer_policy_category("参股企业管理办法") == "subsidiary_equity"
    assert infer_policy_category("人员招聘管理办法") == "recruitment_employment"
    assert infer_policy_category("薪酬管理办法") == "compensation_performance"
    assert infer_policy_category("考勤与休假管理制度") == "attendance_leave"
    assert infer_policy_category("信息化建设管理办法") == "digital_it"
    assert infer_policy_category("未知专项规定") == "other"
