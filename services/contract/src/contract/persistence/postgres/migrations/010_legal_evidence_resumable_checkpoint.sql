-- A source release can legitimately have several projections (for example,
-- different embedding model versions). Checkpoint identity therefore follows
-- the immutable projection release, not only the MySQL source release.

ALTER TABLE legal_evidence_projection_checkpoint
  DROP CONSTRAINT IF EXISTS legal_evidence_projection_checkpoint_pkey;

ALTER TABLE legal_evidence_projection_checkpoint
  ADD PRIMARY KEY (projection_release_id);

CREATE INDEX IF NOT EXISTS idx_legal_projection_checkpoint_source
  ON legal_evidence_projection_checkpoint(source_release_id, updated_at DESC);
