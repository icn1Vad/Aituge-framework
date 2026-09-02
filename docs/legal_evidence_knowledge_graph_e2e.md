# Legal evidence knowledge graph E2E

This document describes the implemented path on
`feature/legal-knowledge-graph-e2e`. It complements the service contracts and
tests; it is not a second architecture.

## End-to-end path

1. Java imports one immutable legal release into MySQL and atomically marks it
   active only after manifest counts and SHA-256 checks pass.
2. The Contract service streams the active MySQL release and assembles one
   retrieval unit per article (or preamble). Paragraphs and items remain with
   their article unless a size boundary requires a paragraph/item split.
3. PostgreSQL stores the rebuildable unit, Chinese full-text, vector and legal
   relation projections. Projection identity freezes the source manifest,
   projection algorithm, relation extractor and embedding model.
4. Contract IR plus the seven review domains produce fact-specific legal
   issues. Each issue carries its question, facts, parties, contract object,
   source domain and evidence needs.
5. The adaptive planner combines exact statute/article lookup, Chinese
   full-text keyword search, vector retrieval, reranking and verified graph
   expansion. Coverage and marginal information gain decide when to stop;
   result count is not a business stopping condition.
6. A `LegalEvidenceBundle` freezes evidence, paths, applicability decisions,
   conflicts, unresolved issues and all model/data versions. The versioned
   binder assigns safe evidence IDs to seven-domain checks.
7. Findings reuse those evidence IDs. Java validates and persists the frozen
   trace, and the frontend can open the cited instrument/article/node.

## Publication and recovery invariants

- Source NDJSON is streamed. The input publication is never modified.
- Semantic similarity is never persisted as a legal relation.
- Named relations require an exact unique title target. Ambiguous and unmatched
  targets enter a review queue.
- When MySQL contains authoritative deterministic relations, PostgreSQL maps
  their source and target node IDs to the exact containing retrieval units and
  does not run the legacy projection-time extractor again. The fallback
  extractor is reserved for older releases whose relation table is empty.
- Every persisted relation records source node, evidence text/span, extractor
  version, confidence and verification status.
- `CANDIDATE` relations remain traceable but are not traversed by the planner.
- Unknown temporal metadata is never converted into an active-law claim.
- A projection cannot activate unless database counts equal the source root
  count and every configured embedding is present and hash-compatible.
- The durable checkpoint is written after both unit and embedding writes. On
  recovery, the indexer rewinds to the earliest missing embedding version, so
  interruption between those writes cannot create a permanently skipped gap.
- A new projection algorithm can reuse a prior immutable vector only when the
  unit ID, content hash, embedding-input hash, embedding profile and model
  version all match. Reuse copies the frozen vector into the new release and
  never calls the model again for that unit.
- Relation projection is rebuilt from scratch for a staged release and remains
  immutable after sealing.

## 2026-09-01 reconciled local release

The old release contained 1,392 review events but only 1,370 unique source
documents. Exactly 22 documents each had two distinct review events: a metadata
recognition issue and a structural numbering issue. The old manifest counted
events while the importer counted unique documents. The reconciler now groups
by source document and preserves every event/reason instead of discarding one
or changing the expected count.

Local release (not committed):

- release: `legal-release-20260901-kg-v2`
- directory: `E:\ProofSpaceLegalKG\data-work\legal-release-20260901-kg-v2`
- instruments: 17,138
- versions: 17,184
- nodes: 2,277,486
- deterministic relations: 90,162
- document review queue: 1,370
- relation review queue: 26,732
- temporal review queue: 24

All 17,184 version validity values remain `UNKNOWN`: absence of an explicit
repeal is not evidence that a law is currently effective. Explicit effective
or repeal dates are preserved together with their source spans and
verification status.

## Verification commands

```bash
pytest services/contract/tests -q
pytest services/contract/tests/test_legal_evidence_postgres_integration.py -q
python services/contract/scripts/legal_evidence_e2e_evaluation.py --help
```

The real evaluation script writes a JSON report outside Git. It measures legal
issue coverage, exact/keyword/vector hits, zero-evidence refusal,
traceability, duplicate evidence and graph-added evidence. Synthetic tests
cover verified repealed/future-effective and wrong-jurisdiction exclusions;
real-release results explicitly retain the release's unknown-status caution.
