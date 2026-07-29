ALTER TABLE contract_framework_attempt
  ADD COLUMN IF NOT EXISTS latest_lease_version bigint NOT NULL DEFAULT 0
  CHECK (latest_lease_version >= 0);

ALTER TABLE contract_review_stage_result
  ADD COLUMN IF NOT EXISTS lease_version bigint NOT NULL DEFAULT 0
  CHECK (lease_version >= 0);

CREATE INDEX IF NOT EXISTS idx_contract_stage_result_lease
  ON contract_review_stage_result(review_id, attempt_no, lease_version);
