CREATE TABLE contract_document (
  id text PRIMARY KEY,
  tenant_id text NOT NULL,
  user_id text NOT NULL,
  contract_version_id text NOT NULL,
  original_name text NOT NULL,
  content_type text NOT NULL
    CHECK (content_type IN (
      'application/pdf',
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )),
  file_type text NOT NULL CHECK (file_type IN ('pdf', 'docx')),
  file_size bigint NOT NULL CHECK (file_size > 0 AND file_size <= 26214400),
  content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  storage_path text NOT NULL,
  active_generation_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, tenant_id),
  UNIQUE (id, tenant_id, user_id),
  UNIQUE (tenant_id, user_id, contract_version_id, content_hash)
);

CREATE INDEX idx_contract_document_scope
  ON contract_document(tenant_id, user_id, contract_version_id);

CREATE TABLE contract_parse_generation (
  id text PRIMARY KEY,
  tenant_id text NOT NULL,
  document_id text NOT NULL,
  generation_no integer NOT NULL CHECK (generation_no > 0),
  content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  parser_version text NOT NULL,
  status text NOT NULL CHECK (status IN ('CREATED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
  block_count integer NOT NULL DEFAULT 0 CHECK (block_count >= 0),
  contract_ir_json jsonb,
  ir_hash text CHECK (ir_hash IS NULL OR ir_hash ~ '^sha256:[0-9a-f]{64}$'),
  started_at timestamptz,
  completed_at timestamptz,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, tenant_id),
  UNIQUE (document_id, generation_no),
  FOREIGN KEY (document_id, tenant_id)
    REFERENCES contract_document(id, tenant_id) ON DELETE CASCADE,
  CHECK (
    status <> 'SUCCEEDED'
    OR (
      block_count > 0
      AND contract_ir_json IS NOT NULL
      AND jsonb_typeof(contract_ir_json) = 'object'
      AND ir_hash IS NOT NULL
      AND completed_at IS NOT NULL
    )
  ),
  CHECK (status <> 'FAILED' OR error_code IS NOT NULL)
);

CREATE UNIQUE INDEX uq_contract_generation_succeeded
  ON contract_parse_generation(document_id, content_hash, parser_version)
  WHERE status = 'SUCCEEDED';

CREATE UNIQUE INDEX uq_contract_generation_in_progress
  ON contract_parse_generation(document_id, content_hash, parser_version)
  WHERE status IN ('CREATED', 'RUNNING');

ALTER TABLE contract_document
  ADD CONSTRAINT fk_contract_document_active_generation
  FOREIGN KEY (active_generation_id)
  REFERENCES contract_parse_generation(id)
  ON DELETE SET NULL
  DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION contract_validate_active_generation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.active_generation_id IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM contract_parse_generation generation
    WHERE generation.id = NEW.active_generation_id
      AND generation.document_id = NEW.id
      AND generation.status = 'SUCCEEDED'
  ) THEN
    RAISE EXCEPTION 'active_generation_id must reference a SUCCEEDED generation for the same document'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_contract_document_active_generation
BEFORE INSERT OR UPDATE OF active_generation_id ON contract_document
FOR EACH ROW EXECUTE FUNCTION contract_validate_active_generation();

CREATE TABLE contract_document_block (
  block_id text PRIMARY KEY,
  tenant_id text NOT NULL,
  generation_id text NOT NULL,
  block_no integer NOT NULL CHECK (block_no > 0),
  block_type text NOT NULL,
  page_number integer CHECK (page_number IS NULL OR page_number > 0),
  paragraph_no integer CHECK (paragraph_no IS NULL OR paragraph_no > 0),
  char_start integer NOT NULL CHECK (char_start >= 0),
  char_end integer NOT NULL CHECK (char_end > char_start),
  text text NOT NULL CHECK (char_length(text) = char_end - char_start),
  heading_path jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(heading_path) = 'array'),
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata_json) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (generation_id, block_no),
  FOREIGN KEY (generation_id, tenant_id)
    REFERENCES contract_parse_generation(id, tenant_id) ON DELETE CASCADE
);

