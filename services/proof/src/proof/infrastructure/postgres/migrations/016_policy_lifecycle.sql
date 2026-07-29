ALTER TABLE proof_policy
  DROP CONSTRAINT proof_policy_version_format_check;

UPDATE proof_policy
SET version =
  'v' || (1 + version_seq / 100)::text ||
  '.' || ((version_seq % 100) / 10)::text ||
  '.' || (version_seq % 10)::text;

ALTER TABLE proof_policy
  ADD CONSTRAINT proof_policy_version_format_check
  CHECK (
    version =
      'v' || (1 + version_seq / 100)::text ||
      '.' || ((version_seq % 100) / 10)::text ||
      '.' || (version_seq % 10)::text
  );

CREATE TABLE proof_policy_delete_tombstone (
  operation_id text PRIMARY KEY,
  tenant_id text NOT NULL,
  policy_id text NOT NULL,
  original_storage_path text NOT NULL,
  trash_storage_path text NOT NULL,
  status text NOT NULL DEFAULT 'prepared'
    CHECK (status IN ('prepared', 'deleted', 'cleanup_failed')),
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE INDEX idx_proof_policy_delete_tombstone_tenant_policy
  ON proof_policy_delete_tombstone(tenant_id, policy_id, created_at DESC);

CREATE TABLE proof_policy_lifecycle_operation (
  operation_id text PRIMARY KEY,
  tenant_id text NOT NULL,
  policy_id text NOT NULL,
  action text NOT NULL CHECK (action IN ('activate', 'expire', 'delete')),
  status text NOT NULL
    CHECK (status IN ('ACCEPTED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
  framework_task_id text,
  framework_run_id text,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE INDEX idx_proof_policy_lifecycle_operation_tenant_policy
  ON proof_policy_lifecycle_operation(tenant_id, policy_id, created_at DESC);
