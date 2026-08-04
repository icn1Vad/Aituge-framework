CREATE TABLE tuge_model_invocation_event (
  event_id varchar(80) PRIMARY KEY,
  event_type varchar(80) NOT NULL,
  schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
  logical_call_id varchar(80) NOT NULL,
  invocation_id varchar(80) NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),
  dispatch_status varchar(32) NOT NULL CHECK (
    dispatch_status IN ('NOT_DISPATCHED', 'DISPATCHED', 'DISPATCH_UNKNOWN')
  ),
  fallback_from_invocation_id varchar(80),
  occurred_at timestamptz NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  tenant_id varchar(64) NOT NULL,
  user_id varchar(120),
  feature_code varchar(120) NOT NULL,
  task_id varchar(80),
  run_id varchar(80),
  stage_id varchar(120),
  request_id varchar(120),
  trace_id varchar(80),
  provider varchar(80) NOT NULL,
  provider_request_id_hash varchar(64),
  model_pack_id varchar(120),
  model_pack_version varchar(64),
  model_name varchar(160) NOT NULL,
  deployment_name varchar(160),
  provider_region varchar(80),
  privacy_mode varchar(16) NOT NULL CHECK (privacy_mode IN ('STANDARD', 'PRIVATE')),
  route_type varchar(16) NOT NULL CHECK (route_type IN ('LOCAL', 'EXTERNAL')),
  model_config_id varchar(120),
  input_token_count bigint CHECK (input_token_count IS NULL OR input_token_count >= 0),
  output_token_count bigint CHECK (output_token_count IS NULL OR output_token_count >= 0),
  latency_ms bigint CHECK (latency_ms IS NULL OR latency_ms >= 0),
  time_to_first_token_ms bigint CHECK (
    time_to_first_token_ms IS NULL OR time_to_first_token_ms >= 0
  ),
  outcome varchar(32),
  error_code varchar(120),
  retry_reason varchar(120),
  cost_amount numeric(20, 8) CHECK (cost_amount IS NULL OR cost_amount >= 0),
  cost_currency varchar(3) CHECK (
    cost_currency IS NULL OR cost_currency ~ '^[A-Z]{3}$'
  ),
  cost_source varchar(16) CHECK (
    cost_source IS NULL OR cost_source IN ('PROVIDER', 'ESTIMATED')
  ),
  pricing_version varchar(80),
  cost_calculated_at timestamptz,
  service_version varchar(80),
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT ck_tuge_model_event_type CHECK (
    event_type IN (
      'MODEL_INVOCATION_STARTED',
      'MODEL_INVOCATION_DISPATCHED',
      'MODEL_INVOCATION_DISPATCH_UNKNOWN',
      'MODEL_INVOCATION_SUCCEEDED',
      'MODEL_INVOCATION_FAILED',
      'MODEL_INVOCATION_VALIDATION_FAILED',
      'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
      'MODEL_INVOCATION_OUTCOME_UNKNOWN',
      'MODEL_INVOCATION_ABANDONED'
    )
  ),
  CONSTRAINT ck_tuge_model_event_terminal_outcome CHECK (
    (
      event_type IN (
        'MODEL_INVOCATION_SUCCEEDED',
        'MODEL_INVOCATION_FAILED',
        'MODEL_INVOCATION_VALIDATION_FAILED',
        'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
        'MODEL_INVOCATION_OUTCOME_UNKNOWN',
        'MODEL_INVOCATION_ABANDONED'
      )
      AND outcome IS NOT NULL
    ) OR (
      event_type NOT IN (
        'MODEL_INVOCATION_SUCCEEDED',
        'MODEL_INVOCATION_FAILED',
        'MODEL_INVOCATION_VALIDATION_FAILED',
        'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
        'MODEL_INVOCATION_OUTCOME_UNKNOWN',
        'MODEL_INVOCATION_ABANDONED'
      )
      AND outcome IS NULL
    )
  ),
  CONSTRAINT ck_tuge_model_event_outcome CHECK (
    outcome IS NULL OR outcome IN (
      'SUCCESS', 'FAILURE', 'DENIED', 'CANCELLED', 'TIMEOUT',
      'PARTIAL', 'UNKNOWN', 'ABANDONED'
    )
  ),
  CONSTRAINT ck_tuge_model_event_dispatch_fact CHECK (
    (event_type = 'MODEL_INVOCATION_STARTED' AND dispatch_status = 'NOT_DISPATCHED')
    OR (event_type = 'MODEL_INVOCATION_DISPATCHED' AND dispatch_status = 'DISPATCHED')
    OR (event_type = 'MODEL_INVOCATION_DISPATCH_UNKNOWN' AND dispatch_status = 'DISPATCH_UNKNOWN')
    OR event_type NOT IN (
      'MODEL_INVOCATION_STARTED',
      'MODEL_INVOCATION_DISPATCHED',
      'MODEL_INVOCATION_DISPATCH_UNKNOWN'
    )
  ),
  CONSTRAINT ck_tuge_model_event_terminal_semantics CHECK (
    (event_type = 'MODEL_INVOCATION_SUCCEEDED' AND outcome = 'SUCCESS' AND dispatch_status = 'DISPATCHED')
    OR (event_type = 'MODEL_INVOCATION_FAILED' AND outcome IN ('FAILURE', 'CANCELLED', 'TIMEOUT', 'PARTIAL'))
    OR (event_type = 'MODEL_INVOCATION_VALIDATION_FAILED' AND outcome = 'FAILURE')
    OR (event_type = 'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED' AND outcome = 'DENIED' AND dispatch_status = 'DISPATCHED')
    OR (event_type = 'MODEL_INVOCATION_OUTCOME_UNKNOWN' AND outcome = 'UNKNOWN' AND dispatch_status <> 'NOT_DISPATCHED')
    OR (event_type = 'MODEL_INVOCATION_ABANDONED' AND outcome = 'ABANDONED')
    OR event_type NOT IN (
      'MODEL_INVOCATION_SUCCEEDED',
      'MODEL_INVOCATION_FAILED',
      'MODEL_INVOCATION_VALIDATION_FAILED',
      'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
      'MODEL_INVOCATION_OUTCOME_UNKNOWN',
      'MODEL_INVOCATION_ABANDONED'
    )
  ),
  CONSTRAINT ck_tuge_model_event_fallback CHECK (
    fallback_from_invocation_id IS NULL OR fallback_from_invocation_id <> invocation_id
  ),
  CONSTRAINT ck_tuge_model_event_cost_metadata CHECK (
    (cost_amount IS NULL AND cost_currency IS NULL AND cost_source IS NULL)
    OR (cost_amount IS NOT NULL AND cost_currency IS NOT NULL AND cost_source IS NOT NULL)
  )
);

