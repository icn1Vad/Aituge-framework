from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

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
            "local_tool": [],
            "pipeline": [],
            "result_sink": [],
            "stage_handler": [],
        }

    def register_task(self, **kwargs) -> None:
        self.calls["task"].append(kwargs)

    def register_resource_task(self, **kwargs) -> None:
        self.calls["task"].append(kwargs)

    def register_agent(self, **kwargs) -> None:
        self.calls["agent"].append(kwargs)

    def register_skill_package(self, **kwargs) -> None:
        self.calls["skill_package"].append(kwargs)

    def register_skill_root(self, path: Path) -> None:
        self.calls["skill_root"].append({"path": path})

    def register_http_tool(self, **kwargs) -> None:
        self.calls["http_tool"].append(kwargs)

    def register_local_tool(self, **kwargs) -> None:
        self.calls["local_tool"].append(kwargs)

    def register_pipeline(self, **kwargs) -> None:
        self.calls["pipeline"].append(kwargs)

    def register_result_sink(self, **kwargs) -> None:
        self.calls["result_sink"].append(kwargs)

    def register_stage_handler(self, **kwargs) -> None:
        self.calls["stage_handler"].append(kwargs)


def test_proof_capability_declares_minimal_qa_runtime():
    registry = _Registry()

    asyncio.run(
        proof_capability.register(
            registry,
            _Settings(
                {
                    "PROOF_SERVICE_BASE_URL": "http://proof:18100",
                }
            ),
        )
    )

    sink = registry.calls["result_sink"][0]
    assert sink["task_type"] == "proof.audit.run"
    assert sink["required"] is True
    assert callable(sink["handler"])
    task = registry.calls["task"][0]
    agent = registry.calls["agent"][0]
    package = registry.calls["skill_package"][0]
    tools = {tool["tool_name"]: tool for tool in registry.calls["http_tool"]}
    local_tools = {
        tool["tool_name"]: tool for tool in registry.calls["local_tool"]
    }
    tool = tools["proof_search"]
    html_tool = local_tools["html_report_renderer"]
    skill_root = registry.calls["skill_root"][0]["path"]

    assert task["task_type"] == "proof.qa.chat"
    assert task["resource_pool"] == "proof-library"
    assert task["access_mode"] == "read"
    assert task["handler"] == "scheduler"
    assert task["default_agent_id"] == "proof-qa-agent"
    assert task["default_skill_package"] == "proof-policy-qa-package"
    assert task["default_tools"] == [
        "proof_search",
        "proof_sql",
        "code_interpreter",
        "html_report_renderer",
    ]
    assert task["stream_chunk_chars"] == 24
    assert task["conversation_message_field"] == "question"
    assert task["input_model"] is proof_capability.ProofQaInput
    assert agent["agent_type"] == "single"
    assert agent["default_tools"] == [
        "proof_search",
        "proof_sql",
        "code_interpreter",
        "html_report_renderer",
    ]
    assert package["primary_skill"] == "proof-policy-qa"
    assert package["auxiliary_skills"] == ["proof-policy-sql", "proof-html-report"]
    assert tool["tool_name"] == "proof_search"
    assert tool["base_url"] == "http://proof:18100"
    assert tool["path"] == "/v1/retrieval/search"
    assert tools["proof_sql"]["path"] == "/v1/query/sql"
    assert html_tool["provider"] == "proof_structured_html"
    assert html_tool["llm_tool_names"] == ["RenderHtmlReport"]
    assert callable(html_tool["factory"])
    assert (skill_root / "proof-policy-qa" / "SKILL.md").is_file()
    assert (skill_root / "proof-policy-sql" / "SKILL.md").is_file()
    assert (skill_root / "proof-html-report" / "SKILL.md").is_file()
    audit_task = registry.calls["task"][1]
    agents = {item["agent_id"]: item for item in registry.calls["agent"]}
    packages = {item["package_name"]: item for item in registry.calls["skill_package"]}
    audit_agent = agents["proof-audit-agent"]
    audit_package = packages["proof-policy-semantic-audit-package"]
    assert audit_task["task_type"] == "proof.audit.run"
    assert audit_task["resource_pool"] == "proof-library"
    assert audit_task["access_mode"] == "read"
    assert audit_task["handler"] == "pipeline"
    assert audit_task["input_model"] is proof_capability.ProofAuditInput
    assert audit_task["result_sink_url"] == (
        "http://proof:18100/v1/internal/semantic-audits/result"
    )
    assert audit_agent["agent_id"] == "proof-audit-agent"
    assert audit_agent["default_tools"] == []
    assert audit_package["primary_skill"] == "proof-policy-semantic-audit"
    assert (skill_root / "proof-policy-semantic-audit" / "SKILL.md").is_file()
    pipeline = registry.calls["pipeline"][0]
    stages = {stage["stage_id"]: stage for stage in pipeline["stages"]}
    assert pipeline["max_parallelism"] == 4
    assert stages["semantic_audit"]["item_source"] == "semantic_items"
    assert stages["conflict_audit"]["item_source"] == "conflict_items"
    assert stages["conflict_audit"]["agent_id"] == "proof-conflict-agent"
    assert stages["conflict_audit"]["artifact_type"] == "proof_conflict_audit"
    assert stages["semantic_audit"]["item_failure_policy"] == "continue"
    assert stages["semantic_audit"]["failure_policy"] == "continue_with_warning"
    assert stages["conflict_audit"]["item_failure_policy"] == "continue"
    assert stages["conflict_audit"]["failure_policy"] == "continue_with_warning"
    assert stages["intra_conflict_audit"]["item_source"] == "intra_conflict_items"
    assert stages["intra_conflict_audit"]["item_output_model"] is (
        proof_capability.ProofIntraConflictItemOutput
    )
    assert stages["intra_conflict_audit"]["agent_id"] == "proof-intra-conflict-agent"
    assert stages["intra_conflict_audit"]["tools"] == ["proof_intra_conflict_search"]
    assert stages["intra_conflict_audit"]["item_failure_policy"] == "continue"
    assert stages["intra_conflict_audit"]["failure_policy"] == "continue_with_warning"
    assert set(stages["finalize_report"]["depends_on"]) == {
        "policy_summary", "semantic_audit", "conflict_audit", "intra_conflict_audit"
    }
    assert not any(task["task_type"] == "proof.intra.conflict.audit" for task in registry.calls["task"])
    mutation_task = next(
        item for item in registry.calls["task"]
        if item["task_type"] == "proof.policy.mutate"
    )
    assert mutation_task["resource_pool"] == "proof-library"
    assert mutation_task["access_mode"] == "write"
    conflict_task = next(
        item for item in registry.calls["task"]
        if item["task_type"] == "proof.conflict.audit"
    )
    conflict_agent = agents["proof-conflict-agent"]
    conflict_package = packages["proof-policy-conflict-audit-package"]
    assert conflict_task["task_type"] == "proof.conflict.audit"
    assert conflict_task["handler"] == "batch_item_scheduler"
    assert conflict_task["input_model"] is proof_capability.ProofConflictAuditInput
    assert conflict_task["item_output_model"] is proof_capability.ProofConflictItemOutput
    assert conflict_task["default_tools"] == ["proof_conflict_search"]
    assert conflict_task["result_sink_url"] == (
        "http://proof:18100/v1/internal/conflict-audits/result"
    )
    assert conflict_agent["agent_id"] == "proof-conflict-agent"
    assert conflict_agent["default_tools"] == ["proof_conflict_search"]
    assert conflict_package["primary_skill"] == "proof-policy-conflict-audit"
    assert tools["proof_conflict_search"]["path"] == "/v1/internal/conflict-retrieval"
    assert (skill_root / "proof-policy-conflict-audit" / "SKILL.md").is_file()
    assert agents["proof-intra-conflict-agent"]["default_tools"] == [
        "proof_intra_conflict_search"
    ]
    assert packages["proof-policy-intra-conflict-audit-package"]["primary_skill"] == (
        "proof-policy-intra-conflict-audit"
    )
    assert tools["proof_intra_conflict_search"]["path"] == "/v1/internal/intra-conflict-retrieval"
    assert (skill_root / "proof-policy-intra-conflict-audit" / "SKILL.md").is_file()


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
    payload = proof_capability.ProofQaInput(
        question="  关联交易如何审批？  ",
        model_id="  deepseek-v4-pro  ",
    )

    assert payload.question == "关联交易如何审批？"
    assert payload.top_k == 8
    assert payload.model_id == "deepseek-v4-pro"
    with pytest.raises(ValidationError):
        proof_capability.ProofQaInput(question=" ")
    with pytest.raises(ValidationError):
        proof_capability.ProofQaInput(question="审批", top_k=21)
    with pytest.raises(ValidationError):
        proof_capability.ProofQaInput(question="审批", model_id=" ")


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
    assert "ReadSkill" in content
    assert "proof-policy-sql" in content
    assert "proof-html-report" in content
    assert "citation.label" in content


