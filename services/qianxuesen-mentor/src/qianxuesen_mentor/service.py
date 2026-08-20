from __future__ import annotations

from pathlib import Path
from typing import Any

from qianxuesen_mentor.answering import MentorAnswerService
from qianxuesen_mentor.catalog import EXPECTED_DOCUMENTS, EXPECTED_PAGES, validate_catalog
from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.model_clients import ModelClients
from qianxuesen_mentor.repository import Repository
from qianxuesen_mentor.retrieval import RetrievalService
from qianxuesen_mentor.sql_query import SqlQueryService
from qianxuesen_mentor.tools_runtime import CodeInterpreterTool, WebSearchTool


class MentorService:
    def __init__(self, settings: Settings, repository: Repository | None = None,
                 models: ModelClients | None = None) -> None:
        self.settings = settings
        self.repository = repository or Repository(settings)
        if models is None:
            try:
                models = ModelClients(settings)
            except Exception:
                models = None
        self.models = models
        self.retrieval = RetrievalService(settings, self.repository, models)
        self.web_search = WebSearchTool(settings)
        self.code_interpreter = CodeInterpreterTool(settings)
        self.answering = MentorAnswerService(
            self.retrieval,
            models,
            web_search=self.web_search,
            code_interpreter=self.code_interpreter,
        )
        self.sql = SqlQueryService(settings, self.repository)

    def bootstrap(self) -> None:
        self.repository.upsert_catalog()

    def health(self) -> dict[str, Any]:
        corpus_errors = validate_catalog(self.settings.resolved_corpus_root())
        return {"service": "qianxuesen-mentor", "status": "ok" if not corpus_errors else "degraded",
                "expected_documents": EXPECTED_DOCUMENTS, "expected_pages": EXPECTED_PAGES,
                "corpus_errors": corpus_errors, "models_configured": self.models is not None,
                "tools": {
                    "qxs_retrieve": True,
                    "web_search": self.web_search.enabled,
                    "code_interpreter": self.code_interpreter.available,
                },
                "database": self.repository.health()}

    def list_files(self) -> dict[str, Any]:
        return self.repository.list_files()

    def file_content(self, document_id: str) -> dict[str, str]:
        document = self.repository.get_document(document_id)
        path = Path(document["storage_path"]).resolve()
        corpus_root = self.settings.resolved_corpus_root()
        if not path.is_file() or not path.is_relative_to(corpus_root):
            from qianxuesen_mentor.errors import QianXuesenError
            raise QianXuesenError("file_not_found", "原始资料文件不存在", status_code=404)
        return {"path": str(path), "name": document["original_name"]}

    def list_chunks(self, document_id: str, *, limit: int, offset: int) -> dict[str, Any]:
        return self.repository.list_chunks(document_id, limit=limit, offset=offset)

    def search(self, query: str, *, top_k: int, retrieval_mode: str) -> dict[str, Any]:
        return self.retrieval.search(query, top_k=top_k, retrieval_mode=retrieval_mode)

    def execute_sql(self, question: str, sql: str) -> dict[str, Any]:
        return self.sql.execute(question, sql)
