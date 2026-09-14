"""Opt-in bridge from the formal contract pipeline to existing rule-library review."""
from datetime import date

from contract.risk.plan_builder import RiskReviewPlanBuilder
from contract.rule_evidence.shadow import RuleLibraryShadow
from contract.rule_evidence.reviewer import RuleLibraryReviewer, cached_rule_review, REVIEWER_VERSION


class RuleLibraryExecution:
    def __init__(self, directory, cache_directory, *, mode="PREVIEW", standard="neutral", max_calls=4,
                 runtime_factory=None, semantic_selection=False):
        if mode not in {"PREVIEW", "ACTIVE"} or standard not in {"neutral", "strong", "weak"}:
            raise ValueError("Invalid rule-library execution policy")
        self.shadow = RuleLibraryShadow(directory)
        self.cache_directory = cache_directory
        self.mode, self.standard, self.max_calls = mode, standard, max_calls
        self.runtime_factory = runtime_factory
        self.semantic_selection = semantic_selection

    async def run(self, value, tenant_id, model_id, *, contract_type_name=None, business_role=None,
                  standard=None, infer_business_role=True, business_roles=None):
        # Task-local selection: never mutate the shared execution instance.
        selected_standard = self.standard if standard is None else standard
        if selected_standard not in {"neutral", "strong", "weak"}:
            raise ValueError("Invalid task rule-review standard")
        plan = RiskReviewPlanBuilder().build(value)
        detected_type, detected_role = self.shadow.selectors(value)
        contract_type_name = contract_type_name or detected_type
        roles = sorted({role.strip() for role in
            [*(business_roles or []), *([business_role] if business_role else [])] if role.strip()})
        if not roles and infer_business_role and detected_role:
            roles = [detected_role]
        selection = None
        type_aliases = []
        def runtime():
            if self.runtime_factory is not None:
                return self.runtime_factory(tenant_id)
            from service.conversation.llm_runner import LlmRuntime
            return LlmRuntime(tenant_id, provider_max_retries=0)
        if self.semantic_selection:
            from contract.rule_evidence.semantic_selection import select_rule_applicability
            selection = await select_rule_applicability(value, self.shadow.snapshot, tenant_id=tenant_id,
                model_id=model_id, runtime_factory=runtime, cache_directory=self.cache_directory,
                declared_roles=roles)
            roles = selection['business_roles']
            type_aliases = selection['contract_types']
            contract_type_name = type_aliases[0] if len(type_aliases)==1 else None
        business_role = roles[0] if len(roles) == 1 else None  # legacy scalar only
        observation = self.shadow.evaluate(
            plan, tenant_id=tenant_id, contract_type_name=contract_type_name,
            business_role=business_role, business_roles=roles, review_standard=selected_standard,
            preview_pending=self.mode == "PREVIEW", review_as_of_date=date.today(),
            contract_type_aliases=type_aliases,
        )
        if selection and selection['status'] != 'RESOLVED':
            observation['selection_warnings'] = sorted(set(observation['selection_warnings'] +
                ['SEMANTIC_SELECTION_UNRESOLVED'] + selection.get('diagnostics',[])))
        key = {"review_id": value.review_id, "generation_id": value.generation_id,
               "tenant_id": tenant_id, "plan_hash": plan.plan_hash,
               "bundle_hash": observation["bundle"]["bundle_hash"],
               "business_role": business_role, "business_roles": roles, "contract_type_name": contract_type_name,
               "model_id": model_id, "mode": self.mode, "reviewer_version": REVIEWER_VERSION,
               "max_calls": self.max_calls, "rule_review_standard": selected_standard,
               "semantic_selection_hash": selection['input_hash'] if selection else None}
        async def execute():
            return await RuleLibraryReviewer(runtime(), max_calls=self.max_calls).review(
                observation, plan, tenant_id=tenant_id, model_id=model_id, mode=self.mode,
            )
        result = await cached_rule_review(self.cache_directory, key, execute)
        return result.model_copy(update={'semantic_selection':selection}) if selection else result