def test_html_report_skill_uses_structured_renderer_instead_of_python():
    content = (
        Path(proof_capability.__file__).resolve().parent
        / "skills"
        / "proof-html-report"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "RenderHtmlReport" in content
    assert "Do not generate Python" in content
    assert "Do not use `code_interpreter`" in content
    assert "Distinguish analytical coverage from record enumeration" in content
    assert "Do not query the complete policy list" in content


def test_sql_auxiliary_skill_has_complete_schema_and_business_mappings():
    content = (
        Path(proof_capability.__file__).resolve().parent
        / "skills"
        / "proof-policy-sql"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "proof_sql_policy_v" in content
    assert "proof_sql_clause_v" in content
    assert "policy_status = 'effective'" in content
    assert "`upper`" in content
    assert "`peer`" in content
    assert "`lower`" in content
    assert "`file_type`" in content
    assert "`created_at`" in content
    assert "`updated_at`" in content
    assert "`original_name`" in content
    assert "`document_status`" in content
    assert "`structure_profile`" in content
    assert "LIKE '%金额%'" in content
    assert "ILIKE '%审批%'" in content
    assert "SUM(matching_clause_count) OVER ()" in content


def test_policy_summary_skill_uses_compact_identifier_free_output():
    content = (
        Path(proof_capability.__file__).resolve().parent
        / "skills"
        / "proof-policy-summary"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "do not copy chunk IDs" in content
    assert '"summary"' in content
    assert '"source_ids"' not in content
    assert '"responsibilities"' not in content
    assert '"key_process"' not in content
    assert '"exceptions"' not in content
    assert "There is no\nrequired item count" in content


def test_policy_summary_output_contract_accepts_compact_semantic_outline():
    output = proof_capability.ProofPolicySummaryOutput.model_validate(
        {
            "plain_summary": "一、制度定位与总体框架\n制度用于规范投资事项。",
            "purpose": "规范投资决策和执行。",
            "scope": ["公司及其子公司的对外投资。"],
            "concerned_roles": [
                {
                    "role": "董事会",
                    "summary": "审议权限范围内的投资事项，并监督执行情况。",
                }
            ],
            "key_rules": ["达到规定标准的事项应提交股东大会审议。"],
        }
    ).model_dump()

    assert output["concerned_roles"] == [
        {
            "role": "董事会",
            "summary": "审议权限范围内的投资事项，并监督执行情况。",
        }
    ]
    assert "source_ids" not in str(output)


def test_policy_summary_output_contract_rejects_removed_key_process_field():
    with pytest.raises(ValidationError):
        proof_capability.ProofPolicySummaryOutput.model_validate(
            {
                "plain_summary": "制度概览",
                "key_process": [],
            }
        )


def test_policy_summary_output_contract_rejects_removed_exceptions_field():
    with pytest.raises(ValidationError):
        proof_capability.ProofPolicySummaryOutput.model_validate(
            {
                "plain_summary": "制度概览",
                "exceptions": [],
            }
        )


def test_audit_skill_defines_production_clarity_and_executability_rules():
    content = (
        Path(proof_capability.__file__).resolve().parent
        / "skills"
        / "proof-policy-semantic-audit"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "核心原则" in content
    assert "语义歧义" in content
    assert "可执行性缺口" in content
    assert "抑制误报" in content
    assert "不是封闭清单" in content
    assert "每个有问题的 target 最多返回一条" in content
    assert '"findings"' in content
    assert '"target_ref"' in content
    assert '"id"' not in content
    assert '"quote"' not in content
    assert '"problem"' in content
    assert '"suggestion"' in content


def test_audit_contract_rejects_duplicate_targets_and_findings():
    batch = {
        "id": "batch-1",
        "audit_id": "audit-1",
        "check": "semantic",
        "targets": [{"id": "unit-1", "ref": "T01", "text": "相关部门应及时处理。"}],
    }
    payload = proof_capability.ProofAuditInput(
        audit_id="audit-1",
        document_id="document-1",
        summary_chunks=[{"id": "unit-1", "text": "相关部门应及时处理。"}],
        semantic_items=[batch],
        conflict_items=[{
            "id": "conflict-1",
            "audit_id": "audit-1",
            "check": "conflict",
            "targets": [{"id": "unit-1", "unit_id": "unit-1", "text": "相关部门应及时处理。"}],
        }],
        intra_conflict_items=[{
            "id": "intra-1",
            "audit_id": "audit-1",
            "check": "conflict",
            "targets": [{"id": "unit-1", "unit_id": "unit-1", "text": "相关部门应及时处理。"}],
        }],
    )
    assert payload.failure_policy == "fail_fast"

    with pytest.raises(ValidationError):
        proof_capability.ProofAuditInput(
            audit_id="audit-1",
            document_id="document-1",
            summary_chunks=[{"id": "unit-1", "text": "相关部门应及时处理。"}],
            semantic_items=[batch, {**batch, "id": "batch-2"}],
            conflict_items=[{
                "id": "conflict-1",
                "audit_id": "audit-1",
                "check": "conflict",
                "targets": [{"id": "unit-1", "unit_id": "unit-1"}],
            }],
            intra_conflict_items=[{
                "id": "intra-1",
                "audit_id": "audit-1",
                "check": "conflict",
                "targets": [{"id": "unit-1", "unit_id": "unit-1"}],
            }],
        )
    with pytest.raises(ValidationError):
        proof_capability.ProofAuditItemOutput(
            findings=[
                {"target_ref": "T01", "category": "semantic_ambiguity", "problem": "主体不明", "suggestion": "明确部门"},
                {"target_ref": "T01", "category": "semantic_ambiguity", "problem": "时限不明", "suggestion": "明确时限"},
            ]
        )


def test_semantic_contract_uses_short_refs_without_chunk_ids():
    output = proof_capability.ProofAuditItemOutput(
        findings=[{
            "target_ref": " t01 ",
            "category": "semantic_ambiguity",
            "problem": "责任主体不明确。",
            "suggestion": "明确责任部门。",
        }]
    )

    assert output.findings[0].target_ref == "T01"
    dumped = output.model_dump()["findings"][0]
    assert set(dumped) == {"target_ref", "category", "problem", "suggestion"}
    assert "id" not in dumped

    with pytest.raises(ValidationError):
        proof_capability.ProofAuditItemOutput(
            findings=[{
                "target_ref": "T09",
                "category": "semantic_ambiguity",
                "problem": "责任主体不明确。",
                "suggestion": "明确责任部门。",
            }]
        )


def test_intra_conflict_contract_uses_short_refs_without_chunk_ids():
    output = proof_capability.ProofIntraConflictItemOutput(
        findings=[{
            "candidate_refs": ["c01"],
            "conflict_type": "numeric_conflict",
            "problem": "相同事项的期限不同。",
            "suggestion": "统一期限。",
        }]
    )

    assert output.findings[0].candidate_refs == ["C01"]
    dumped = output.model_dump()
    assert "candidate_refs" in str(dumped)
    assert "candidate_ids" not in str(dumped)
    assert "id" not in dumped["findings"][0]

    with pytest.raises(ValidationError):
        proof_capability.ProofIntraConflictItemOutput(
            findings=[{
                "candidate_refs": ["C11"],
                "conflict_type": "numeric_conflict",
                "problem": "相同事项的期限不同。",
                "suggestion": "统一期限。",
            }]
        )

    content = (
        Path(proof_capability.__file__).resolve().parent
        / "skills"
        / "proof-policy-intra-conflict-audit"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "candidate_refs" in content
    assert "C01 到 C10" in content
    assert "不要输出当前 target ID 或任何 Chunk ID" in content


def test_conflict_contract_and_skill_define_short_ref_joint_judge():
    batch = {
        "id": "batch-1",
        "audit_id": "audit-1",
        "check": "conflict",
        "targets": [{"id": "unit-1", "unit_id": "unit-1", "text": "报销时限为三十日。"}],
    }
    payload = proof_capability.ProofConflictAuditInput(
        audit_id="audit-1",
        document_id="document-1",
        items=[batch],
    )
    assert payload.max_concurrency == 4

    finding = proof_capability.ProofConflictItemOutput(
        findings=[
            {
                "candidate_refs": ["C01"],
                "conflict_type": "numeric_conflict",
                "problem": "同一期限分别要求三十日和十五日。",
                "suggestion": "统一期限并明确适用版本。",
            }
        ]
    )
    assert finding.findings[0].conflict_type == "numeric_conflict"
    dumped = finding.model_dump()["findings"][0]
    assert set(dumped) == {"candidate_refs", "conflict_type", "problem", "suggestion"}
    assert "id" not in dumped

    with pytest.raises(ValidationError):
        proof_capability.ProofConflictAuditInput(
            audit_id="audit-1",
            items=[{**batch, "targets": [{"id": "alias", "unit_id": "unit-1"}]}],
        )

    content = (
        Path(proof_capability.__file__).resolve().parent
        / "skills"
        / "proof-policy-conflict-audit"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "proof_conflict_search" in content
    assert "同一事项" in content
    assert "适用范围重叠" in content
    assert "无法同时满足" in content
    assert "candidate_refs" in content
    assert "不要输出当前 target ID 或任何 Chunk ID" in content
    assert '"findings"' in content


def test_proof_result_sink_forwards_task_tenant(monkeypatch) -> None:
    calls = []

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"base_url": "http://proof:18100", "timeout": 15}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, path, *, json, headers):
            calls.append((path, json, headers))
            return Response()

    monkeypatch.setattr(proof_capability.httpx, "AsyncClient", Client)
    handler = proof_capability._proof_result_sink_handler("http://proof:18100")
    delivery = SimpleNamespace(
        task=SimpleNamespace(
            id="task-2",
            current_run_id="run-2",
            task_type="proof.audit.run",
            tenant_id="2",
            input_payload_json={"audit_id": "audit-2"},
        ),
        stage_id=None,
        status="completed",
        output={"ok": True},
        error_message=None,
        error_code=None,
        error_details=None,
    )

    asyncio.run(handler(delivery))

    assert calls[0][2] == {"X-Tenant-ID": "2"}
