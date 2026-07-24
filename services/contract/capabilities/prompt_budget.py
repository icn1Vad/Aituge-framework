from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROMPT_BUDGET_POLICY_VERSION = "2.0"
PROVIDER_PROMPT_TARGET_TOKENS = 6000
PROVIDER_PROMPT_HARD_LIMIT_TOKENS = 7000

PromptBudgetStatus = Literal[
    "WITHIN_TARGET",
    "SOFT_WARNING",
    "HARD_LIMIT_EXCEEDED",
    "PROVIDER_USAGE_UNAVAILABLE",
]


class PromptBudgetResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    policy_version: Literal["2.0"] = PROMPT_BUDGET_POLICY_VERSION
    target_tokens: Literal[6000] = PROVIDER_PROMPT_TARGET_TOKENS
    hard_limit_tokens: Literal[7000] = PROVIDER_PROMPT_HARD_LIMIT_TOKENS
    estimated_business_context_tokens: int | None = Field(default=None, ge=0)
    client_estimated_prompt_tokens: int | None = Field(default=None, ge=0)
    client_tokenizer_name: str | None = Field(default=None, min_length=1, max_length=160)
    provider_prompt_tokens: int | None = Field(default=None, ge=0)
    provider_cached_tokens: int | None = Field(default=None, ge=0)
    budget_status: PromptBudgetStatus
    tokens_over_target: int | None = Field(default=None, ge=0)
    tokens_over_hard_limit: int | None = Field(default=None, ge=0)
    over_target_ratio: float | None = Field(default=None, ge=0)
    unit_id: str = Field(min_length=1, max_length=160)
    batch_id: str = Field(pattern=r"^risk-batch-[0-9a-f]{32}$")

    @model_validator(mode="after")
    def validate_provider_usage(self) -> "PromptBudgetResult":
        if (
            self.provider_prompt_tokens is not None
            and self.provider_cached_tokens is not None
            and self.provider_cached_tokens > self.provider_prompt_tokens
        ):
            raise ValueError("cached_tokens must be a subset of prompt_tokens")
        if self.provider_prompt_tokens is None:
            if self.budget_status != "PROVIDER_USAGE_UNAVAILABLE":
                raise ValueError("Missing provider usage must use PROVIDER_USAGE_UNAVAILABLE")
            if any(
                value is not None
                for value in (
                    self.tokens_over_target,
                    self.tokens_over_hard_limit,
                    self.over_target_ratio,
                )
            ):
                raise ValueError("Missing provider usage cannot report exact overage")
        return self


def evaluate_prompt_budget(
    *,
    unit_id: str,
    batch_id: str,
    estimated_business_context_tokens: int | None,
    provider_prompt_tokens: int | None,
    provider_cached_tokens: int | None,
    client_estimated_prompt_tokens: int | None = None,
    client_tokenizer_name: str | None = None,
) -> PromptBudgetResult:
    if provider_prompt_tokens is None:
        return PromptBudgetResult(
            estimated_business_context_tokens=estimated_business_context_tokens,
            client_estimated_prompt_tokens=client_estimated_prompt_tokens,
            client_tokenizer_name=client_tokenizer_name,
            provider_prompt_tokens=None,
            provider_cached_tokens=provider_cached_tokens,
            budget_status="PROVIDER_USAGE_UNAVAILABLE",
            unit_id=unit_id,
            batch_id=batch_id,
        )

    tokens_over_target = max(
        0,
        provider_prompt_tokens - PROVIDER_PROMPT_TARGET_TOKENS,
    )
    tokens_over_hard_limit = max(
        0,
        provider_prompt_tokens - PROVIDER_PROMPT_HARD_LIMIT_TOKENS,
    )
    if tokens_over_hard_limit:
        status: PromptBudgetStatus = "HARD_LIMIT_EXCEEDED"
    elif tokens_over_target:
        status = "SOFT_WARNING"
    else:
        status = "WITHIN_TARGET"
    return PromptBudgetResult(
        estimated_business_context_tokens=estimated_business_context_tokens,
        client_estimated_prompt_tokens=client_estimated_prompt_tokens,
        client_tokenizer_name=client_tokenizer_name,
        provider_prompt_tokens=provider_prompt_tokens,
        provider_cached_tokens=provider_cached_tokens,
        budget_status=status,
        tokens_over_target=tokens_over_target,
        tokens_over_hard_limit=tokens_over_hard_limit,
        over_target_ratio=round(
            tokens_over_target / PROVIDER_PROMPT_TARGET_TOKENS,
            6,
        ),
        unit_id=unit_id,
        batch_id=batch_id,
    )


def summarize_prompt_budgets(
    values: list[PromptBudgetResult],
) -> PromptBudgetResult:
    if not values:
        raise ValueError("At least one PromptBudgetResult is required")
    identity = {(item.unit_id, item.batch_id) for item in values}
    if len(identity) != 1:
        raise ValueError("Prompt budget results must belong to one Batch")
    status_order = {
        "WITHIN_TARGET": 0,
        "PROVIDER_USAGE_UNAVAILABLE": 1,
        "SOFT_WARNING": 2,
        "HARD_LIMIT_EXCEEDED": 3,
    }
    worst = max(
        values,
        key=lambda item: (
            status_order[item.budget_status],
            item.provider_prompt_tokens or -1,
        ),
    )
    return worst.model_copy()
