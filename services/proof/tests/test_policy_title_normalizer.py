from proof.application.conflict_retrieval.title_normalizer import normalize_policy_title


def test_policy_title_versions_share_one_normalized_key() -> None:
    assert normalize_policy_title("《采购管理办法（试行）》") == "采购管理"
    assert normalize_policy_title("采购管理制度（2025年修订版）") == "采购管理"
    assert normalize_policy_title("采购管理办法 第2版") == "采购管理"


def test_policy_title_normalization_does_not_merge_distinct_business_subjects() -> None:
    assert normalize_policy_title("采购管理办法") != normalize_policy_title("供应商管理制度")


def test_dataset_prefix_and_subsidiary_suffix_are_removed() -> None:
    assert normalize_policy_title("L3_采购管理办法_三级子公司局部制度_测试修改版") == "采购管理"