CREATE UNIQUE INDEX uq_tuge_model_event_started
  ON tuge_model_invocation_event (invocation_id)
  WHERE event_type = 'MODEL_INVOCATION_STARTED';

CREATE UNIQUE INDEX uq_tuge_model_event_logical_attempt_started
  ON tuge_model_invocation_event (logical_call_id, attempt_no)
  WHERE event_type = 'MODEL_INVOCATION_STARTED';

CREATE UNIQUE INDEX uq_tuge_model_event_dispatch_conclusion
  ON tuge_model_invocation_event (invocation_id)
  WHERE event_type IN (
    'MODEL_INVOCATION_DISPATCHED',
    'MODEL_INVOCATION_DISPATCH_UNKNOWN'
  );

CREATE UNIQUE INDEX uq_tuge_model_event_terminal
  ON tuge_model_invocation_event (invocation_id)
  WHERE event_type IN (
    'MODEL_INVOCATION_SUCCEEDED',
    'MODEL_INVOCATION_FAILED',
    'MODEL_INVOCATION_VALIDATION_FAILED',
    'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
    'MODEL_INVOCATION_OUTCOME_UNKNOWN',
    'MODEL_INVOCATION_ABANDONED'
  );

CREATE INDEX idx_tuge_model_event_tenant_occurred
  ON tuge_model_invocation_event (tenant_id, occurred_at DESC, event_id DESC);
CREATE INDEX idx_tuge_model_event_task
  ON tuge_model_invocation_event (task_id, occurred_at DESC)
  WHERE task_id IS NOT NULL;
CREATE INDEX idx_tuge_model_event_run
  ON tuge_model_invocation_event (run_id, occurred_at DESC)
  WHERE run_id IS NOT NULL;
CREATE INDEX idx_tuge_model_event_request
  ON tuge_model_invocation_event (request_id, occurred_at DESC)
  WHERE request_id IS NOT NULL;
CREATE INDEX idx_tuge_model_event_trace
  ON tuge_model_invocation_event (trace_id, occurred_at DESC)
  WHERE trace_id IS NOT NULL;

