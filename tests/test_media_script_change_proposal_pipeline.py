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
from task_manager.pipeline import media_script as media_pipeline
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


class ProposalAndRevisionAgent:
    calls: list[str] = []

    def __init__(self, llm, system_prompt, tools):
        if "Media Script Change Proposal" in system_prompt:
            self.stage = "proposal"
        elif "Media Script Writer" in system_prompt:
            self.stage = "writer"
        elif "Media Storyboard" in system_prompt:
            self.stage = "storyboard"
        else:
            raise AssertionError(system_prompt[:500])
        assert len(tools) == 0

    async def run_async(self, state):
        type(self).calls.append(self.stage)
        payloads = {
            "proposal": {
                "status": "pending_confirmation",
                "summary": "Rewrite only the opening hook.",
                "reason": "The conclusion should appear immediately.",
                "target_fields": ["hook_3s"],
                "changes": [{"field": "hook_3s", "instruction": "Lead with the eligibility conclusion."}],
                "preserve_fields": ["persona_name"],
                "storyboard_regeneration_required": True,
                "warnings": [],
                "clarification_question": "",
            },
            "writer": {
                "final_script": {
                    "topic_name": "Veteran employment eligibility",
                    "persona_name": "Yan Jie",
                    "video_goal": "Explain the eligibility boundary",
                    "platform": "douyin",
                    "duration_seconds": 60,
                    "duration_reason": "Fits a one-minute explanation.",
                    "target_char_range": [160, 220],
                    "hook_3s": "Not every veteran automatically qualifies; check these conditions first.",
                    "structure": ["hook", "conditions", "action"],
                    "voiceover": "Not every veteran automatically qualifies; check these conditions first.",
                    "subtitle_points": ["Check conditions"],
                    "visual_direction": ["Presenter on camera"],
                    "material_bridge": "Uses the supplied policy boundary.",
                    "master_library_usage": {},
                },
                "readable_script": "Not every veteran automatically qualifies; check these conditions first.",
                "hermes_agent_result": {
                    "status": "done",
                    "editor_summary": "Applied the approved hook-only proposal.",
                    "why_this_angle": "The conclusion is immediate.",
                    "risks": [],
                    "parse_notes": [],
                    "master_library_usage": {},
                },
            },
            "storyboard": {
                "storyboard": [
                    {
                        "time": "0-6s",
                        "scene": "Presenter",
                        "shot": "medium",
                        "action": "faces camera",
                        "voiceover": "Not every veteran automatically qualifies.",
                        "subtitle_focus": "Check conditions",
                        "visual_prompt": "presenter and policy document",
                    }
                ],
                "storyboard_plan": {"summary": "Open with the conclusion."},
                "visual_direction": ["Policy document close-up"],
                "warnings": [],
            },
        }

        async def generate():
            content = json.dumps(payloads[self.stage], ensure_ascii=False)
            yield TextChunk(delta=content)

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


