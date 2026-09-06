# 13009: source-backed lightweight AI party identification

Implemented 2026-09-06. Enabled only in the local legal-evidence-e2e Compose
override (`CONTRACT_PARTY_AI_ENABLED=true`). Other deployments retain the legacy
handler. No source branch was pushed by this change.

## User-requested change: optional source anchors (2026-09-06)

`party-source-ai-v2-optional-anchors` removes the source-binding acceptance gate
for AI party identities. Missing quotes, unknown fragment references, non-verbatim
or ambiguous quotes, and names not contained in quotes no longer reject a
schema-valid RESOLVED party. Exact anchors are attached only where available;
otherwise the anchor list remains empty. Metadata explicitly records
`identity_source_validation=NOT_PERFORMED`; this is not a claim of verified identity.

Names and business roles now rely on the AI decision and the user's confirmation.
Basic response schema, UNKNOWN/CONFLICT handling, user perspective/confirmation,
and all independent contract/legal evidence validation remain unchanged.
Existing 13009 party cache files are explicitly cleared during the scoped rollout;
successful cache reuse and prevention of automatic paid retries remain in place.
Historical failed jobs are not rewritten. The paragraphs below document the
initial v1 behavior except where superseded by this change.

## Data flow

Persisted OCR/text blocks -> selected original fragments -> one small AI call ->
verbatim quote binding -> party artifact metadata -> selected-side business role
-> existing rule-library execution. Existing seven-domain/legal review is unchanged.

- Selection cues choose **where to look**, never that service provider equals B.
- Include the opening, signature ending, declarations and neighbouring table/OCR
  rows, then business-role/factual cues. Send at most 6,000 original characters and
  24 fragments. These are input-selection safety bounds, not evidence-result caps.
- Code retains block ID, page if known, and block-local offsets. Model input uses
  short fragment references and literal text, not coordinates to calculate.
- AI returns A/B names, business roles and fragment+verbatim quotes. Contract text
  is untrusted data; it cannot alter the identification instructions.
- Code binds quotes to original fragments and computes positions. There is no
  model-generated page number, text offset, checksum or fixed A/B role dictionary.
- Both confirmed names and the user-selected perspective take precedence. A name
  correction cannot inherit the AI role of a differently named organisation.
- Roles and quotes are frozen in the existing party stage artifact metadata; the
  public party DTO and frontend form remain compatible.
- Rule matching keeps the literal A/B side separate from the business role. An
  unresolved AI role never silently falls back to the old role regexes.

## Cost and latency

Reuse the current configured LLM via `LlmRuntime`; no new vendor or credentials.
One call, thinking disabled, output cap 1,000 tokens, 12-second provider deadline,
no application/provider retries. The stage allows 18 seconds including source
loading/cache wait. Preflight pipeline deadline is 25 seconds. The frontend's
user-requested **5-second manual-entry fallback is unchanged**; 5-second AI success
is not guaranteed and was not measured. A late result can be reused in formal review.

**Follow-up, 2026-09-06:** the user requested removing that frontend 5-second
cutoff. The `party-wait-20260906` frontend now polls the existing resolution until
the backend reports success/failure or the user leaves/replaces the file. The
backend deadlines above remain unchanged. No additional model call is triggered
by polling. A 10.5-second delayed-success regression test confirms names reach the
page callback. The earlier paragraph describes the initial AI rollout only.

Local cache under `/app/runtime/party-ai-cache` is keyed by tenant, model, prompt
version, entire loaded text hash and selected input/vocabulary. Cached model quotes
are rebound to the current parse. Preflight and formal review share a call even
when parse IDs differ. Concurrent requests do not start duplicate calls.

Successful cache hits record zero new calls and preserve original provider usage.
Failures/timeouts are cached; crash intent markers prevent automatic recharge.
An operator must explicitly clear a particular failed entry/intent or change the
resolver version to allow another attempt. The frontend retry button alone does
not currently override this cost safeguard. Unknown provider usage stays unknown.

## Known limits / truthful status

