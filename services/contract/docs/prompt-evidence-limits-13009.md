# 13009 input-budget and evidence-cardinality removal — 2026-09-06

User-authorized evaluation change, prompt-budget policy **3.0**.

- Remove the post-provider 7,000-input-token rejection from base, horizontal,
  consolidation and final orchestration. Completed responses are still parsed
  and validated. Provider usage remains authoritative, cached tokens are not
  double counted, and missing usage is never fabricated.
- Keep 6,000 as an observation target. The legacy `hard_limit_tokens=7000` and
  `tokens_over_hard_limit` fields remain comparison metrics, not failure gates;
  `hard_limit_enforced=false` makes that explicit. No finite overage triggers a
  retry or discards a valid result under policy 3.0.
- Remove fixed source-count caps through candidate, decision, severity audit,
  canonical root and Finding materialization. LRE no longer slices primary
  source IDs to the first 20. Preserve uniqueness, source ownership, literal
  evidence, core/context partition, closed verdicts and coverage validation.
- Retain existing context partitioning, provider output caps, timeout/retry
  limits, model capacity, legal-catalog sizing and the complete-review guard.
  This change is **not** the full adaptive preflight/batching optimization.
- Invalid/truncated model JSON can still fail. A partial review is not marked
  complete, and old failed tasks are not rewritten as successes.

Offline regression: synthetic 24/64-primary-source contracts are materialized
through real candidate/decision/root/Finding code using fake 7,489/17,694-token
completions. No paid model calls are needed for these tests.

Deployment target: `local/proofspace-legal-framework:budget-observe-20260906`,
only `proofspace-legal-e2e` framework and framework-worker. Previous image
`local/proofspace-legal-framework:budget-tolerance-20260905` remains available.
No changes to 13005/13007, business data, credentials, or source branches.

## Verification

Offline suite: **268 passed, 42 skipped, 1 warning in 10.69s**. Skips require
external fixed-contract/recorded-attempt fixtures; they are not passing tests.
The consolidation test double was updated to implement the production
`complete_with_usage` protocol; its existing malformed-output/repair assertions
are unchanged.

Command (inside a disposable container with `--network none`, source mounted at
`/workspace`, and Python paths for tests, contract src and backend/single-agent):

```text
python -m pytest tests/test_contract_removed_limits.py tests/test_contract_budget_regression_7035.py tests/test_contract_risk_prompt_budget.py tests/test_contract_risk_direct_review.py tests/test_contract_risk_horizontal_review.py tests/test_contract_risk_base_bundle.py tests/test_finding_consolidation.py services/contract/tests/test_risk_plan_builder.py services/contract/tests/test_direct_runtime_packaging.py -q --tb=short --disable-warnings --maxfail=3
```

The built image separately imports the formal runtime and confirms a 17,694
input-token observation returns `SOFT_WARNING` with policy 3.0 and
`hard_limit_enforced=false`, without mounting the source tree.

No real contract resubmission or paid API call was made for this change.
