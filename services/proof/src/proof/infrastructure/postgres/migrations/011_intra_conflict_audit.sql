ALTER TABLE proof_audit_run
  ADD COLUMN intra_conflict_status text NOT NULL DEFAULT 'pending'
    CHECK (intra_conflict_status IN ('pending', 'running', 'completed', 'failed')),
  ADD COLUMN intra_conflict_error_message text;

-- Existing runs predate this stage and must not become permanently incomplete.
UPDATE proof_audit_run
SET intra_conflict_status = 'completed';

CREATE TABLE proof_draft_retrieval_embedding (
  audit_run_id text NOT NULL REFERENCES proof_audit_run(id) ON DELETE CASCADE,
  retrieval_unit_id text NOT NULL REFERENCES proof_retrieval_unit(id) ON DELETE CASCADE,
  profile_id text NOT NULL,
  dimensions integer NOT NULL CHECK (dimensions > 0),
  embedding vector NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (audit_run_id, retrieval_unit_id)
);

CREATE INDEX idx_proof_draft_embedding_profile
  ON proof_draft_retrieval_embedding(audit_run_id, profile_id, dimensions);

CREATE TABLE proof_intra_conflict_audit_finding (
  id bigserial PRIMARY KEY,
  audit_run_id text NOT NULL REFERENCES proof_audit_run(id) ON DELETE CASCADE,
  source_unit_id text NOT NULL REFERENCES proof_retrieval_unit(id) ON DELETE CASCADE,
  candidate_ids jsonb NOT NULL,
  conflict_type text NOT NULL
    CHECK (conflict_type IN (
      'numeric_conflict', 'authority_conflict', 'process_conflict', 'rule_reversal'
    )),
  problem text NOT NULL,
  suggestion text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_proof_intra_conflict_finding_run
  ON proof_intra_conflict_audit_finding(audit_run_id);

CREATE INDEX idx_proof_intra_conflict_finding_source
  ON proof_intra_conflict_audit_finding(source_unit_id);
