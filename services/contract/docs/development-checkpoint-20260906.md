# Development checkpoint: legal evidence and existing rule-library review

This is a work-in-progress handoff on `feature/legal-knowledge-graph-e2e`,
not a production release or an assertion that the end-to-end review passes.
All five repositories use this feature branch, based on the previously frozen
`carpertest` commits. Do not merge into `carpertest` or `main` without approval.

## Included work

- Generic evidence-planning abstractions and adapters for legal evidence and
  the existing strong/neutral/weak rule library.
- Java rule snapshots, applicability checks, result validation/persistence and
  task-specific rule review standard; frontend selection and supplementary
  rule results.
- Party-source AI, source-bound review records, check-local output repair and
  private diagnostics; removal of the nonblank `decision_note` immutability
  restriction. Other validation remains in place.
- Legal classification, temporal metadata and projection/reuse changes.
- Development-only Compose overlays for rules and OnlyOffice on 13009.

The nontechnical rule-authoring workflow (new rule, validation, publishing and
integration into the active rule network) is subsequent work, not completed
by this checkpoint. Build on the existing rule API and evidence abstractions;
do not add a parallel review engine.

## Known unresolved problems

The saved SIG-003 pre-interruption run exposed the following failures; its raw
contract and model diagnostics remain local and are not included in Git:

1. Evidence output can contain unsupported types or mismatched fact/source
   references. Repair context currently filters using the rejected answer's
   references, which can remove the correct source candidates.
2. Evidence immutability constraints still conflict with repairing evidence
   metadata. Only the separate restriction on editing an existing explanation
   has been removed. This does not fix all output recovery failures.
3. A check split across contexts can claim global absence from a partial
   context. Source scope is recorded and unsafe absence is rejected, but
   cross-shard synthesis remains unresolved rather than a completed feature.
4. CF-005 partial-absence validation can fail before the existing payer/payee
   perspective correction runs. The full validation order needs replay tests.
5. After an interrupted Docker run, a Redis executor lock can outlive the
   worker while the database lease is repeatedly claimed. Recovery/backoff
   and the frontend's stale 99%/disconnection display need work.

Do not remove source provenance, tenant checks or failure reporting to make
these scenarios appear successful. Preserve original and repaired diagnostics.

## Verification at handoff

- Focused commercial recovery/direct-review tests: 84 passed.
- Broader offline contract/legal/rule regression selection: 417 passed,
  43 skipped. Run in a network-disabled container; completions in this suite
  are test doubles. This is not live model or full database acceptance.
- Frontend upload, rule panel, rule standard selector and business review API:
  4 test files, 51 tests passed; TypeScript no-emit check passed.
- Java repository's legal graph release generator: 7 offline unittest cases
  passed (this is a Python utility, not the Java Maven suite).
- No new live contract review, embedding generation, image build or service
  deployment was performed to publish this checkpoint. Java's full Maven
  suite was not rerun for this handoff.

## Parallel development and environment

Start rule-authoring work in a separate feature branch/worktree from this
checkpoint. Coordinate edits to the shared review contracts and do not
concurrently modify the same local checkout or deploy the shared 13009 stack.
The original task retains the review validation/recovery audit.

Source publishing does not update running containers. In particular, the
latest explanation-repair change is source-only until an explicit deployment.
Deployment overlays contain machine-specific paths and local image tags;
database credentials, rule snapshots, legal release data and runtime files
are intentionally outside Git. Do not commit or copy private keys, API keys,
database dumps, full contract diagnostics, model files or multi-GB releases.

The helper `services/contract/scripts/run_rule_model_smoke_local.py` is a
manual live-model test and may incur API charges; it is not part of the
offline suite and must not be run merely to inspect this checkpoint.
