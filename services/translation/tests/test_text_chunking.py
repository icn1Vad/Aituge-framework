from translation_service.model.translator import _split_plain_text


def test_text_chunking_prefers_paragraph_boundaries() -> None:
    chunks = _split_plain_text("first\n\nsecond\n\nthird", 13)
    assert chunks == ["first\n\nsecond", "third"]


def test_oversized_paragraph_is_split_without_loss() -> None:
    source = "abcdefghij"
    chunks = _split_plain_text(source, 4)
    assert chunks == ["abcd", "efgh", "ij"]
    assert "".join(chunks) == source
