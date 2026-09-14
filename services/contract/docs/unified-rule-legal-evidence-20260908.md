# Rule and legal evidence: explicit model selections

## Scope

The model makes semantic judgments and selects identifiers. The server owns original text, coordinates, hashes, source versions and persisted Finding/Evidence associations. Frontend cards and law dialogs are unchanged.

- Rule review v6 replaces `quotes[{source_id, quote}]` with `primary_evidence_source_ids[]`. Binding copies the complete frozen source, including offsets and hash. Old quote objects are rejected per decision; valid sibling decisions remain available. The reviewer version invalidates old response-cache keys without deleting historical results.
- Legal prompt v4 exposes the frozen `evidence_id` in the law catalogue. Findings and candidate decisions select `legal_evidence_ids[]`, separately from contract evidence IDs. No legal selection means no automatic law binding.
- Removed the prose/regular-expression law-name binding function entirely. Abbreviations in explanations no longer determine source association. Selected law IDs are preserved through direct Finding creation, candidate canonicalization, horizontal review, and existing result merges.
- Validate legal IDs against the actually supplied frozen catalogue, not retrieval classification within that catalogue. Unknown IDs, unsent laws and duplicate IDs are evidence errors. They do not invent citations or trigger uncaught ValueErrors. Existing partial-review isolation remains in effect.
- Same-title/different-version laws have distinct IDs. Validity/metadata cautions are retained; explicit ID selection is provenance, not proof of substantive applicability.
- Direct check repair retains complete contract and legal catalogues. Horizontal validation occurs inside per-candidate recovery, before aggregate materialization.

## Verification

- Source offline regression: 480 passed, 42 skipped (unavailable historical fixtures).
- Packaged image regression: 439 passed, 42 skipped; no network or model access.
- Covers selected laws without exact names, unknown-law repair, supplied-catalog retention, no invented technical fields, candidate-to-Finding propagation, horizontal sibling isolation and rule legacy-output rejection.
- Deployment changes only framework and framework-worker in 13009/vettingtest. Other stack/container identities and preexisting review rows were verified unchanged before live acceptance.
- Incremental image reuses 146 base layers; six source files; added compressed image size approximately 173 KB.

Live acceptance uses `output/unified-evidence-five-file-test-plan.json`, exactly one new review per specified PDF, distributed A-strong/B-strong/A-weak/B-weak/A-strong. The final acceptance report is maintained outside this repository in the workspace output directory. Partial results must not be reported as complete substantive reviews.

Live acceptance completed: five results returned (16/19/10/13/4 Findings), 62 Findings including 22 rule Findings, 119 exact original-text bindings and nine task-level legal citations verified. All results remain PARTIAL; file 5 had no applicable rule decisions or bound laws. The recorded result-to-frontend replay passed 47 tests. No additional review submissions were made to improve the acceptance outcome.

## Why earlier displayed laws were only the Civil Code

Read-only audit of the preceding five reviews found 59 retrieved law units (40 Civil Code, 19 other laws), but only 20 survived the existing check-binding stage. Five excluded units had all their linked legal issues unresolved; the remaining 34 failed other existing check-mapping conditions. Those filters were not changed by this output-protocol migration.

The active index contains 686,348 retrieval units under 17,140 distinct stored titles; these counts are catalogue inventory, not a statement that all laws/versions are current or verified. Previous final results contained only one bound legal citation. Non-Civil-Code provisions did enter some model prompts, but this does not mean they yielded an accepted legal Finding. Output binding and upstream legal retrieval/check mapping are separate stages; explicit IDs fix the former, not automatically the latter.
