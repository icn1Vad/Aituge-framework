from qianxuesen_mentor.ingestion import sanitize_unicode


def test_sanitize_unicode_combines_pairs_and_replaces_isolated_surrogates():
    assert sanitize_unicode("公式\ud835\udc00") == "公式𝐀"
    assert sanitize_unicode("异常\ud835字符") == "异常�字符"
    assert sanitize_unicode("空\x00字符") == "空�字符"
