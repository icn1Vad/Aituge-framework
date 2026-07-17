ALTER TABLE proof_document
  ADD COLUMN structure_profile text NOT NULL DEFAULT 'article',
  ADD COLUMN structure_diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE proof_retrieval_unit
  ADD COLUMN unit_type text NOT NULL DEFAULT 'article';

CREATE INDEX idx_proof_document_structure_profile
  ON proof_document(structure_profile);

CREATE INDEX idx_proof_unit_type
  ON proof_retrieval_unit(document_id, unit_type, clause_ordinal);
