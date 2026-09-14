# Seven-domain model-selected evidence (13009)

## Scope and cause

All seven review domains now advertise `model-selected-evidence-v2`. Financial
and formation/authority review no longer ask the model to produce an evidence
type and independent I/A references. The three previously migrated candidate
domains retain explicit primary selection. Both horizontal domains now require
explicit primary selection, too; materialization no longer attaches all trigger
and background sources as primary evidence.

The frozen failing run is `6e06fba2100842dfa23e3b9d6c482bd0`. Its financial
response mixed law IDs into contract Evidence, paired a heading fact with the
next line's anchor, and called post-acceptance payment advance payment. The
actual old CF-005 candidate already had `payment_before_performance=false` and
`payer_role_status=COUNTERPARTY`: it would be inaccurate to blame this particular
failure solely on the old invoice keyword. There was also loss of the acceptance
condition in split IR summaries, biased candidate naming, and repair instructions
that could preserve a wrong conclusion while asking for evidence fixes.

## Model/server boundary

- Direct domains: the model selects `primary_evidence_source_ids` from a
  check-scoped, named contract catalogue. A source fixes generation, IR item,
  anchor, original quote and hash. The server expands selected IDs into the
  existing internal Evidence shape before business validation/materialization.
- Candidate domains: primary, supporting and counter roles are selections from
  candidate-bounded material, never positional arrays or inferred primary lists.
- Horizontal conflict findings require selected sources on both conflict sides;
  missing-mechanism findings require their scoped absence source. Unselected
  background is not promoted to primary evidence.
- Existing deterministic FVA absence sources remain check-scoped. Financial
  model-inferred absence uses a separate `absence_assessments` field, never
  disguised as an original quote. Known partial scopes go through the existing
  unresolved-observation quarantine; absent scope metadata cannot prove absence.
- The legacy schemas/parsers remain usable for stored-artifact/internal regression
  analysis. Live direct-review calls always pass the source-selection decoder;
  they do not silently accept the legacy model-output protocol.

Law text remains complete and separate. The model sees citation labels and full
law text, not the server's legal evidence IDs. It explains a relevant provision
in `issue`/`decision_summary`; existing binding persists the exact supplied law
and frozen version. Contract source arrays cannot contain legal IDs.

## Financial and repair changes

Payment timing uses the complete anchored clause, not only subject/predicate/
object fragments. Invoice receipt alone is not pre-performance evidence, and a
large/one-time payment is not synonymous with advance payment. Semantic IR still
helps describe installment or milestone mechanisms; it does not override the
original temporal condition. CF-005's public candidate is a neutral payment-
timing/security question instead of an asserted unsecured-prepayment risk.

Direct-domain repair is one bounded reassessment of failed checks, with the
original contract catalogue, legal text and new wire contract retained. It may
correct the target verdict, title, explanation and source selection together.
Accepted sibling checks remain immutable; all repaired targets pass the existing
domain, party, scope and provenance validators again. No extra unconditional
review/model pass was introduced. Horizontal failure handling remains unchanged
(no newly added automatic model retry).

## Verification and deployment

- Offline Docker suite: **957 passed, 43 skipped**, 4 warnings. The 43 optional
  fixture/snapshot tests are not counted as passes.
- Added tests cover direct wire-to-Finding binding, missing/unknown/legal/duplicate
  source rejection, full legal text through repair and citation persistence,
  scope handling, post-acceptance vs advance payment, correction of a wrong
  target without rewriting siblings, and selected horizontal evidence reaching
  roots/Findings without automatic background inclusion.
- Frozen input replay rebuilds all five base-domain prompts and available
  horizontal candidates. This real contract produced no model batch for the
  conflict domain; that domain is covered by synthetic propagation tests instead.
  All six stored failed financial responses are rejected by the live new decoder.
  The old heading/line mismatch cannot be produced by selecting the actual line
  source, which binds to I023/A007 in the frozen financial projection.
- No paid model request, embedding, new review submission, or historical task
  status change was made by these tests. This is not a fresh live-model review
  and does not establish that model judgments can never be wrong.
- Incremental image copies five source files over the previous installed image;
  only 13009's `framework` and `framework-worker` are replaced. Frontend/Java
  wire display shape, database, volumes, 13005 and 13007 stay unchanged.

Private diagnostics, test commands, hashes, frozen replay and deployment checks:
`E:\ProofSpaceLegalKG\seven-domain-evidence-20260907` (not committed).
Deployment runner: workspace `output/deploy_seven_domain_evidence.py`.
No source or secret was pushed in this change.
