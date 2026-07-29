ALTER TABLE contract_review_run
  ADD COLUMN IF NOT EXISTS model_pack_id text NOT NULL DEFAULT 'api-rerank';

ALTER TABLE contract_framework_attempt
  ADD COLUMN IF NOT EXISTS model_pack_id text NOT NULL DEFAULT 'api-rerank';

ALTER TABLE contract_review_run
  DROP CONSTRAINT IF EXISTS ck_contract_review_model_pack_nonempty;

ALTER TABLE contract_review_run
  ADD CONSTRAINT ck_contract_review_model_pack_nonempty
  CHECK (btrim(model_pack_id) <> '');

ALTER TABLE contract_framework_attempt
  DROP CONSTRAINT IF EXISTS ck_contract_attempt_model_pack_nonempty;

ALTER TABLE contract_framework_attempt
  ADD CONSTRAINT ck_contract_attempt_model_pack_nonempty
  CHECK (btrim(model_pack_id) <> '');
