-- Forward-only resume identities.  Existing v1/v2 projections keep a neutral
-- placeholder because published child rows are immutable; projection v3 writes
-- exact hashes for every newly staged row.

ALTER TABLE legal_evidence_unit
  ADD COLUMN IF NOT EXISTS projection_hash char(64)
    NOT NULL DEFAULT repeat('0', 64);

ALTER TABLE legal_evidence_unit
  ADD COLUMN IF NOT EXISTS embedding_input_hash char(64)
    NOT NULL DEFAULT repeat('0', 64);

ALTER TABLE legal_evidence_embedding
  ADD COLUMN IF NOT EXISTS embedding_input_hash char(64)
    NOT NULL DEFAULT repeat('0', 64);

ALTER TABLE legal_evidence_unit
  ADD CONSTRAINT ck_legal_evidence_unit_projection_hash
  CHECK (projection_hash ~ '^[0-9a-f]{64}$') NOT VALID;

ALTER TABLE legal_evidence_unit
  ADD CONSTRAINT ck_legal_evidence_unit_embedding_input_hash
  CHECK (embedding_input_hash ~ '^[0-9a-f]{64}$') NOT VALID;

ALTER TABLE legal_evidence_embedding
  ADD CONSTRAINT ck_legal_evidence_embedding_input_hash
  CHECK (embedding_input_hash ~ '^[0-9a-f]{64}$') NOT VALID;

ALTER TABLE legal_evidence_unit
  VALIDATE CONSTRAINT ck_legal_evidence_unit_projection_hash;

ALTER TABLE legal_evidence_unit
  VALIDATE CONSTRAINT ck_legal_evidence_unit_embedding_input_hash;

ALTER TABLE legal_evidence_embedding
  VALIDATE CONSTRAINT ck_legal_evidence_embedding_input_hash;
