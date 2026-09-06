"""Opt-in bridge from the formal contract pipeline to existing rule-library review."""
from datetime import date

from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.rule_evidence.shadow import RuleLibraryShadow
from contract.rule_evidence.reviewer import RuleLibraryReviewer, cached_rule_review


class RuleLibraryExecution:
    def __init__(self, directory, cache_directory, *, mode="PREVIEW", standard="neutral", max_calls=4,
                 runtime_factory=None):
        if mode not in {"PREVIEW", "ACTIVE"} or standard not in {"neutral", "strong", "weak"}:
            raise ValueError("Invalid rule-library execution policy")
        self.shadow = RuleLibraryShadow(directory)
        self.cache_directory = cache_directory
        self.mode, self.standard, self.max_calls = mode, standard, max_calls
        self.runtime_factory = runtime_factory

    async def run(self, value, tenant_id, model_id, *, contract_type_name=None, business_role=None,
                  standard=None, infer_business_role=True):
        # Task-local selection: never mutate the shared execution instance.
        selected_standard = self.standard if standard is None else standard
        if selected_standard not in {"neutral", "strong", "weak"}:
            raise ValueError("Invalid task rule-review standard")
        plan = RiskReviewPlanBuilder().build(value)
        detected_type, detected_role = self.shadow.selectors(value)
        contract_type_name = contract_type_name or detected_type
        business_role = (business_role or detected_role) if infer_business_role else business_role
        observation = self.shadow.evaluate(
            plan, tenant_id=tenant_id, contract_type_name=contract_type_name,
            business_role=business_role, review_standard=selected_standard,
            preview_pending=self.mode == "PREVIEW", review_as_of_date=date.today(),
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
