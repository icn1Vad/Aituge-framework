-- Persist the exact rule-pack selection used by one contract review.
-- The data is metadata only: contract text, prompts and model output never belong here.
ALTER TABLE contract_review_run
  ADD COLUMN IF NOT EXISTS primary_playbook_id text,
  ADD COLUMN IF NOT EXISTS selected_playbook_ids_json jsonb NOT NULL DEFAULT '["base_neutral"]'::jsonb,
  ADD COLUMN IF NOT EXISTS roles_by_playbook_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS rule_release_id text;

ALTER TABLE contract_review_run
  DROP CONSTRAINT IF EXISTS contract_review_run_contract_type_check,
  ADD CONSTRAINT contract_review_run_contract_type_check
    CHECK (contract_type ~ '^[A-Z][A-Z0-9_]{0,79}$'),
  DROP CONSTRAINT IF EXISTS contract_review_run_review_attitude_check,
  ADD CONSTRAINT contract_review_run_review_attitude_check
    CHECK (review_attitude IN ('STRONG', 'NEUTRAL', 'WEAK')),
  DROP CONSTRAINT IF EXISTS ck_contract_review_primary_playbook_id,
  ADD CONSTRAINT ck_contract_review_primary_playbook_id
    CHECK (
      primary_playbook_id IS NULL
      OR primary_playbook_id ~ '^[a-z][a-z0-9_-]{0,79}$'
    ),
  DROP CONSTRAINT IF EXISTS ck_contract_review_selected_playbook_ids_json,
  ADD CONSTRAINT ck_contract_review_selected_playbook_ids_json
    CHECK (
      CASE
        WHEN jsonb_typeof(selected_playbook_ids_json) = 'array'
        THEN jsonb_array_length(selected_playbook_ids_json) BETWEEN 1 AND 20
        ELSE false
      END
    ),
  DROP CONSTRAINT IF EXISTS ck_contract_review_roles_by_playbook_json,
  ADD CONSTRAINT ck_contract_review_roles_by_playbook_json
    CHECK (jsonb_typeof(roles_by_playbook_json) = 'object'),
  DROP CONSTRAINT IF EXISTS ck_contract_review_rule_release_id,
  ADD CONSTRAINT ck_contract_review_rule_release_id
    CHECK (
      rule_release_id IS NULL
      OR rule_release_id ~ '^[A-Za-z0-9._:-]{1,160}$'
    );