CREATE INDEX idx_contract_block_generation
  ON contract_document_block(generation_id, block_no);

CREATE TABLE contract_review_run (
  id text PRIMARY KEY,
  tenant_id text NOT NULL,
  user_id text NOT NULL,
  business_task_id text NOT NULL,
  contract_version_id text NOT NULL,
  document_id text NOT NULL,
  idempotency_key text NOT NULL,
  request_id text NOT NULL,
  request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
  file_sha256 text NOT NULL CHECK (file_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  perspective text NOT NULL CHECK (perspective IN ('PARTY_A', 'PARTY_B')),
  our_party_name text,
  contract_type text NOT NULL CHECK (contract_type = 'AUTO'),
  review_attitude text NOT NULL CHECK (review_attitude = 'NEUTRAL'),
  status text NOT NULL CHECK (status IN ('CREATED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
  current_stage text CHECK (current_stage IS NULL OR current_stage IN (
    'PARSING',
    'PARTY_RESOLUTION',
    'IR_EXTRACTION',
    'RIGHTS_OBLIGATIONS',
    'RISK_REVIEW',
    'EVIDENCE_VERIFICATION',
    'FINALIZING'
  )),
  active_attempt_no integer CHECK (active_attempt_no IS NULL OR active_attempt_no > 0),
  cancel_requested boolean NOT NULL DEFAULT false,
  version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
  error_code text,
  error_message text,
  retryable boolean NOT NULL DEFAULT false,
  user_action_required boolean NOT NULL DEFAULT false,
  error_details_json jsonb,
  schema_version text NOT NULL CHECK (schema_version = '1.0'),
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, tenant_id),
  UNIQUE (tenant_id, business_task_id),
  UNIQUE (tenant_id, user_id, idempotency_key),
  FOREIGN KEY (document_id, tenant_id, user_id)
    REFERENCES contract_document(id, tenant_id, user_id),
  CHECK (error_details_json IS NULL OR jsonb_typeof(error_details_json) = 'object'),
  CHECK (
    (status = 'CREATED' AND current_stage IS NULL AND active_attempt_no IS NULL)
    OR (status = 'RUNNING' AND current_stage IS NOT NULL AND active_attempt_no IS NOT NULL)
    OR status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
  ),
  CHECK (status <> 'FAILED' OR error_code IS NOT NULL)
);

CREATE INDEX idx_contract_review_scope
  ON contract_review_run(tenant_id, user_id, created_at DESC);
CREATE INDEX idx_contract_review_status
  ON contract_review_run(status, updated_at);

CREATE TABLE contract_framework_attempt (
  review_id text NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  tenant_id text NOT NULL,
  framework_task_id text,
  framework_run_id text,
  status text NOT NULL CHECK (status IN (
    'CREATING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'ORPHANED'
  )),
  current_stage text CHECK (current_stage IS NULL OR current_stage IN (
    'PARSING',
    'PARTY_RESOLUTION',
    'IR_EXTRACTION',
    'RIGHTS_OBLIGATIONS',
    'RISK_REVIEW',
    'EVIDENCE_VERIFICATION',
    'FINALIZING'
  )),
  last_event_sequence bigint NOT NULL DEFAULT 0 CHECK (last_event_sequence >= 0),
  last_activity_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  orphaned_at timestamptz,
  orphan_reason text,
  is_active boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (review_id, attempt_no),
  UNIQUE (review_id, attempt_no, tenant_id),
  UNIQUE (review_id, attempt_no, tenant_id, framework_task_id, framework_run_id),
  UNIQUE (framework_task_id),
  UNIQUE (framework_run_id),
  FOREIGN KEY (review_id, tenant_id)
    REFERENCES contract_review_run(id, tenant_id) ON DELETE CASCADE,
  CHECK ((framework_task_id IS NULL) = (framework_run_id IS NULL)),
  CHECK (NOT is_active OR framework_task_id IS NOT NULL)
);

CREATE UNIQUE INDEX uq_contract_attempt_active
  ON contract_framework_attempt(review_id)
  WHERE is_active;

CREATE TABLE contract_review_stage_result (
  id text PRIMARY KEY,
  tenant_id text NOT NULL,
  review_id text NOT NULL,
  attempt_no integer NOT NULL,
  framework_task_id text NOT NULL,
  framework_run_id text NOT NULL,
  callback_id text NOT NULL,
  callback_type text NOT NULL CHECK (callback_type IN ('STAGE_RESULT', 'RUN_SUCCEEDED', 'RUN_FAILED')),
  event_sequence bigint NOT NULL CHECK (event_sequence >= 0),
  stage_id text,
  result_type text,
  result_json jsonb,
  error_json jsonb,
  validation_status text NOT NULL CHECK (validation_status IN ('PENDING', 'VALIDATED', 'REJECTED', 'IGNORED')),
  ignored_reason text,
  received_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (review_id, attempt_no, callback_id),
  UNIQUE (review_id, attempt_no, event_sequence),
  FOREIGN KEY (
    review_id, attempt_no, tenant_id, framework_task_id, framework_run_id
  ) REFERENCES contract_framework_attempt(
    review_id, attempt_no, tenant_id, framework_task_id, framework_run_id
  ) ON DELETE CASCADE,
  CHECK (result_json IS NULL OR jsonb_typeof(result_json) = 'object'),
  CHECK (error_json IS NULL OR jsonb_typeof(error_json) = 'object'),
  CHECK (
    (callback_type = 'STAGE_RESULT' AND stage_id IS NOT NULL AND result_type IS NOT NULL
      AND result_json IS NOT NULL AND error_json IS NULL)
    OR (callback_type = 'RUN_SUCCEEDED' AND stage_id IS NULL AND result_type IS NULL
      AND result_json IS NULL AND error_json IS NULL)
    OR (callback_type = 'RUN_FAILED' AND result_type IS NULL
      AND result_json IS NULL AND error_json IS NOT NULL)
  )
);

CREATE INDEX idx_contract_stage_result_run
  ON contract_review_stage_result(review_id, attempt_no, event_sequence);

CREATE TABLE contract_review_result (
  id text PRIMARY KEY,
  tenant_id text NOT NULL,
  review_id text NOT NULL UNIQUE,
  attempt_no integer NOT NULL,
  schema_version text NOT NULL CHECK (schema_version = '1.0'),
  result_hash text NOT NULL CHECK (result_hash ~ '^sha256:[0-9a-f]{64}$'),
  result_json jsonb NOT NULL CHECK (jsonb_typeof(result_json) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (review_id, attempt_no, tenant_id)
    REFERENCES contract_framework_attempt(review_id, attempt_no, tenant_id)
);

CREATE OR REPLACE FUNCTION contract_touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_contract_document_updated_at
BEFORE UPDATE ON contract_document
FOR EACH ROW EXECUTE FUNCTION contract_touch_updated_at();

CREATE TRIGGER trg_contract_generation_updated_at
BEFORE UPDATE ON contract_parse_generation
FOR EACH ROW EXECUTE FUNCTION contract_touch_updated_at();

CREATE TRIGGER trg_contract_review_updated_at
BEFORE UPDATE ON contract_review_run
FOR EACH ROW EXECUTE FUNCTION contract_touch_updated_at();

CREATE TRIGGER trg_contract_attempt_updated_at
BEFORE UPDATE ON contract_framework_attempt
FOR EACH ROW EXECUTE FUNCTION contract_touch_updated_at();

CREATE TRIGGER trg_contract_result_updated_at
BEFORE UPDATE ON contract_review_result
FOR EACH ROW EXECUTE FUNCTION contract_touch_updated_at();