- No real LLM identification or paid end-to-end review was submitted in this change.
  Synthetic model responses test integration, not semantic recognition accuracy.
- The excerpt selector cannot guarantee that every unusual contract contains all
  necessary information in its chosen text. OCR omission, missing declarations,
  conflicting/multiple parties still require human input. More than 2,000 persisted
  blocks is rejected as incomplete rather than silently using a truncated list.
- Multiple business roles can be recorded. The existing rule selector accepts one
  role; multiple roles are explicitly unresolved, not arbitrarily narrowed. Multi-role
  rule union, conflict handling and role aliases need a separate extension.
- Contract-type selection is still the existing title-based path. This change does
  not claim to solve every rule-selection failure.
- Old failed review tasks are not rewritten or resubmitted.

## Regression evidence

367 passed, 43 skipped, 1 warning, network-disabled Docker run (14.41 seconds).
New tests cover role reversal, table neighbours, long-block source offsets, absent
page numbers, invented/ambiguous quotes, preserved confirmations, concurrent cache
reuse, failed-call non-retry, separate A/B filtering, and unknown role transport.
The null-stripping transport bug is corrected by a default-null `business_role`
in both Framework and ai-contract; unknown role no longer creates the previous
missing-field HTTP 422. This is not a removal of citation-integrity validation.

Command (from the Framework repository, Windows PowerShell; substitute its absolute
path for `<repo>`):

```powershell
docker run --rm --network none --entrypoint python --workdir /workspace `
  -e PYTHONPATH=/workspace/tests:/workspace/services/contract/src:/workspace/services/contract/tests:/workspace:/workspace/backend/single-agent `
  --mount type=bind,source=<repo>,target=/workspace `
  local/proofspace-legal-framework:budget-observe-20260906 -m pytest `
  services/contract/tests/test_party_ai_resolver.py services/contract/tests/test_party_identity_resolution.py `
  services/contract/tests/test_party_extraction.py services/contract/tests/test_party_source_span.py `
  services/contract/tests/test_party_role_binding.py services/contract/tests/test_rule_library_shadow.py `
  services/contract/tests/test_rule_library_reviewer.py services/contract/tests/test_rule_evidence_planner.py `
  tests/test_contract_capability.py tests/test_contract_removed_limits.py tests/test_contract_budget_regression_7035.py `
  tests/test_contract_risk_prompt_budget.py tests/test_contract_risk_direct_review.py `
  tests/test_contract_risk_horizontal_review.py tests/test_contract_risk_base_bundle.py `
  tests/test_finding_consolidation.py services/contract/tests/test_risk_plan_builder.py `
  services/contract/tests/test_direct_runtime_packaging.py -q --tb=short --disable-warnings --maxfail=3
```

Two existing contract parses were read without mutation or model calls:

| Existing task | Full text | Selected original text | A/B names both included | Exact offsets |
| --- | ---: | ---: | --- | --- |
| 886748344641126413 | 3,856 chars | 610 chars | yes | yes |
| 886910237150281778 | 2,321 chars | 810 chars | yes | yes |

Character counts exclude prompt instructions, vocabulary and JSON framing; they
are not provider-billed token counts. Reports are outside the source repositories:
`C:/Users/Admin/Documents/Codex/workspaces/proofspace-rule-data/party-ai-13009-20260906/`.
The `source-coverage.json` report contains counts/booleans, not contract text.

## Deployment / rollback

Small overlay Dockerfiles in the sibling `output/` directory copy only changed
source files onto the prior images. Framework build context was about 137 KB.
New tags: `local/proofspace-legal-framework:party-ai-20260906` and
`local/proofspace-legal-contract:party-ai-20260906`.

`output/deploy_party_ai.py` checks no active 13009 reviews, isolated resources and
unchanged protected container IDs before updating only ai-contract/framework/worker.
The original images remain available. To roll back, restore the previous image tags
recorded in `before.json`, disable `CONTRACT_PARTY_AI_ENABLED`, then use a scoped
Compose `up --no-deps --pull never` for those three services after checking no active
review. Do not roll back/drop databases or delete caches, volumes or other projects.
