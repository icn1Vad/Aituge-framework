# Five-file acceptance: binding and recovery corrections

This patch follows the failed `semantic-role-recovery-20260907` acceptance.
It does not change the UI or publish a separate rule-review panel.

## Production changes

1. Direct evidence decoding may bind explicit check-level model selections to
   the sole Finding when that Finding's selection is empty. Never use retrieval
   candidates as a fallback; multiple Findings, foreign/legal IDs and malformed
   arrays still require correction. Existing downstream business checks remain.
2. Horizontal evidence adequacy uses primary plus supporting selections. Counter
   evidence cannot substitute for positive evidence. Missing-mechanism judgments
   still need their scoped absence record and actual trigger; conflicts need both
   sides. Materialization persists supporting text so the original quote/anchor
   survives into risk cards and revision planning. Removed obsolete prompt text
   claiming Python preselects primary evidence.
3. Upload party-resolution parsing gets 60 seconds, matching normal review
   parsing, instead of the residual 5-second stage timeout. Its total pipeline
   budget is 90 seconds, covering parsing, the existing 18-second AI stage and
   orchestration. No additional model retry was introduced.
4. Revision drafts no longer reject a completed provider response above 7000
   prompt tokens. Removed the local 4000 output cap on both local and remote
   runtime paths, 1200-character draft/numbering rejection, 24 insertion-candidate
   truncation and fixed source-excerpt truncation. Item/token packing targets
   still divide work into batches; they do not omit Findings or reject atomic
   inputs. Identifier, schema and source/version identity checks remain. Revision
   cache version advanced; historical caches/tasks were not deleted or reset.
5. Semantic routing asks the AI to select catalogue option IDs with per-selection
   reasons and source refs, then binds them to existing names. Multiple genuine
   roles/types remain supported. Prompts discourage inventing a separate mandate
   or transport relationship from incidental purchase services. Valid historical
   exact-name responses remain compatible. No A/B role inference fallback.
6. Rule decisions are validated individually. Good siblings survive a malformed,
   missing, duplicated, foreign or ungrounded row. Unresolved rules remain pending;
   their missing results are not represented as NO_RISK. No model retry added.
7. Semantic failures and all rule batches retain exact replies, delivered sources,
   hashes and field-level errors in the existing private diagnostic directory.
   Public output contains error codes/diagnostic IDs only, not raw provider text.
   Semantic/reviewer cache versions advanced to isolate prior behavior.

## Offline verification

- Source suite and packaged-image suite: 1135 passed, 43 skipped (unmounted
  historical fixtures plus the full external rule snapshot test).
- Exact failed SIG002 commercial and SIG004 horizontal replies replayed unchanged
  under Docker `--network none`. Both complete; horizontal materialization retains
  both ABSENCE and TEXT_QUOTE. Commercial replay uses its two already saved replies,
  not a fresh model response. These checks verify software flow, not the legal or
  commercial correctness of every model sentence.
- New negative controls ensure unknown/legal/ambiguous source selections still
  fail, counterevidence cannot prove the positive trigger, semantic mapping errors
  remain unresolved without retry, and valid rule siblings survive failures.
- Deployment uses source-only layers on the previous verified images. Protect
  13005 and 13007; retain existing review records and data volumes.

## Live acceptance

The separate `output/binding-recovery-five-file-test-plan.json` records one attempt
per file, sequentially: A strong / B strong / A weak / B weak / A strong. Passwords
and tokens are held only in process memory. Final results must be audited before
claiming acceptance; offline passing alone does not mean five-file success.

## Live-discovered follow-up and final state

SIG003 exposed a second graph-builder invariant: the real object `（  %）` is
nonempty, but alphanumeric normalization yields an empty value. RelationshipNode
now permits an empty normalized value while retaining the exact source and object;
punctuation-only subjects fall back to their IR category for topic indexing. No
value or percentage is invented. The previous deployed image reproduces the exact
failure; p2 builds the graph from all five frozen live inputs successfully offline.

Final source and packaged suites: 1141 passed, 43 skipped. The actual 34,959-rule
snapshot integration test also passed separately. P2 was deployed only AFTER all
five paid attempts ended; no old task result/status was overwritten. SIG003 has
not been rerun through the full paid pipeline, so its historical FAILED task is
not counted as successful.

Live result: SIG002 and SIG004 complete rule review and revisions; SIG005 returns
base findings/revisions but its semantic routing selects roles with zero eligible
rules under the chosen contract types. An offline diagnostic with 买受方 finds
73 eligible / 16 selected rules, but that counterfactual was NOT substituted into
the user's result. SIG001 still has ungrounded/empty model evidence selections.
All three returned results have zero final legal citations and degraded legal
coverage. This is NOT full acceptance. See the root workspace's
`output/binding-recovery-five-file-acceptance-20260907.md` for the exact outcomes.