def test_apply_proposal_creates_one_idempotent_revision_task_and_run(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'revision.db'}")
        monkeypatch.setenv("TASK_EVENT_BROKER", "memory")
        monkeypatch.setenv("TASK_EXECUTOR_LOCK_BACKEND", "memory")
        reset_engine_for_test()
        reset_event_broker_for_test()
        await init_db()
        await _seed_llm_config()
        ProposalAndRevisionAgent.calls = []
        monkeypatch.setattr(runner_mod, "ReactAgent", ProposalAndRevisionAgent)

        async def fake_gateway(path, payload, context):
            if path.endswith("/context"):
                assert payload["revision_mode"] is True
                assert payload["proposal_artifact_id"]
                return {
                    "topic_card": payload["topic_card"],
                    "source_brief": {"usable_level": "usable"},
                    "current_persona": {"display_name": "Yan Jie"},
                    "persona_context": {},
                    "material_full": {},
                    "material_comments": {},
                    "script_stack_recommendation": {},
                    "material_analysis": {},
                    "product_intent": {},
                    "rules": {},
                    "user_constraints": {"duration_seconds": 60},
                    "warnings": [],
                }
            return {
                "passed": True,
                "high_risk": False,
                "score": 90,
                "findings": [],
                "metrics": {},
                "warnings": [],
            }

        monkeypatch.setattr(media_pipeline, "_post_gateway", fake_gateway)
        app = create_app()
        headers = {"X-User-Id": "revision-user", "X-Tenant-Id": DEFAULT_TENANT_ID}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "media.script.change.propose",
                    "input_payload": {
                        "message": "Make the opening direct and preserve the persona.",
                        "base_script_id": "script-v1",
                        "base_artifact_id": "artifact-v1",
                        "current_script": {
                            "topic_name": "Veteran employment eligibility",
                            "persona_name": "Yan Jie",
                            "hook_3s": "Let us review the background first.",
                            "voiceover": "Existing policy-bound script.",
                            "duration_seconds": 60,
                        },
                        "topic_card": {"topic_name": "Veteran employment eligibility"},
                        "persona": {"display_name": "Yan Jie"},
                        "user_constraints": {"platform": "douyin", "duration_seconds": 60},
                    },
                },
            )
            proposal_task_id = created.json()["task"]["id"]
            started = await client.post(f"/task-manager/tasks/{proposal_task_id}/runs", headers=headers, json={})
            proposal_run_id = started.json()["run_id"]
            assert await _wait_for_status(client, proposal_run_id, headers, {"waiting_human", "failed"}) == "waiting_human"
            proposal_artifacts = (
                await client.get(f"/task-manager/tasks/{proposal_task_id}/artifacts", headers=headers)
            ).json()["artifacts"]
            proposal_artifact = proposal_artifacts[-1]

            applied = await client.post(
                f"/task-manager/runs/{proposal_run_id}/apply",
                headers=headers,
                json={
                    "proposal_artifact_id": proposal_artifact["id"],
                    "comment": "Approved for revision.",
                },
            )
            assert applied.status_code == 200, applied.text
            application = applied.json()
            revision_task_id = application["revision_task_id"]
            revision_run_id = application["revision_run_id"]
            assert revision_task_id != proposal_task_id
            assert revision_run_id != proposal_run_id
            assert await _wait_for_status(client, revision_run_id, headers, {"succeeded", "failed"}) == "succeeded"

            revision_task = (
                await client.get(f"/task-manager/tasks/{revision_task_id}", headers=headers)
            ).json()["task"]
            revision_input = revision_task["input_payload_json"]
            assert revision_task["task_type"] == "media.script.pipeline.generate"
            assert revision_task["parent_task_id"] == proposal_task_id
            assert revision_input["revision_mode"] is True
            assert revision_input["base_script_id"] == "script-v1"
            assert revision_input["proposal_artifact_id"] == proposal_artifact["id"]
            assert revision_input["previous_script"]["hook_3s"] == "Let us review the background first."
            assert revision_input["preserve_fields"] == ["persona_name"]

            revision_artifacts = (
                await client.get(f"/task-manager/tasks/{revision_task_id}/artifacts", headers=headers)
            ).json()["artifacts"]
            final_output = revision_artifacts[-1]["content_json"]
            assert final_output["final_script"]["persona_name"] == "Yan Jie"
            assert final_output["final_script"]["hook_3s"].startswith("Not every veteran")
            assert final_output["generation_meta"]["revision_mode"] is True
            assert final_output["generation_meta"]["base_script_id"] == "script-v1"
            assert final_output["generation_meta"]["proposal_artifact_id"] == proposal_artifact["id"]

            approved_proposal_artifacts = (
                await client.get(f"/task-manager/tasks/{proposal_task_id}/artifacts", headers=headers)
            ).json()["artifacts"]
            latest_proposal_artifact = [
                item
                for item in approved_proposal_artifacts
                if item["artifact_type"] == "media_script_change_proposal"
            ][-1]
            assert latest_proposal_artifact["checksum"] == proposal_artifact["checksum"]
            repeated = await client.post(
                f"/task-manager/runs/{proposal_run_id}/apply",
                headers=headers,
                json={"proposal_artifact_id": latest_proposal_artifact["id"]},
            )
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()["revision_task_id"] == revision_task_id
            assert repeated.json()["revision_run_id"] == revision_run_id
            assert ProposalAndRevisionAgent.calls == ["proposal", "writer", "storyboard"]

            unchanged_proposal = (
                await client.get(f"/task-manager/artifacts/{proposal_artifact['id']}", headers=headers)
            ).json()["artifact"]
            assert unchanged_proposal["checksum"] == proposal_artifact["checksum"]

    try:
        asyncio.run(run())
    finally:
        reset_event_broker_for_test()
        reset_engine_for_test()
