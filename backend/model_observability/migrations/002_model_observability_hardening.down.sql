DROP TRIGGER IF EXISTS trg_tuge_model_event_validate_fallback
  ON tuge_model_invocation_event;
DROP FUNCTION IF EXISTS tuge_validate_model_event_fallback();

DROP TABLE IF EXISTS tuge_model_observability_query_snapshot;

DROP TRIGGER IF EXISTS trg_tuge_model_event_append_only
  ON tuge_model_invocation_event;
DROP FUNCTION IF EXISTS tuge_reject_model_event_mutation();

ALTER TABLE tuge_model_invocation_projection
  DROP CONSTRAINT IF EXISTS fk_tuge_model_projection_fallback,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_retry_reason,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_uncertain_usage,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_running_payload,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_ttft_latency,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_provider_request_hash,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_currency,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_cost_metadata,
  ADD CONSTRAINT ck_tuge_model_projection_cost_metadata CHECK (
    (cost_amount IS NULL AND cost_currency IS NULL AND cost_source IS NULL)
    OR (
      cost_amount IS NOT NULL
      AND cost_currency IS NOT NULL
      AND cost_source IS NOT NULL
    )
  );

ALTER TABLE tuge_model_invocation_event
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_terminal_semantics,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_hash_by_fact,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_uncertain_usage,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_nonterminal_payload,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_ttft_latency,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_retry_reason,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_metadata_object,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_provider_request_hash,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_currency,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_cost_metadata,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_schema,
  ADD CONSTRAINT ck_tuge_model_event_cost_metadata CHECK (
    (cost_amount IS NULL AND cost_currency IS NULL AND cost_source IS NULL)
    OR (
      cost_amount IS NOT NULL
      AND cost_currency IS NOT NULL
      AND cost_source IS NOT NULL
    )
  ),
  ADD CONSTRAINT ck_tuge_model_event_terminal_semantics CHECK (
    (
      event_type = 'MODEL_INVOCATION_SUCCEEDED'
      AND outcome = 'SUCCESS'
      AND dispatch_status = 'DISPATCHED'
    )
    OR (
      event_type = 'MODEL_INVOCATION_FAILED'
      AND outcome IN ('FAILURE', 'CANCELLED', 'TIMEOUT', 'PARTIAL')
    )
    OR (
      event_type = 'MODEL_INVOCATION_VALIDATION_FAILED'
      AND outcome = 'FAILURE'
    )
    OR (
      event_type = 'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED'
      AND outcome = 'DENIED'
      AND dispatch_status = 'DISPATCHED'
    )
    OR (
      event_type = 'MODEL_INVOCATION_OUTCOME_UNKNOWN'
      AND outcome = 'UNKNOWN'
      AND dispatch_status <> 'NOT_DISPATCHED'
    )
    OR (
      event_type = 'MODEL_INVOCATION_ABANDONED'
      AND outcome = 'ABANDONED'
    )
    OR event_type NOT IN (
      'MODEL_INVOCATION_SUCCEEDED',
      'MODEL_INVOCATION_FAILED',
      'MODEL_INVOCATION_VALIDATION_FAILED',
      'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
      'MODEL_INVOCATION_OUTCOME_UNKNOWN',
      'MODEL_INVOCATION_ABANDONED'
    )
  );

ALTER TABLE tuge_model_invocation_projection
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_causal_sequence,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_terminal_sequence,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_dispatch_sequence,
  DROP CONSTRAINT IF EXISTS ck_tuge_model_projection_started_sequence,
  DROP COLUMN IF EXISTS terminal_sequence,
  DROP COLUMN IF EXISTS dispatch_sequence,
  DROP COLUMN IF EXISTS started_sequence;

DROP INDEX IF EXISTS uq_tuge_model_event_server_sequence;

ALTER TABLE tuge_model_invocation_event
  DROP CONSTRAINT IF EXISTS ck_tuge_model_event_server_sequence,
  DROP COLUMN IF EXISTS server_sequence;

DROP TABLE IF EXISTS tuge_model_observability_sequence;
