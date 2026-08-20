from qianxuesen_mentor.chunking import build_chunks


def test_chunks_keep_page_citations_and_overlap():
    chunks = build_chunks("doc", [
        {"page_no": 1, "effective_text": "第一章 系统工程\n" + "系统工程需要总体协调。" * 100},
        {"page_no": 2, "effective_text": "第二章 实践\n" + "实践是检验方法的重要环节。" * 100},
    ], max_chars=300, overlap=50)
    assert len(chunks) > 2
    assert all(item["page_start"] == item["page_end"] for item in chunks)
    assert {item["page_start"] for item in chunks} == {1, 2}
    assert all(len(item["content"]) <= 300 for item in chunks)
