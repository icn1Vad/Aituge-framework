import asyncio
import json

import httpx

from backend.local_code_chat_app import create_app
from common.encrypt_utils import encrypt_key
from common.llm.models import TextChunk
from common.system_constants import DEFAULT_TENANT_ID
from db.db_context import create_db_session, init_db, reset_engine_for_test
from db.models.llm import LlmModelEntity
import service.agent.single_agent_runner as runner_mod
from task_manager.runtime.broker import reset_event_broker_for_test


class ChangeProposalAgent:
    calls = 0
    tool_counts: list[int] = []

    def __init__(self, llm, system_prompt, tools):
        assert "Media Script Change Proposal" in system_prompt
        type(self).tool_counts.append(len(tools))

    async def run_async(self, state):
        type(self).calls += 1
        proposal = {
            "status": "pending_confirmation",
            "summary": "Rewrite only the opening hook and preserve the evidence boundary.",
            "reason": "The current opening delays the main conclusion.",
            "target_fields": ["hook_3s"],
            "changes": [
                {
                    "field": "hook_3s",
                    "instruction": "Lead with the eligibility conclusion in the first sentence.",
                }
            ],
            "preserve_fields": ["persona_name", "voiceover_evidence", "source_refs"],
            "storyboard_regeneration_required": True,
            "warnings": ["Do not strengthen the policy claim beyond the supplied source."],
            "clarification_question": "",
        }

        async def generate():
            content = json.dumps(proposal, ensure_ascii=False)
            midpoint = len(content) // 2
            yield TextChunk(delta=content[:midpoint])
            yield TextChunk(delta=content[midpoint:])

        return generate()


async def _seed_llm_config():
    async with create_db_session() as session:
        session.add(
            LlmModelEntity(
                tenant_id=DEFAULT_TENANT_ID,
                base_url="http://example.test/v1",
                model="deepseek-v4-pro",
                model_name="deepseek-v4-pro",
                model_id="deepseek-v4-pro",
                encrypted_api_key=encrypt_key("test-key"),
                provider_name="openai_like",
                source="openai_like",
            )
        )


async def _wait_for_status(client, run_id, headers, expected, timeout=20):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/task-manager/runs/{run_id}", headers=headers)
        assert response.status_code == 200
        status = response.json()["run"]["status"]
        if status in expected:
            return status
        await asyncio.sleep(0.03)
    raise AssertionError(f"Run {run_id} did not reach {expected}.")


def test_change_proposal_pipeline_pauses_with_artifact_and_resumes_without_second_agent_call(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'proposal.db'}")
        monkeypatch.setenv("TASK_EVENT_BROKER", "memory")
        monkeypatch.setenv("TASK_EXECUTOR_LOCK_BACKEND", "memory")
        reset_engine_for_test()
        reset_event_broker_for_test()
        await init_db()
        await _seed_llm_config()
        ChangeProposalAgent.calls = 0
        ChangeProposalAgent.tool_counts = []
        monkeypatch.setattr(runner_mod, "ReactAgent", ChangeProposalAgent)

        app = create_app()
        headers = {"X-User-Id": "proposal-user", "X-Tenant-Id": DEFAULT_TENANT_ID}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "media.script.change.propose",
                    "input_payload": {
                        "message": "Make the opening more direct but keep all policy boundaries.",
                        "base_script_id": "script-1",
                        "base_artifact_id": "artifact-script-1",
                        "conversation_thread_id": "script:script-1",
                        "current_script": {
                            "persona_name": "Yan Jie",
                            "hook_3s": "Let us first review the background.",
                            "voiceover": "Existing policy-bound script.",
                        },
                        "topic_card": {"topic_name": "Veteran employment eligibility"},
                        "persona": {"display_name": "Yan Jie"},
                        "user_constraints": {"duration_seconds": 60},
                    },
                },
            )
            assert created.status_code == 200, created.text
            task_id = created.json()["task"]["id"]

            started = await client.post(f"/task-manager/tasks/{task_id}/runs", headers=headers, json={})
            assert started.status_code == 200, started.text
            run_id = started.json()["run_id"]
            assert await _wait_for_status(client, run_id, headers, {"waiting_human", "failed"}) == "waiting_human"

            stages = (await client.get(f"/task-manager/runs/{run_id}/stages", headers=headers)).json()["stages"]
            assert [(item["stage_id"], item["status"]) for item in stages] == [
                ("context", "succeeded"),
                ("propose", "succeeded"),
                ("await_confirmation", "waiting_human"),
            ]

            artifacts = (await client.get(f"/task-manager/tasks/{task_id}/artifacts", headers=headers)).json()["artifacts"]
            artifact_types = [item["artifact_type"] for item in artifacts]
            assert artifact_types == [
                "media_script_change_context",
                "media_script_change_proposal_draft",
                "media_script_change_proposal",
            ]
            proposal = artifacts[-1]["content_json"]
            assert proposal["status"] == "pending_confirmation"
            assert proposal["target_fields"] == ["hook_3s"]
            proposal_artifact_id = artifacts[-1]["id"]

            events = (await client.get(f"/task-manager/runs/{run_id}/events", headers=headers)).json()["events"]
            review_event = next(item for item in events if item["event_type"] == "human_review_required")
            assert review_event["payload"]["review_artifact_id"] == proposal_artifact_id
            assert review_event["payload"]["allowed_actions"] == ["approve", "reject", "revise_input"]
            assert ChangeProposalAgent.calls == 1
            assert ChangeProposalAgent.tool_counts == [0]

            approved = await client.post(
                f"/task-manager/runs/{run_id}/review",
                headers=headers,
                json={
                    "action": "approve",
                    "comment": "Apply this bounded proposal in a later revision run.",
                    "resume_from_stage": "await_confirmation",
                },
            )
            assert approved.status_code == 200, approved.text
            assert await _wait_for_status(client, run_id, headers, {"succeeded", "failed"}) == "succeeded"
            assert ChangeProposalAgent.calls == 1

            final_artifacts = (
                await client.get(f"/task-manager/tasks/{task_id}/artifacts", headers=headers)
            ).json()["artifacts"]
            proposal_versions = [
                item for item in final_artifacts if item["artifact_type"] == "media_script_change_proposal"
            ]
            assert [item["artifact_version"] for item in proposal_versions] == [1, 2]
            assert proposal_versions[0]["checksum"] == proposal_versions[1]["checksum"]

    try:
        asyncio.run(run())
    finally:
        reset_event_broker_for_test()
        reset_engine_for_test()
