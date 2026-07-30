ALTER TABLE proof_policy_lifecycle_operation
  ADD COLUMN idempotency_key text,
  ADD COLUMN request_fingerprint text,
  ADD COLUMN attempt_count integer NOT NULL DEFAULT 1;

CREATE UNIQUE INDEX proof_policy_lifecycle_operation_tenant_request
  ON proof_policy_lifecycle_operation(tenant_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
