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


class MediaPipelineAgent:
    def __init__(self, llm, system_prompt, tools):
        if "Media Script Research" in system_prompt:
            self.stage = "research"
        elif "Media Script Writer" in system_prompt:
            self.stage = "writer"
        elif "Media Storyboard" in system_prompt:
            self.stage = "storyboard"
        elif "Media Script Review" in system_prompt:
            self.stage = "review"
        else:
            raise AssertionError(system_prompt[:500])

    async def run_async(self, state):
        payloads = {
            "research": {
                "topic_summary": "Explain a verified transition policy update.",
                "key_facts": [{"claim": "Policy applies after verification.", "source": "official", "confidence": 0.9}],
                "usable_materials": [],
                "audience_questions": ["Who is eligible?"],
                "controversies": [],
                "source_evidence": [{"title": "Official notice", "url": "https://example.test", "source_name": "official", "supports": "eligibility"}],
                "recommended_angle": "Explain the eligibility boundary.",
                "risks": [],
            },
            "writer": {
                "final_script": {
                    "topic_name": "Policy boundary",
                    "persona_name": "Veteran adviser",
                    "video_goal": "Explain eligibility",
                    "platform": "douyin",
                    "duration_seconds": 60,
                    "duration_reason": "About 180 Chinese characters.",
                    "target_char_range": [160, 220],
                    "hook_3s": "This policy is not automatic for everyone.",
                    "structure": ["hook", "facts", "action"],
                    "voiceover": "Verify your eligibility against the official notice before applying.",
                    "subtitle_points": ["Check eligibility", "Use official notice"],
                    "visual_direction": ["Presenter on camera"],
                    "material_bridge": "Uses the verified policy notice.",
                    "master_library_usage": {},
                },
                "readable_script": "Verify your eligibility against the official notice before applying.",
                "hermes_agent_result": {"status": "done", "editor_summary": "Boundary-first explanation", "why_this_angle": "Prevents overclaiming", "risks": [], "parse_notes": [], "master_library_usage": {}},
            },
            "storyboard": {
                "storyboard": [{"time": "0-6s", "scene": "Presenter", "shot": "medium", "action": "faces camera", "voiceover": "This policy is not automatic for everyone.", "subtitle_focus": "Check eligibility", "visual_prompt": "document and presenter"}],
                "storyboard_plan": {"shot_count": 1},
                "visual_direction": ["Document close-up"],
                "warnings": [],
            },
            "review": {
                "recommendation": "pass",
                "summary": "Evidence and boundaries are acceptable.",
                "compliance_findings": [],
                "quality_findings": [],
                "storyboard_findings": [],
                "revise_instruction": "",
            },
        }

        async def generate():
            text = json.dumps(payloads[self.stage], ensure_ascii=False)
            yield TextChunk(delta=text[: len(text) // 2])
            yield TextChunk(delta=text[len(text) // 2 :])

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


def test_media_script_pipeline_runs_all_stages(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("SQLITE_URL", f"sqlite+aiosqlite:///{tmp_path / 'media-pipeline.db'}")
        monkeypatch.setenv("TASK_EVENT_BROKER", "memory")
        monkeypatch.setenv("TASK_EXECUTOR_LOCK_BACKEND", "memory")
        reset_engine_for_test()
        reset_event_broker_for_test()
        await init_db()
        await _seed_llm_config()
        monkeypatch.setattr(runner_mod, "ReactAgent", MediaPipelineAgent)

        async def fake_gateway(path, payload, context):
            if path.endswith("/context"):
                return {
                    "topic_card": {"id": "topic-1", "topic_name": "Policy boundary"},
                    "source_brief": {"usable_level": "usable"},
                    "current_persona": {"display_name": "Veteran adviser"},
                    "persona_context": {},
                    "material_full": {"ok": True},
                    "material_comments": {"ok": True},
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
                "score": 86,
                "findings": [],
                "metrics": {},
                "warnings": [],
            }

        monkeypatch.setattr(media_pipeline, "_post_gateway", fake_gateway)
        app = create_app()
        headers = {"X-User-Id": "media-user", "X-Tenant-Id": DEFAULT_TENANT_ID}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/task-manager/tasks",
                headers=headers,
                json={
                    "task_type": "media.script.pipeline.generate",
                    "input_payload": {"topic": "Policy boundary", "topic_card_id": "topic-1", "duration_seconds": 60},
                },
            )
            assert created.status_code == 200, created.text
            task_id = created.json()["task"]["id"]
            started = await client.post(f"/task-manager/tasks/{task_id}/runs", headers=headers, json={})
            assert started.status_code == 200, started.text
            run_id = started.json()["run_id"]
            for _ in range(300):
                run_response = await client.get(f"/task-manager/runs/{run_id}", headers=headers)
                status = run_response.json()["run"]["status"]
                if status in {"succeeded", "failed", "waiting_human"}:
                    break
                await asyncio.sleep(0.02)
            assert status == "succeeded"
            stages = (await client.get(f"/task-manager/runs/{run_id}/stages", headers=headers)).json()["stages"]
            assert [item["stage_id"] for item in stages] == [
                "context", "research", "writer", "storyboard", "deterministic_checks", "review", "finalize"
            ]
            artifacts = (await client.get(f"/task-manager/tasks/{task_id}/artifacts", headers=headers)).json()["artifacts"]
            assert artifacts[-1]["artifact_type"] == "media_script_output"
            assert artifacts[-1]["content_json"]["generation_meta"]["workflow"] == "media-script-pipeline-v1"
            assert artifacts[-1]["content_json"]["final_script"]["storyboard"]

    try:
        asyncio.run(run())
    finally:
        reset_event_broker_for_test()
        reset_engine_for_test()
