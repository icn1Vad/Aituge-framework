CREATE TABLE proof_intra_conflict_audit_warning (
  id bigserial PRIMARY KEY,
  audit_run_id text NOT NULL REFERENCES proof_audit_run(id) ON DELETE CASCADE,
  target_unit_id text NOT NULL REFERENCES proof_retrieval_unit(id) ON DELETE CASCADE,
  finding_index integer CHECK (finding_index IS NULL OR finding_index >= 0),
  code text NOT NULL,
  message text NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_proof_intra_conflict_warning_run
  ON proof_intra_conflict_audit_warning(audit_run_id);

CREATE INDEX idx_proof_intra_conflict_warning_target
  ON proof_intra_conflict_audit_warning(target_unit_id);

