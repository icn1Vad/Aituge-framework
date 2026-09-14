"""Opt-in AI party stage; the public name/perspective DTO stays compatible."""
import time

import httpx

from contract.party.ai_resolver import VERSION, resolve_parties_ai
from task_manager.pipeline.errors import StageExecutionError
from task_manager.pipeline.stage_registry import StageServiceResult


def ai_party_resolution_handler(base_url, token, model_id, cache_directory, role_options=(), rule_library_execution=None):
    async def execute(context):
        from services.contract.capabilities.register import (
            ContractTaskInput, ParseContractStageResult, InternalContractBlocksEnvelope,
            PartyResolutionStageResult, PartyValue, _confirmed_party_value,
            PARTY_RESOLUTION_TASK_TYPE,
        )
        from service.conversation.llm_runner import LlmRuntime

        started = time.perf_counter()
        task = ContractTaskInput.model_validate(context.task.input_payload_json or {})
        selected_roles = role_options
        # Preflight runs before a Java review task exists. It only resolves names;
        # the formal review resolves roles against its own frozen rule vocabulary.
        if rule_library_execution is not None and getattr(context.task, "task_type", None) != PARTY_RESOLUTION_TASK_TYPE:
            try:
                shadow = await rule_library_execution.task_shadow(task.business_task_id, str(context.task.tenant_id))
                selected_roles = sorted({rule.party_stance for rule in shadow.snapshot.rules if rule.party_stance})
            except (httpx.HTTPError, ValueError, OSError) as exc:
                raise StageExecutionError("无法读取本次审查的规则快照，请重试。",
                    code="FRAMEWORK_RUN_FAILED", retryable=isinstance(exc, httpx.TransportError)
                    or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500)) from exc
        artifact = context.artifacts.get("parse_contract")
        if artifact is None:
            raise StageExecutionError("Missing persisted parse", code="PARTY_UNRESOLVED", retryable=False)
        parsed = ParseContractStageResult.model_validate(artifact.content_json)
        metadata = {"party_resolution_engine": VERSION, "model_call_count": 0,
                    "parties": {}, "party_resolution_id": task.party_resolution_id}
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=2.5) as client:
                response = await client.post(
                    "/v1/internal/contract-tools/blocks",
                    headers={"X-Internal-Service": "aituge-framework", "X-Internal-Token": token,
                             "X-Request-Id": f"contract-party-ai:{context.run.id}"},
                    json={"review_id": task.review_id, "document_id": task.document_id,
                          "block_ids": [], "limit": 2000})
            response.raise_for_status()
            blocks = InternalContractBlocksEnvelope.model_validate(response.json()).data
            if (blocks.review_id != task.review_id or blocks.document_id != task.document_id
                    or blocks.generation_id != parsed.generation_id
                    or len(blocks.blocks) != parsed.block_count):
                raise ValueError("Source parse mismatch or incomplete block list")
            metadata.update(await resolve_parties_ai(
                blocks.blocks, runtime_factory=lambda: LlmRuntime(str(context.task.tenant_id), provider_max_retries=0),
                tenant_id=str(context.task.tenant_id), model_id=model_id, review_id=task.review_id,
                run_id=str(context.run.id), cache_directory=cache_directory, role_options=selected_roles))
        except (httpx.HTTPError, ValueError, OSError) as exc:
            # Confirmed human identities remain usable even if business roles are unknown.
            metadata["diagnostics"] = ["PARTY_AI_UNAVAILABLE:" + type(exc).__name__]
            metadata["model_call_count"] = None  # provider usage may be unknown after failure
            if task.confirmed_party_a_name is None:
                raise StageExecutionError("Party AI could not return a usable identity result; manual input is required.",
                                          code="PARTY_UNRESOLVED", retryable=False) from exc
        values = {}
        for side, label in (("party_a", "PARTY_A"), ("party_b", "PARTY_B")):
            confirmed = getattr(task, "confirmed_" + side + "_name")
            item = metadata["parties"].get(side, {})
            if confirmed is not None:
                values[side] = _confirmed_party_value(confirmed, label)
                # A user correction must not inherit the role of a differently named organisation.
                if item.get("name") and "".join(item["name"].split()).casefold() != "".join(confirmed.split()).casefold():
                    item["business_roles"] = []
                    item["status"] = "USER_NAME_DIFFERS"
            elif item.get("status") == "RESOLVED":
                name = item.get("name")
                values[side] = PartyValue(name=name or ("甲方" if label == "PARTY_A" else "乙方"),
                                          name_resolved=bool(name), name_status="EXTRACTED" if name else "NOT_STATED")
            else:
                raise StageExecutionError("Party mapping is unknown or conflicting; manual input is required.",
                                          code="PARTY_UNRESOLVED", retryable=False)
        a, b = values["party_a"], values["party_b"]
        if a.name.casefold() == b.name.casefold():
            raise StageExecutionError("Party identities are identical", code="PARTY_UNRESOLVED", retryable=False)
        result = PartyResolutionStageResult(
            result_type="PARTY_RESOLUTION_STAGE_V1", contract_type="AUTO",
            resolution_status="RESOLVED" if a.name_resolved and b.name_resolved else "PARTIAL",
            party_a=a, party_b=b, perspective=task.perspective,
            our_party=a.name if task.perspective == "PARTY_A" else b.name,
            counterparty=b.name if task.perspective == "PARTY_A" else a.name)
        metadata["duration_ms"] = round((time.perf_counter() - started) * 1000)
        return StageServiceResult(output=result.model_dump(mode="json"), metadata=metadata,
                                  summary="AI party mapping with optional source anchors; confirmed names and perspective preserved.")
    return execute


def rule_role_arguments(metadata, perspective):
    """An unresolved AI mapping must not silently fall back to role regexes."""
    if metadata.get("party_resolution_engine") != VERSION:
        return {}
    side = "party_a" if str(getattr(perspective, "value", perspective)) == "PARTY_A" else "party_b"
    item = metadata.get("parties", {}).get(side, {})
    roles = item.get("business_roles", []) if item.get("status") == "RESOLVED" else []
    roles = sorted({role.strip() for role in roles if isinstance(role, str) and role.strip()})
    return {"business_role": roles[0] if len(roles) == 1 else None,
            "business_roles": roles, "infer_business_role": False}
