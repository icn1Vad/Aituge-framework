import pytest

from qianxuesen_mentor.errors import QianXuesenError
from qianxuesen_mentor.sql_query import normalize_sql


def test_sql_allows_only_published_views():
    assert normalize_sql("SELECT * FROM qxs_sql_fact_v LIMIT 3").startswith("SELECT")
    with pytest.raises(QianXuesenError):
        normalize_sql("SELECT * FROM qxs_fact")
    with pytest.raises(QianXuesenError):
        normalize_sql("DELETE FROM qxs_sql_fact_v")
    with pytest.raises(QianXuesenError):
        normalize_sql("SELECT 1; SELECT 2")
