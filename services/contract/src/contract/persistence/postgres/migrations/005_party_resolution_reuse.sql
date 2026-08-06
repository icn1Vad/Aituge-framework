ALTER TABLE contract_review_run
  ADD COLUMN IF NOT EXISTS party_resolution_id text;

CREATE INDEX IF NOT EXISTS idx_contract_review_run_party_resolution_id
  ON contract_review_run (tenant_id, party_resolution_id)
  WHERE party_resolution_id IS NOT NULL;
