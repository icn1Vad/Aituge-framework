# Semantic rule applicability and recorded-output recovery

## Requested behavior

Map contract language (for example 采购人) semantically to existing rule-library roles
(for example 买受方), including multiple roles and contract types. Do not infer buyer
or seller from the PARTY_A/PARTY_B label. Keep the user's confirmed perspective and
party names unchanged. Existing canonical risk cards remain the presentation path.

## Implemented

- `rule_evidence/semantic_selection.py`: one model request using actual contract
  declaration/transaction fragments, confirmed parties, declared business roles and
  the existing role/type catalogues. The model selects existing catalogue names;
  tenant, standard, lifecycle and relevance filtering still run afterwards.
- Production `RuleLibraryExecution` enables this selector. It accepts multiple
  selected roles/types, then uses the existing reviewer and Finding projection.
  Unresolved selection stays explicitly unresolved; no forced A/B fallback.
- Versioned tenant/model/document/party/snapshot cache and single-flight lock avoid
  duplicate semantic calls. Provider failure does not trigger an automatic retry.
  Selection usage and cached recorded usage are distinct, and stage metadata records
  selector usage separately from rule-review calls. This extra request is not free
  when it is actually executed; offline tests below use recorded/fake completions.
- Payment-fact extraction recognizes payment 30 working days / 12 or 24 months after
  completed acceptance. Exact payment headings, currency declarations and no-extra-
  payment statements no longer become UNKNOWN payment events. Genuine unknown or
  advance payment conditions remain protected.
- Horizontal prompts carry their response schema and scoped-absence requirements.
  Only invalid candidate rows get one bounded repair with the full original context;
  valid sibling rows are retained. A supplied NO_RISK resolution reason may fill an
  empty decision summary, but risk explanations are not invented. Repair may correct
  verdict and explanation together. Invalid second replies remain failures.
- Missing-attachment candidates now include the actual reference as primary-left
  evidence. Legacy candidates can use their original context reference. Unrelated
  shared sources do not satisfy the original-trigger requirement.
- Obsolete CF allowed-field wording was aligned with the final response contract.
  The old field list was already removed before the actual model request, so it is
  NOT established as the cause of the historical missing-ID replies.

## Verification and deployment

- Source and packaged offline regression: **1067 passed, 43 skipped**. Skip reasons
  are recorded in the logs and are not passes. No provider/network access.
- The one real-snapshot integration test among those skips was additionally run
  with the actual 34,959-rule snapshot mounted read-only: **1 passed**. Its fixture
  selects 15 / 13 / 11 rules for neutral / strong / weak respectively; these are
  fixture results, NOT the five real contracts' results.
- Unmodified historical SIG-001 and SIG-003 CF replies replay to COMPLETED against
  both changed source and the packaged image, with zero paid calls. Exact original
  reply hashes are retained in the private replay reports.
- Deployed only 13009 Framework, worker and ai-contract. Source hashes, healthy
  services, preserved mounts/networks, unchanged prior task records and protected
  13005/13007 resources verified. Local 13009 and public vettingtest HTTP checks pass.
- Framework image: `local/proofspace-legal-framework:semantic-role-recovery-20260907`,
  `sha256:cb97becf943c77c264102f837000298b35230fac2974ca7a83fd71070bf5f815`.
- API image: `local/proofspace-legal-contract:semantic-role-recovery-20260907`,
  `sha256:6fd93bd77a05ac51337a57914af5a87697ff8fc3c405ee28988db4bb97efdb76`.
- All 123 / 40 parent image layers reused; compressed image-size deltas recorded
  as 83,470 / 2,312 bytes. No model/base-image rebuild, database cleanup or frontend
  replacement. Rollback configuration and private DB backup retained.

Private evidence: `E:/ProofSpaceLegalKG/semantic-role-recovery-20260907`.

## Legal diagnostic boundary

The prior SIG-005 frozen legal snapshot has 17 issues, 14 unresolved and five retrieved
legal units. It has no degraded retrieval channels; the stop reason is candidates
exhausted. Some retrieved units do not bind to checks under current binding profiles.
Final legal citations are zero. Thus retrieval did run, but this is not complete
legal coverage. No unrelated law has been forced into the result to hide degradation.
No legal-binder changes were made in this patch.

## Live acceptance is still pending

The current browser inventory contained no retained logged-in tab. Opening 13009
redirected to the login page. The user has been asked to sign in again; no credentials
were accessed, no auth bypass was attempted, and **zero new live reviews have been
submitted in this patch round**. Do not report this round as fully accepted.

Continue from `output/semantic-role-five-file-test-plan.json`, preserving the old
round's plan and reports. Submit each file in `C:/Users/Admin/Desktop/testing` once:
SIG-001 A/strong, SIG-002 B/strong, SIG-003 A/weak, SIG-004 B/weak, SIG-005 A/strong.
No automatic resubmission. Record task IDs immediately. Verify semantic role choice,
actual rule execution, canonical rule-risk card linkage and legal coverage separately.

Browser opened on tab 4 at `http://127.0.0.1:13009/?auth=login#experience`.
New audit scripts support `PROOFSPACE_LIVE_PLAN` and `PROOFSPACE_LIVE_REPORT` to avoid
overwriting previous acceptance evidence. Actual AI semantic selection has not yet
been exercised by a live call in this round.

## Subsequent live acceptance: completed, not accepted

The user subsequently authorized credential-based login. Authentication used the
same frontend proxy and RSA password protocol over localhost; no browser auth
dialog, token forging, or credential persistence. The five files were each attempted
once. Four reached full review; SIG-003 failed at the fast party pipeline's existing
5-second parse timeout. Failed files were not resubmitted. No production code or
running deployment changed during this round.

- SIG-001: 13 canonical findings, including four linked rule findings. AI selected
  买受方/使用方/委托方 and 14 rules, but the rule review remains PARTIAL (8 pending,
  BATCH_FAILED:ValueError). Four revision drafts were rejected by the old local
  7,000-token guard after provider completion. Zero final legal citations.
- SIG-002: FAILED, CF-003 finding-level primary IDs empty although check-level
  decision IDs were selected; the single repair did not change this.
- SIG-003: failed before party AI; no full review submitted.
- SIG-004: FAILED, the correct attachment trigger was selected as supporting
  evidence, but the validator insists it be primary. A single repair also retained
  this arrangement; the source was supplied and valid.
- SIG-005: three canonical findings and Civil Code article 590 cited; revision
  generation completed. AI role selection returned ValueError, causing
  SELECTION_UNRESOLVED and no rule review. Failed raw selection details are not
  captured sufficiently to distinguish the exact catalogue/empty-selection cause.

Both actual final payloads pass the existing frontend card mapping replay, including
the four rule findings. This is not browser visual verification. Across the original
32 frontend API tests plus six live-matrix checks: 35 pass, three fail because their
real cases do not have final results. The real matrix must not be represented as
passing. Both completed review artifacts still have degraded legal coverage.

Known base/horizontal + semantic selector + rule-review token subtotal: 551,841;
not full billing. IR separately recorded 53 calls including 13 retries. Current
database lacks the invocation ledger; missing accounting is not zero usage.

Full Chinese handoff: workspace `output/semantic-role-five-file-acceptance-20260907.md`.
Final plan: `output/semantic-role-five-file-test-plan.json`, COMPLETE_NOT_ACCEPTED.
Private evidence: `E:/ProofSpaceLegalKG/semantic-role-recovery-20260907/live-five`.
