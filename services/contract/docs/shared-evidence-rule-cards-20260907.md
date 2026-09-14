# Shared contract sources and unified rule risk cards

## Scope and behavior

- Evidence protocol: `model-selected-evidence-v5-shared-text`.
- Real contract text already delivered in the frozen batch can be cited across checks/candidates. Retrieval tags are not citation permissions. Unknown IDs, legal IDs in contract-source fields, invalid original offsets/hashes and out-of-scope absence records remain invalid.
- PO semantic-factor validation is keyword/regular-expression logic, not a second AI. A rejected factor now carries the candidate ID and accepted/rejected factors into the existing single bounded redecision. The model may correct its verdict/reason; evidence safeguards are not disabled.
- CF-005 payer determination uses the triggering advance-payment events. Unrelated freight reimbursements or payment headings must not make an explicitly stated advance-payment payer ambiguous. An unknown payer in the advance event itself stays unknown.
- Rule reviewer v4 sends each batch's source catalogue once, shared by its rules; no extra model call for card conversion.
- `rule_evidence/finding_projection.py` converts only grounded `RISK` decisions into the existing canonical `Finding`/`Evidence` payload before finalization and persistence. Existing frontend mapping automatically consumes these Findings.
- No separate rule sidebar is added. Preview rule status and frozen rule/version links stay in `rule_review`. `decision.finding_id` points to the canonical Python Finding ID, also persisted as Java `pythonFindingId`.
- Dedup requires the same category, overlapping exact quote location, identical reasoning and identical title or recommendation. Shared text alone is not duplication. Non-risk/uncertain decisions are not silently converted into risks.
- Rule output currently has no severity field. Newly projected Findings use MEDIUM internally; the existing public card projection omits severity. No legal provision is invented for a rule-only finding.
- Existing historical tasks are not automatically rerun or rewritten.

## Verification so far

- Source and packaged image offline suite: 1025 passed, 43 skipped (unavailable older external fixtures), no paid calls.
- Frontend API/card tests: 38 passed. New test demonstrates projected and original Findings share the same risk collection and original-location/action state mapping.
- Unmodified historical CF replies (`risk-batch-2fabb8b926cb9f176e7527ae1b31f2d0`) replay to COMPLETED. The payment/acceptance cross-check source is accepted. Wrong-perspective advance risk remains excluded; this is not evidence that all model prose is semantically correct.
- Private reports, original raw hashes, rollback backup and deployment/source-hash checks: `E:/ProofSpaceLegalKG/shared-evidence-rule-cards-20260907`.
- Incremental images replace only 13009 Framework, worker and ai-contract. 13005/13007, office, Java, frontend and existing review records are protected by deployment gates.

## User-mandated live acceptance protocol

After each bug-fix round, test the five files in `C:/Users/Admin/Desktop/testing`, ONCE PER FILE, not all four perspectives per file. Do not silently spend on repeats. Agreed matrix:

| File prefix | Perspective | Rule standard |
| --- | --- | --- |
| SIG-001 | PARTY_A | strong |
| SIG-002 | PARTY_B | strong |
| SIG-003 | PARTY_A | weak |
| SIG-004 | PARTY_B | weak |
| SIG-005 | PARTY_A | strong |

## Live acceptance outcome, 2026-09-07

The user signed in and the five files were each submitted ONCE through the real 13009 upload UI. No extra review tasks or paid resubmissions were made. All five party names resolved in the UI, and persisted perspectives/standards matched the matrix. No production code or deployment was changed during the five-file run.

**Acceptance FAILED: four tasks failed, one generated six canonical risk cards but did not exercise rule-risk projection.** Offline unit test success above must not be presented as live acceptance success.

| File | Task ID | Outcome / final blocker |
| --- | --- | --- |
| SIG-001 | 887413520335900915 | FAILED: CF-005 applicability gate did not recognize payment after completing acceptance; unrelated non-payment text also entered the payment-event set. |
| SIG-002 | 887414791138709772 | FAILED: MAC-005 horizontal reply missing required `decision_summary`; no horizontal repair attempt. |
| SIG-003 | 887415992014082340 | FAILED: true payment event is AFTER, but payment headings, currency and no-extra-payment clauses also become UNKNOWN events, preventing trigger rejection. |
| SIG-004 | 887418103758065981 | FAILED: MAC-005 missing-attachment risk lacks selected scoped absence evidence; no horizontal repair attempt. |
| SIG-005 | 887419408236941653 | REVIEW_REQUIRED, 100%, six canonical risk cards visibly rendered; `NO_APPLICABLE_RULES`, zero rule decisions/calls; legal status DEGRADED, zero final law citations. |

The CF initial replies in SIG-001/003 omitted decision evidence selections. Their recorded repair replies supplied selections successfully; the FINAL failure is the business applicability gate, not the earlier missing-field error repeated in aggregated diagnostics. Packaged-image offline computation reproduces the payment-event values with `--network none`.

SIG-005's rule result records `business_roles=["采购人"]`, `review_standard=strong`. Snapshot matching compares literal business-role names; the release has no 采购人 role and procurement rules use 买受方/出卖方 (73 strong 买受方 procurement rules in the release). This is a confirmed selector incompatibility, not proof that all 73 rules apply to this contract. Contract-type and relevance checks still need independent verification. No role mapping or new paid run was silently added during acceptance.

The first four tasks fail before `rule_library_execution.run`, so they cannot verify rule-card merging. SIG-005 returns no applicable rule evidence and thus no projected rule risks. There is no live positive case for the new merge yet. Legal input is present, including full body text, in the recorded SIG-001/004 financial requests, but that is not evidence of complete legal retrieval or correct relevance across all tasks. SIG-005's saved result metadata reports DEGRADED / UNRESOLVED_ISSUES.

Recorded base + horizontal phase usage is 38 model calls, 8 repair calls included, and 718,624 input + output tokens. IR extraction separately records 71 calls including 17 retries. These are NOT full billing totals: OCR/upload party recognition/legal planning and any unrecorded usage are outside this subtotal. Five review submissions must never be described as five model API calls.

Private acceptance evidence is under `E:/ProofSpaceLegalKG/shared-evidence-rule-cards-20260907/live-five`: task rows/stage errors, available raw diagnostics, partial usage ledger and offline payment gate reproduction. The local matrix is `output/shared-evidence-five-file-test-plan.json`. The browser was left on SIG-005's six-card result; no apply/revert/QA/report-generation action was invoked.

Next: fix and offline replay the payment-event classification and horizontal output recovery paths, reconcile canonical business-role selection, and diagnose legal degradation. Do not delete real-source/absence grounding safeguards to make these samples green. Ask before expanding the paid test scope; do not resubmit these five tasks automatically.