CREATE TABLE tuge_model_invocation_projection (
  invocation_id varchar(80) PRIMARY KEY,
  projection_version bigint NOT NULL CHECK (projection_version >= 1),
  data_as_of timestamptz NOT NULL,
  logical_call_id varchar(80) NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),
  fallback_from_invocation_id varchar(80),
  tenant_id varchar(64) NOT NULL,
  user_id varchar(120),
  feature_code varchar(120) NOT NULL,
  task_id varchar(80),
  run_id varchar(80),
  stage_id varchar(120),
  request_id varchar(120),
  trace_id varchar(80),
  started_at timestamptz NOT NULL,
  finished_at timestamptz,
  ingested_at timestamptz NOT NULL,
  lifecycle_status varchar(16) NOT NULL CHECK (
    lifecycle_status IN ('RUNNING', 'TERMINAL')
  ),
  dispatch_status varchar(32) NOT NULL CHECK (
    dispatch_status IN ('NOT_DISPATCHED', 'DISPATCHED', 'DISPATCH_UNKNOWN')
  ),
  outcome varchar(32),
  error_code varchar(120),
  retry_reason varchar(120),
  provider varchar(80) NOT NULL,
  provider_request_id_hash varchar(64),
  model_pack_id varchar(120),
  model_pack_version varchar(64),
  model_name varchar(160) NOT NULL,
  deployment_name varchar(160),
  provider_region varchar(80),
  privacy_mode varchar(16) NOT NULL CHECK (privacy_mode IN ('STANDARD', 'PRIVATE')),
  route_type varchar(16) NOT NULL CHECK (route_type IN ('LOCAL', 'EXTERNAL')),
  model_config_id varchar(120),
  input_token_count bigint CHECK (input_token_count IS NULL OR input_token_count >= 0),
  output_token_count bigint CHECK (output_token_count IS NULL OR output_token_count >= 0),
  latency_ms bigint CHECK (latency_ms IS NULL OR latency_ms >= 0),
  time_to_first_token_ms bigint CHECK (
    time_to_first_token_ms IS NULL OR time_to_first_token_ms >= 0
  ),
  cost_amount numeric(20, 8) CHECK (cost_amount IS NULL OR cost_amount >= 0),
  cost_currency varchar(3) CHECK (
    cost_currency IS NULL OR cost_currency ~ '^[A-Z]{3}$'
  ),
  cost_source varchar(16) CHECK (
    cost_source IS NULL OR cost_source IN ('PROVIDER', 'ESTIMATED')
  ),
  pricing_version varchar(80),
  cost_calculated_at timestamptz,
  service_version varchar(80),
  CONSTRAINT uq_tuge_model_projection_logical_attempt UNIQUE (
    logical_call_id,
    attempt_no
  ),
  CONSTRAINT ck_tuge_model_projection_fallback CHECK (
    fallback_from_invocation_id IS NULL OR fallback_from_invocation_id <> invocation_id
  ),
  CONSTRAINT ck_tuge_model_projection_terminal CHECK (
    (lifecycle_status = 'RUNNING' AND outcome IS NULL AND finished_at IS NULL)
    OR (lifecycle_status = 'TERMINAL' AND outcome IS NOT NULL AND finished_at IS NOT NULL)
  ),
  CONSTRAINT ck_tuge_model_projection_outcome CHECK (
    outcome IS NULL OR outcome IN (
      'SUCCESS', 'FAILURE', 'DENIED', 'CANCELLED', 'TIMEOUT',
      'PARTIAL', 'UNKNOWN', 'ABANDONED'
    )
  ),
  CONSTRAINT ck_tuge_model_projection_time_order CHECK (
    finished_at IS NULL OR finished_at >= started_at
  ),
  CONSTRAINT ck_tuge_model_projection_dispatch_outcome CHECK (
    outcome IS NULL OR outcome NOT IN ('SUCCESS', 'DENIED')
    OR dispatch_status = 'DISPATCHED'
  ),
  CONSTRAINT ck_tuge_model_projection_unknown_dispatch CHECK (
    outcome IS NULL OR outcome <> 'UNKNOWN'
    OR dispatch_status <> 'NOT_DISPATCHED'
  ),
  CONSTRAINT ck_tuge_model_projection_cost_metadata CHECK (
    (cost_amount IS NULL AND cost_currency IS NULL AND cost_source IS NULL)
    OR (cost_amount IS NOT NULL AND cost_currency IS NOT NULL AND cost_source IS NOT NULL)
  )
);

CREATE INDEX idx_tuge_model_projection_tenant_started
  ON tuge_model_invocation_projection (tenant_id, started_at DESC, invocation_id DESC);
CREATE INDEX idx_tuge_model_projection_tenant_dispatch_started
  ON tuge_model_invocation_projection (
    tenant_id,
    dispatch_status,
    started_at DESC,
    invocation_id DESC
  );
CREATE INDEX idx_tuge_model_projection_task
  ON tuge_model_invocation_projection (task_id, started_at DESC)
  WHERE task_id IS NOT NULL;
CREATE INDEX idx_tuge_model_projection_run
  ON tuge_model_invocation_projection (run_id, started_at DESC)
  WHERE run_id IS NOT NULL;
CREATE INDEX idx_tuge_model_projection_trace
  ON tuge_model_invocation_projection (trace_id, started_at DESC)
  WHERE trace_id IS NOT NULL;
