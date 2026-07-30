CREATE TABLE proof_policy_create_request (
  tenant_id text NOT NULL,
  idempotency_key text NOT NULL,
  request_fingerprint text NOT NULL,
  status text NOT NULL
    CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
  policy_id text,
  document_id text,
  error_code text,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  PRIMARY KEY (tenant_id, idempotency_key)
);

CREATE INDEX idx_proof_policy_create_request_policy
  ON proof_policy_create_request(tenant_id, policy_id);

ALTER TABLE proof_policy_lifecycle_operation
  DROP CONSTRAINT proof_policy_lifecycle_operation_action_check;

ALTER TABLE proof_policy_lifecycle_operation
  ADD CONSTRAINT proof_policy_lifecycle_operation_action_check
  CHECK (action IN ('activate', 'expire', 'discard', 'delete'));
