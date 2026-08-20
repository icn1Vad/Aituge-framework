from qianxuesen_mentor.catalog import CATALOG, EXPECTED_DOCUMENTS, EXPECTED_PAGES


def test_fixed_catalog_has_expected_size():
    assert len(CATALOG) == EXPECTED_DOCUMENTS == 20
    assert sum(item.page_count for item in CATALOG) == EXPECTED_PAGES == 7492
    assert len({item.id for item in CATALOG}) == 20
    assert len({item.filename for item in CATALOG}) == 20
