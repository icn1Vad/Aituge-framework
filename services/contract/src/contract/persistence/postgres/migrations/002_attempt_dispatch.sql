ALTER TABLE contract_framework_attempt
  DROP CONSTRAINT IF EXISTS contract_framework_attempt_status_check;

ALTER TABLE contract_framework_attempt
  ADD COLUMN IF NOT EXISTS execution_status text,
  ADD COLUMN IF NOT EXISTS dispatch_status text NOT NULL DEFAULT 'PENDING_DISPATCH',
  ADD COLUMN IF NOT EXISTS dispatch_attempts integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS next_dispatch_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS lease_owner text,
  ADD COLUMN IF NOT EXISTS lease_until timestamptz,
  ADD COLUMN IF NOT EXISTS lease_version bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_heartbeat_at timestamptz,
  ADD COLUMN IF NOT EXISTS request_fingerprint text NOT NULL
    DEFAULT 'sha256:0000000000000000000000000000000000000000000000000000000000000000',
  ADD COLUMN IF NOT EXISTS dispatch_response_fingerprint text;

UPDATE contract_framework_attempt
SET execution_status = CASE status
  WHEN 'CREATING' THEN 'PENDING'
  ELSE status
END,
dispatch_status = CASE
  WHEN framework_task_id IS NOT NULL AND framework_run_id IS NOT NULL THEN 'SENT'
  ELSE 'PENDING_DISPATCH'
END
WHERE execution_status IS NULL;

UPDATE contract_framework_attempt
SET status = CASE status
  WHEN 'CREATING' THEN 'PENDING'
  ELSE status
END
WHERE status = 'CREATING';

ALTER TABLE contract_framework_attempt
  ALTER COLUMN execution_status SET NOT NULL;

ALTER TABLE contract_framework_attempt
  ADD CONSTRAINT contract_framework_attempt_execution_status_check
    CHECK (execution_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'ORPHANED')),
  ADD CONSTRAINT contract_framework_attempt_dispatch_status_check
    CHECK (dispatch_status IN ('PENDING_DISPATCH', 'CLAIMED', 'SENT', 'RETRY_WAIT', 'DISPATCH_FAILED')),
  ADD CONSTRAINT contract_framework_attempt_dispatch_attempts_check
    CHECK (dispatch_attempts >= 0),
  ADD CONSTRAINT contract_framework_attempt_lease_version_check
    CHECK (lease_version >= 0),
  ADD CONSTRAINT contract_framework_attempt_request_fingerprint_check
    CHECK (request_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
  ADD CONSTRAINT contract_framework_attempt_response_fingerprint_check
    CHECK (
      dispatch_response_fingerprint IS NULL
      OR dispatch_response_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
  ADD CONSTRAINT contract_framework_attempt_status_projection_check
    CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'ORPHANED'));

CREATE OR REPLACE FUNCTION contract_sync_attempt_status()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.status = 'CREATING' THEN
    NEW.status := 'PENDING';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.execution_status IS NULL THEN
      NEW.execution_status := NEW.status;
    END IF;
    IF NEW.status IS NULL THEN
      NEW.status := NEW.execution_status;
    END IF;
  ELSIF NEW.execution_status IS DISTINCT FROM OLD.execution_status
        AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
    NEW.status := NEW.execution_status;
  ELSIF NEW.status IS DISTINCT FROM OLD.status
        AND NEW.execution_status IS NOT DISTINCT FROM OLD.execution_status THEN
    NEW.execution_status := NEW.status;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_contract_sync_attempt_status
  ON contract_framework_attempt;

CREATE TRIGGER trg_contract_sync_attempt_status
BEFORE INSERT OR UPDATE OF status, execution_status
ON contract_framework_attempt
FOR EACH ROW EXECUTE FUNCTION contract_sync_attempt_status();

CREATE INDEX IF NOT EXISTS idx_contract_attempt_dispatch
  ON contract_framework_attempt(dispatch_status, next_dispatch_at, lease_until, created_at);

CREATE INDEX IF NOT EXISTS idx_contract_attempt_execution
  ON contract_framework_attempt(execution_status, updated_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_contract_callback_id
  ON contract_review_stage_result(callback_id);
