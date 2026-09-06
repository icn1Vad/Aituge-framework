"""Opt-in bridge from the formal contract pipeline to existing rule-library review."""
from datetime import date

from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.rule_evidence.shadow import RuleLibraryShadow
from contract.rule_evidence.reviewer import RuleLibraryReviewer, cached_rule_review


class RuleLibraryExecution:
    def __init__(self, directory, cache_directory, *, mode="PREVIEW", standard="neutral", max_calls=4,
                 runtime_factory=None, snapshot_client=None):
        if mode not in {"PREVIEW", "ACTIVE"} or standard not in {"neutral", "strong", "weak"}:
            raise ValueError("Invalid rule-library execution policy")
        self.shadow = RuleLibraryShadow(directory) if directory is not None else None
        self.snapshot_client = snapshot_client
        if self.shadow is None and snapshot_client is None:
            raise ValueError("Rule snapshot source is required")
        self.cache_directory = cache_directory
        self.mode, self.standard, self.max_calls = mode, standard, max_calls
        self.runtime_factory = runtime_factory

    async def task_shadow(self, business_task_id, tenant_id):
        if self.snapshot_client is not None:
            return RuleLibraryShadow(snapshot=await self.snapshot_client.load(business_task_id, tenant_id))
        return self.shadow

    async def run(self, value, tenant_id, model_id, *, contract_type_name=None, business_role=None,
                  standard=None, infer_business_role=True, task_shadow=None):
        # Task-local selection: never mutate the shared execution instance.
        selected_standard = self.standard if standard is None else standard
        if selected_standard not in {"neutral", "strong", "weak"}:
            raise ValueError("Invalid task rule-review standard")
        shadow = task_shadow if task_shadow is not None else self.shadow
        if shadow is None:
            raise ValueError("Task's frozen Java rule snapshot is required")
        plan = RiskReviewPlanBuilder().build(value)
        detected_type, detected_role = shadow.selectors(value)
        contract_type_name = contract_type_name or detected_type
        business_role = (business_role or detected_role) if infer_business_role else business_role
        observation = shadow.evaluate(
            plan, tenant_id=tenant_id, contract_type_name=contract_type_name,
            business_role=business_role, review_standard=selected_standard,
            preview_pending=self.mode == "PREVIEW",
            review_as_of_date=getattr(shadow.snapshot, "as_of_date", None) or date.today(),
        )
        key = {"review_id": value.review_id, "generation_id": value.generation_id,
               "tenant_id": tenant_id, "plan_hash": plan.plan_hash,
               "bundle_hash": observation["bundle"]["bundle_hash"],
               "business_role": business_role, "contract_type_name": contract_type_name,
               "model_id": model_id, "mode": self.mode, "reviewer_version": "rule-review-v1",
               "max_calls": self.max_calls, "rule_review_standard": selected_standard}
        async def execute():
            if self.runtime_factory is None:
                from service.conversation.llm_runner import LlmRuntime
                runtime = LlmRuntime(tenant_id, provider_max_retries=0)
            else:
                runtime = self.runtime_factory(tenant_id)
            return await RuleLibraryReviewer(runtime, max_calls=self.max_calls).review(
                observation, plan, tenant_id=tenant_id, model_id=model_id, mode=self.mode,
            )
        return await cached_rule_review(self.cache_directory, key, execute)
