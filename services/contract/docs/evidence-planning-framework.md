# Evidence planning framework

## Boundary

The shared layer owns orchestration mechanics only:

1. select an explicitly named evidence profile;
2. run adaptive frontier search under physical time, round and candidate budgets;
3. stop on profile-defined coverage, exhaustion or low marginal value;
4. bind selected evidence to executable review check codes;
5. fail closed for unknown or disabled profiles.

Each profile continues to own its domain semantics. In particular, retrieval,
applicability, relation trust, evidence schema and coverage rules are not shared
between legal evidence and business rules.

## Current profiles

- `legal`: preserves the existing `LegalEvidenceProvider` public API and uses
  the shared adaptive search loop internally. Legal release, projection,
  temporal/jurisdiction applicability and legal relation policy remain intact.
- `review-rule`: consumes an immutable Java rule-library snapshot. Selection
  and binding are deterministic and make no model calls. It filters by
  tenant, contract type, review stance, jurisdiction, active status and
  effective dates before selecting and binding rules.

The Java endpoint `GET /business/review-rules/snapshot` exports only the current
tenant plus platform-shared rules, in stable order, with a source version and
SHA-256 snapshot identity. Python validates that wire contract before planning.

## Compatibility and activation

This extraction does not switch the active review pipeline. The legal profile
continues through its original provider, while the review-rule profile is an
opt-in dark path covered by tests. There is no silent fallback from one profile
to another.

An optional supplementary review executor is now implemented (see below).
It does not overwrite the original seven-domain findings, counts or editing
workflow. Production activation still requires applicable rules to be reviewed
and published, plus full deployed upload-to-result acceptance. Pending rules
may only run in explicitly labelled PREVIEW mode.

## Existing strong / neutral / weak rules (2026-09-05)

The existing shared library is in `caranteetest-mysql-1/continew_admin`, not in
the two `proofspace_legal_app` databases. It contains 34,959 rules: 12,716 strong,
12,182 neutral and 10,061 weak. All source rows are currently `pending`.
`Javabackend/scripts/review-rules/export_rule_snapshot.py` exports those rows
read-only into a new directory, retaining their status and producing a
count-checked SHA-256 manifest. No source data is committed to Git.

The rule adapter now selects exactly one standard and uses explicit business
roles (such as buyer/lessor) separately from PARTY_A/PARTY_B. Contract types match
complete identifiers or complete path elements, never arbitrary substrings.
The local snapshot reader verifies every row, unique ID, count and checksum.

`RuleLibraryShadow` uses the existing review plan's actual check assignments and
scoped evidence to create issues, retrieve applicable existing rules and bind
their evidence IDs to those checks. Subject gates prevent incidental keywords
in the rule body from binding it to an unrelated check. Pending rules can be
examined only in an explicit preview; their status remains pending and the
resulting bundle has `usable=false`. Horizontal checks without a rule topic
policy remain unbound rather than inheriting unrelated rules.

Setting `RULE_LIBRARY_SHADOW_SNAPSHOT_DIR` when registering the framework
enables this observer. It runs after the review plan is built, writes its result
to the review-stage metadata, and leaves the authoritative final result and its
hash unchanged. With no setting, there is no new read or execution path.
Observer failures are reported as SHADOW/FAILED without failing the existing
review. No model, embedding or external service is used by this observer.

The full snapshot was tested against the existing plan-builder test fixture
with an explicit procurement/buyer selector. Neutral/strong/weak selected
15/13/11 evidence items from 42/39/55 applicable rules respectively. These are
retrieval-and-binding results using real rules and a test contract context,
not acceptance of model-generated findings or proof of full business coverage.
The three bundles remain DEGRADED because some review issues are unresolved.
Existing deployed services were not restarted or switched to this feature.

## Optional model review and traceable results

`RuleLibraryExecution` builds the existing risk plan, selects the immutable rule
snapshot, binds rules to check-scoped contract sources, and calls
`RuleLibraryReviewer`. The model receives rule content, review method and the
assigned contract quotations. Each decision identifies one existing rule
evidence ID. Risk decisions require a suggestion and exact contract quotations;
invented text, foreign source IDs and missing/duplicate rule decisions fail
validation. The final result sink rechecks quotations against frozen blocks.

The optional `rule_review` result travels through the existing final stage and
callback. It freezes the snapshot, bundle, input, rule versions and usage. Java
validates task/tenant/perspective and evidence links before persisting the
self-contained JSON. The additive `contract_rule_review.sql` migration adds a
nullable column; old results require no backfill. The frontend detail and
workbench panels display decisions, quotations, original rules and versions.
These are supplementary decisions, not existing Finding rows or OnlyOffice
revision drafts.

Configuration (all new paths are local; do not commit snapshots or caches):

- `RULE_LIBRARY_REVIEW_MODE`: `OFF` (default), `PREVIEW`, or `ACTIVE`.
- `RULE_LIBRARY_REVIEW_SNAPSHOT_DIR`: verified immutable snapshot directory.
- `RULE_LIBRARY_REVIEW_CACHE_DIR`: writable private result/cache directory.
- `RULE_LIBRARY_REVIEW_STANDARD`: `neutral` (default), `strong`, or `weak`.
- `RULE_LIBRARY_REVIEW_MAX_CALLS`: default 4, hard maximum 8.

The review standard is currently an execution configuration, not a newly wired
per-task frontend selector. Explicit business-role and contract-type matching
remain mandatory; unresolved selection is reported, not guessed. ACTIVE
requires published rules. PREVIEW never changes pending source rows.

Each batch bounds input length, output tokens and timeout. There is no repair
loop or provider retry. Cached frozen requests avoid repeated model calls.
An interrupted request retains a lock for operator inspection rather than
automatically recharging an uncertain call. Partial results retain unevaluated
evidence IDs. `rule_library_recorded_usage` is the stored execution usage, not
an additional charge on cache replay. No embeddings are generated by this path.

## Local validation, 2026-09-05

- Python regression selection: 212 passed, 25 skipped (before the two additional
  final-stage opt-in parameter cases); skipped cases are not claimed as passed.
- Final-stage OFF/PREVIEW compatibility and reviewer/cache rerun: 23 passed,
  including both additional opt-in cases. These overlap the regression selection.
- Java validator/client/import/query selection: 43 passed.
- Frontend component/client selection: 35 passed; TypeScript typecheck passed.
- A disposable isolated MySQL 8.0.42 applied the exact new migration, stored a
  real result, and recovered byte-equivalent JSON data through a new connection.
  No migration was applied to the three deployed applications.
- One actual model call used the local synthetic sample
  `C02_设备采购_R2_付款验收风险.docx`, clause 3.1, and the existing neutral buyer
  rule `预付款比例与担保机制`. It returned a grounded risk about 95% advance
  payment. Usage was 482 input + 274 output tokens; no embedding calls.
- The production React panel was rendered using that saved result. The local
  artifacts are under `proofspace-rule-data/execution-20260905`, outside Git.

This verifies one real rule/clause execution and separate persistence/UI
checks, not a full browser-upload contract review. No existing deployed service
was rebuilt or switched. Remaining acceptance includes a fully deployed
upload-to-result run, per-task standard selection, multiple contract types,
quality approval of pending rules, and any future conversion of supplementary
decisions into editable Finding/OnlyOffice revisions.
