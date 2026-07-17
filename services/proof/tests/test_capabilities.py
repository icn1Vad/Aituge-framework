from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from capabilities import register as proof_capability


class _Settings:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str, default: str = "") -> str:
        return self.values.get(name, default)

    def require(self, name: str) -> str:
        value = self.get(name).strip()
        if not value:
            raise ValueError(name)
        return value


class _Registry:
    def __init__(self) -> None:
        self.calls: dict[str, list[dict]] = {
            "task": [],
            "agent": [],
            "skill_package": [],
            "skill_root": [],
            "http_tool": [],
        }

    def register_task(self, **kwargs) -> None:
        self.calls["task"].append(kwargs)

    def register_agent(self, **kwargs) -> None:
        self.calls["agent"].append(kwargs)

    def register_skill_package(self, **kwargs) -> None:
        self.calls["skill_package"].append(kwargs)

    def register_skill_root(self, path: Path) -> None:
        self.calls["skill_root"].append({"path": path})

    def register_http_tool(self, **kwargs) -> None:
        self.calls["http_tool"].append(kwargs)


def test_proof_capability_declares_minimal_qa_runtime():
    registry = _Registry()

    asyncio.run(
        proof_capability.register(
            registry,
            _Settings(
                {
                    "PROOF_SERVICE_BASE_URL": "http://proof:18100",
                    "PROOF_QA_MODEL_ID": "deepseek-v4-pro",
                    "PROOF_AUDIT_MODEL_ID": "deepseek-v4-pro",
                }
            ),
        )
    )

    task = registry.calls["task"][0]
    agent = registry.calls["agent"][0]
    package = registry.calls["skill_package"][0]
    tools = {tool["tool_name"]: tool for tool in registry.calls["http_tool"]}
    tool = tools["proof_search"]
    skill_root = registry.calls["skill_root"][0]["path"]

    assert task["task_type"] == "proof.qa.chat"
    assert task["handler"] == "scheduler"
    assert task["default_agent_id"] == "proof-qa-agent"
    assert task["default_skill_package"] == "proof-policy-qa-package"
    assert task["default_tools"] == ["proof_search", "proof_sql", "code_interpreter"]
    assert task["stream_chunk_chars"] == 24
    assert task["conversation_message_field"] == "question"
    assert task["input_model"] is proof_capability.ProofQaInput
    assert agent["agent_type"] == "single"
    assert agent["default_tools"] == ["proof_search", "proof_sql", "code_interpreter"]
    assert package["primary_skill"] == "proof-policy-qa"
    assert tool["tool_name"] == "proof_search"
    assert tool["base_url"] == "http://proof:18100"
    assert tool["path"] == "/v1/retrieval/search"
    assert tools["proof_sql"]["path"] == "/v1/query/sql"
    assert (skill_root / "proof-policy-qa" / "SKILL.md").is_file()
    audit_task = registry.calls["task"][1]
    audit_agent = registry.calls["agent"][1]
    audit_package = registry.calls["skill_package"][1]
    assert audit_task["task_type"] == "proof.audit.run"
    assert audit_task["handler"] == "batch_item_scheduler"
    assert audit_task["input_model"] is proof_capability.ProofAuditInput
    assert audit_task["item_output_model"] is proof_capability.ProofAuditItemOutput
    assert audit_task["result_sink_url"] == (
        "http://proof:18100/v1/internal/semantic-audits/result"
    )
    assert audit_agent["agent_id"] == "proof-audit-agent"
    assert audit_agent["default_tools"] == []
    assert audit_package["primary_skill"] == "proof-policy-semantic-audit"
    assert (skill_root / "proof-policy-semantic-audit" / "SKILL.md").is_file()


def test_proof_search_input_matches_retrieval_api_contract():
    payload = proof_capability.ProofSearchInput(
        query="  审批权限  ",
        top_k=20,
        retrieval_mode="keyword",
        policy_ids=["policy-1"],
        level_codes=["upper"],
        category_codes=["governance"],
    )

    assert payload.query == "审批权限"
    assert payload.top_k == 20
    assert payload.retrieval_mode == "keyword"
    with pytest.raises(ValidationError):
        proof_capability.ProofSearchInput(query=" ")
    with pytest.raises(ValidationError):
        proof_capability.ProofSearchInput(query="审批", top_k=21)


def test_proof_qa_input_normalizes_question_and_validates_top_k():
    payload = proof_capability.ProofQaInput(question="  关联交易如何审批？  ")

    assert payload.question == "关联交易如何审批？"
    assert payload.top_k == 8
    with pytest.raises(ValidationError):
        proof_capability.ProofQaInput(question=" ")
    with pytest.raises(ValidationError):
        proof_capability.ProofQaInput(question="审批", top_k=21)


def test_primary_skill_requires_search_and_chunk_citations():
    content = (
        Path(proof_capability.__file__).resolve().parent
        / "skills"
        / "proof-policy-qa"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "proof_search" in content
    assert "[制度名称｜条款编号｜Chunk #序号]" in content
    assert "do not answer from memory" in content.lower()
    assert "code_interpreter" in content
    assert "proof_sql" in content
    assert "proof_sql_policy_v" in content
    assert "citation.label" in content


def test_audit_contract_rejects_duplicate_targets_and_findings():
    batch = {
        "id": "batch-1",
        "audit_id": "audit-1",
        "check": "semantic",
        "targets": [{"id": "unit-1", "text": "相关部门应及时处理。"}],
    }
    payload = proof_capability.ProofAuditInput(
        audit_id="audit-1",
        document_id="document-1",
        items=[batch],
    )
    assert payload.failure_policy == "fail_fast"

    with pytest.raises(ValidationError):
        proof_capability.ProofAuditInput(
            audit_id="audit-1",
            document_id="document-1",
            items=[batch, {**batch, "id": "batch-2"}],
        )
    with pytest.raises(ValidationError):
        proof_capability.ProofAuditItemOutput(
            findings=[
                {"id": "unit-1", "quote": "相关部门", "problem": "主体不明", "suggestion": "明确部门"},
                {"id": "unit-1", "quote": "及时处理", "problem": "时限不明", "suggestion": "明确时限"},
            ]
        )
