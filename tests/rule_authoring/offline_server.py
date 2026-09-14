"""Opt-in local API fixture. All business code is real; only the model is replaced.

Run with RULE_AUTHORING_OFFLINE_TEST=1 and PYTHONPATH=.;backend;backend/single-agent.
Never register this fixture in a deployed application.
"""
import json
import os
from types import SimpleNamespace
from fastapi import FastAPI
from backend.rule_authoring_api import create_rule_authoring_router
import aituge_model.config

if os.getenv("RULE_AUTHORING_OFFLINE_TEST") != "1":
    raise RuntimeError("Offline fixture must be explicitly enabled")

aituge_model.config.get_model_pack_for_ai_mode = lambda mode: SimpleNamespace(id="offline")


class OfflineModel:
    def __init__(self, **kwargs):
        self.model_runtime_provider = SimpleNamespace(active_pack=SimpleNamespace(llm=SimpleNamespace(id="offline-fixture")))

    async def complete_with_usage(self, **kwargs):
        import asyncio
        await asyncio.sleep(0.2)
        req = json.loads(kwargs["messages"][0]["content"])
        latest = req["messages"][-1]["content"]
        if "测试失败" in latest: raise ValueError("offline invalid model output")
        draft = req["draft"]
        path = next(p for p in req["contractTypes"] if p[-1] == "采购合同")
        draft.update(name="预付款保函要求", reviewDirection="预付款", contractTypePath=path,
            partyStance="买受方", reviewStandard="neutral", ruleType="dedicated",
            content="我方作为买受方采购设备时，预付款超过合同总价30%，应取得银行保函。",
            reviewMethod="核对付款比例、付款时间和保函约定；依据不足时说明无法判断。")
        return SimpleNamespace(content=json.dumps({"draft": draft, "reply": "已整理为采购规则，请确认。",
            "questions": [], "searchTerms": ["预付款", "保函", "预付"]}, ensure_ascii=False),
            prompt_tokens=100, completion_tokens=100)


app = FastAPI()
app.include_router(create_rule_authoring_router(OfflineModel))
