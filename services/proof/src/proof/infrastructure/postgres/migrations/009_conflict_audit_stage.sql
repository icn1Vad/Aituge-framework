ALTER TABLE proof_audit_run
  ADD COLUMN conflict_status text NOT NULL DEFAULT 'pending'
    CHECK (conflict_status IN ('pending', 'running', 'completed', 'failed')),
  ADD COLUMN conflict_error_message text;

CREATE TABLE proof_conflict_audit_finding (
  id bigserial PRIMARY KEY,
  audit_run_id text NOT NULL REFERENCES proof_audit_run(id) ON DELETE CASCADE,
  source_unit_id text NOT NULL REFERENCES proof_retrieval_unit(id) ON DELETE CASCADE,
  candidate_ids jsonb NOT NULL,
  conflict_type text NOT NULL
    CHECK (conflict_type IN (
      'numeric_conflict', 'authority_conflict', 'process_conflict', 'rule_reversal'
    )),
  mechanism text NOT NULL,
  matter text NOT NULL,
  scope_overlap text NOT NULL,
  contradiction text NOT NULL,
  evidence jsonb NOT NULL,
  severity text NOT NULL CHECK (severity IN ('high', 'medium', 'low')),
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  suggestion text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_proof_conflict_audit_finding_run
  ON proof_conflict_audit_finding(audit_run_id);

CREATE INDEX idx_proof_conflict_audit_finding_source
  ON proof_conflict_audit_finding(source_unit_id);
