# Model output recovery — 13009, 2026-09-06

Scope: commercial-financial output reception, one bounded repair, CF-005 applicability,
and failure diagnostics/usage. No change to legal provenance, contract anchor integrity,
the required-completion gate, or the frozen Java result DTO. Other deployments are untouched.

## Processing

1. Parse JSON, retain original structured errors.
2. Normalize known containers and exact reference shorthand. Unknown evidence stays invalid.
   Missing/blank explanations remain missing: never synthesize “reviewed, no risk”.
   Never manufacture a Finding to satisfy a required candidate decision.
3. Validate the normalized schema, collecting independent reference errors too. Preserve
   both initial and current failures instead of throwing only the initial exception.
4. Check business conditions and materialize only valid source-backed evidence.
5. At most one repair of identified failed checks. Include their frozen context and actual
   schema; merge into the original result. Unrequested checks must remain byte-equivalent.
   Existing nonempty notes, statuses, Findings and evidence remain protected. Only a missing,
   null or blank note may be filled without being treated as a changed business conclusion.
   Unparseable JSON cannot identify a check: the fallback is limited to the same financial
   batch, never another domain or the entire contract.
6. Revalidate. A failed required check still prevents a successful final review.

## CF-005

- `TRIGGER_NOT_MET`: verified non-payer, or explicit after-performance payment facts.
- `RISK_NOT_CONFIRMED`: a named safeguard supported by its exact contract text.
- `RISK_CONFIRMED`: risk and source evidence; existing payer/provenance gates remain.
- `INSUFFICIENT_EVIDENCE`: FAILED, not a no-risk result. Unknown payer is not non-applicability.

The old ambiguity between “not applicable” and “mitigated by a safeguard” is removed.
Legacy no-risk outputs are mapped only when deterministic contract facts establish non-applicability.

## Diagnostics and privacy

`CONTRACT_REVIEW_DIAGNOSTIC_DIR` is opt-in and enabled only on 13009 at
`/app/runtime/review-output-diagnostics` in its existing private Framework volume.
Files are immutable UUID-named JSON, directory 0700, files 0600. They contain original
model text, normalized output/diff paths, every validation error, source request, and
actual returned usage for offline replay. They are not frontend artifacts or Git content.
Runtime secrets and bearer-shaped credentials are redacted. Disk-write failures emit a
safe diagnostic failure code and do not replace the original review exception.

Failed batches retain returned Token counts and attempt diagnostics, not zero usage.
Provider exceptions without a completion are explicitly marked usage UNKNOWN, not free.
The complete base bundle is privately recorded before the whole-review completeness gate.
Records contain contract data: administrator-only local inspection and explicit retention
management are required. No automatic deletion is introduced by this patch.

## Verification

Run the tests in the existing runtime image with `--network none`, a read-only source mount,
and a tmpfs `.pytest_cache`; no model service is reachable. Tests cover empty/null/whitespace
notes, scoped repair, wrong evidence, independent diagnostics, payer/payee reversal, unknown
payer, after-performance payment, safeguards, strict whole-review failure, file permissions,
secret redaction, and preservation of failed-call usage. Keep existing generic/horizontal,
legal-policy, consolidation and formal-callback tests in the regression suite.

Deployment uses a five-file source overlay on the existing runtime image. Only the 13009
Framework API and worker are recreated; no base/model dependencies or other systems rebuild.
Historical failed tasks are not rewritten as successes. Real model reliability is distinct
from offline regression success; report the actual scope and cost of any live validation.
