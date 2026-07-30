ALTER TABLE contract_review_run
  ADD COLUMN IF NOT EXISTS execution_mode text NOT NULL DEFAULT 'FULL_REVIEW',
  ADD COLUMN IF NOT EXISTS confirmed_party_a_name text,
  ADD COLUMN IF NOT EXISTS confirmed_party_b_name text;

ALTER TABLE contract_review_run
  DROP CONSTRAINT IF EXISTS contract_review_run_execution_mode_check,
  ADD CONSTRAINT contract_review_run_execution_mode_check
    CHECK (execution_mode IN ('FULL_REVIEW', 'PARTY_RESOLUTION')),
  DROP CONSTRAINT IF EXISTS contract_review_run_confirmed_parties_check,
  ADD CONSTRAINT contract_review_run_confirmed_parties_check
    CHECK (
      (confirmed_party_a_name IS NULL AND confirmed_party_b_name IS NULL)
      OR (
        confirmed_party_a_name IS NOT NULL
        AND confirmed_party_b_name IS NOT NULL
        AND confirmed_party_a_name <> confirmed_party_b_name
      )
    );

CREATE INDEX IF NOT EXISTS idx_contract_review_party_resolution_scope
  ON contract_review_run(tenant_id, user_id, contract_version_id, execution_mode, updated_at DESC);
