from fastapi.testclient import TestClient

from qianxuesen_mentor.api import create_app
from qianxuesen_mentor.config import Settings


class FakeService:
    def bootstrap(self): pass
    def health(self): return {"status": "ok"}
    def list_files(self): return {"items": [{"id": "qxs-1", "title": "工程控制论"}], "total": 1}
    def list_chunks(self, file_id, *, limit, offset):
        return {"file_id": file_id, "items": [], "limit": limit, "offset": offset, "total": 0, "has_more": False}
    def search(self, query, *, top_k, retrieval_mode):
        return {"query": query, "results": [], "top_k": top_k, "retrieval_mode": retrieval_mode}
    def execute_sql(self, question, sql): return {"question": question, "sql": sql, "rows": []}


def test_public_api_contracts():
    with TestClient(create_app(Settings(database_url="postgresql://unused"), FakeService())) as client:
        assert client.get("/health").json()["data"]["status"] == "ok"
        files = client.get("/v1/files").json()["data"]
        assert files["total"] == 1
        assert client.get("/v1/files/qxs-1/chunks").status_code == 200
        assert client.post("/v1/retrieval/search", json={"query": "系统工程"}).status_code == 200
        assert client.post("/v1/query/sql", json={"question": "多少", "sql": "SELECT 1"}).status_code == 200
