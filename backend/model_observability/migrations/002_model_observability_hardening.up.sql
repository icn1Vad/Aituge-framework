DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM tuge_model_invocation_event
    WHERE schema_version <> 1
      OR jsonb_typeof(metadata_json) IS DISTINCT FROM 'object'
      OR (
        provider_request_id_hash IS NOT NULL
        AND provider_request_id_hash !~ '^[0-9a-f]{64}$'
      )
      OR (
        cost_currency IS NOT NULL
        AND cost_currency !~ '^[A-Z]{3}$'
      )
      OR (
        (cost_amount IS NULL AND (
          cost_currency IS NOT NULL
          OR cost_source IS NOT NULL
          OR pricing_version IS NOT NULL
          OR cost_calculated_at IS NOT NULL
        ))
        OR (
          cost_amount IS NOT NULL
          AND (cost_currency IS NULL OR cost_source IS NULL)
        )
      )
      OR retry_reason IS NOT NULL
         AND retry_reason NOT IN (
           'PROVIDER_RETRY',
           'OUTPUT_REPAIR',
           'GUARDRAIL_RETRY'
         )
      OR (
        time_to_first_token_ms IS NOT NULL
        AND latency_ms IS NOT NULL
        AND time_to_first_token_ms > latency_ms
      )
  ) THEN
    RAISE EXCEPTION 'MODEL_OBSERVABILITY_PREFLIGHT_VALUE_INVALID'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM tuge_model_invocation_event
    WHERE (
      event_type NOT IN (
        'MODEL_INVOCATION_SUCCEEDED',
        'MODEL_INVOCATION_FAILED',
        'MODEL_INVOCATION_VALIDATION_FAILED',
        'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
        'MODEL_INVOCATION_OUTCOME_UNKNOWN',
        'MODEL_INVOCATION_ABANDONED'
      )
      AND (
        input_token_count IS NOT NULL
        OR output_token_count IS NOT NULL
        OR latency_ms IS NOT NULL
        OR time_to_first_token_ms IS NOT NULL
        OR outcome IS NOT NULL
        OR error_code IS NOT NULL
        OR retry_reason IS NOT NULL
        OR cost_amount IS NOT NULL
        OR cost_currency IS NOT NULL
        OR cost_source IS NOT NULL
        OR pricing_version IS NOT NULL
        OR cost_calculated_at IS NOT NULL
      )
    )
    OR (
      dispatch_status <> 'DISPATCHED'
      AND (
        input_token_count IS NOT NULL
        OR output_token_count IS NOT NULL
        OR time_to_first_token_ms IS NOT NULL
        OR cost_amount IS NOT NULL
        OR cost_currency IS NOT NULL
        OR cost_source IS NOT NULL
        OR pricing_version IS NOT NULL
        OR cost_calculated_at IS NOT NULL
      )
    )
    OR (
      event_type IN (
        'MODEL_INVOCATION_STARTED',
        'MODEL_INVOCATION_DISPATCH_UNKNOWN'
      )
      AND provider_request_id_hash IS NOT NULL
    )
    OR (
      event_type = 'MODEL_INVOCATION_VALIDATION_FAILED'
      AND dispatch_status <> 'DISPATCHED'
    )
  ) THEN
    RAISE EXCEPTION 'MODEL_OBSERVABILITY_PREFLIGHT_EVENT_PAYLOAD_INVALID'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM tuge_model_invocation_event AS event
    LEFT JOIN tuge_model_invocation_event AS started
      ON started.invocation_id = event.invocation_id
     AND started.event_type = 'MODEL_INVOCATION_STARTED'
    WHERE event.event_type <> 'MODEL_INVOCATION_STARTED'
      AND (
        started.event_id IS NULL
        OR ROW(
          event.schema_version,
          event.logical_call_id,
          event.attempt_no,
          event.fallback_from_invocation_id,
          event.tenant_id,
          event.user_id,
          event.feature_code,
          event.task_id,
          event.run_id,
          event.stage_id,
          event.request_id,
          event.trace_id,
          event.provider,
          event.model_pack_id,
          event.model_pack_version,
          event.model_name,
          event.deployment_name,
          event.provider_region,
          event.privacy_mode,
          event.route_type,
          event.model_config_id,
          event.service_version
        ) IS DISTINCT FROM ROW(
          started.schema_version,
          started.logical_call_id,
          started.attempt_no,
          started.fallback_from_invocation_id,
          started.tenant_id,
          started.user_id,
          started.feature_code,
          started.task_id,
          started.run_id,
          started.stage_id,
          started.request_id,
          started.trace_id,
          started.provider,
          started.model_pack_id,
          started.model_pack_version,
          started.model_name,
          started.deployment_name,
          started.provider_region,
          started.privacy_mode,
          started.route_type,
          started.model_config_id,
          started.service_version
        )
        OR event.occurred_at < started.occurred_at
      )
  ) THEN
    RAISE EXCEPTION 'MODEL_OBSERVABILITY_PREFLIGHT_FIXED_FACT_INVALID'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM tuge_model_invocation_event AS started
    LEFT JOIN tuge_model_invocation_event AS previous
      ON previous.invocation_id = started.fallback_from_invocation_id
     AND previous.event_type = 'MODEL_INVOCATION_STARTED'
    WHERE started.event_type = 'MODEL_INVOCATION_STARTED'
      AND (
        started.provider_request_id_hash IS NOT NULL
        OR (
          started.fallback_from_invocation_id IS NOT NULL
          AND (
            previous.event_id IS NULL
            OR previous.tenant_id IS DISTINCT FROM started.tenant_id
            OR previous.logical_call_id IS DISTINCT FROM
                 started.logical_call_id
            OR previous.attempt_no >= started.attempt_no
          )
        )
      )
  ) THEN
    RAISE EXCEPTION 'MODEL_OBSERVABILITY_PREFLIGHT_STARTED_INVALID'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM tuge_model_invocation_event AS terminal
    LEFT JOIN tuge_model_invocation_event AS dispatch
      ON dispatch.invocation_id = terminal.invocation_id
     AND dispatch.event_type IN (
       'MODEL_INVOCATION_DISPATCHED',
       'MODEL_INVOCATION_DISPATCH_UNKNOWN'
     )
    WHERE terminal.event_type IN (
      'MODEL_INVOCATION_SUCCEEDED',
      'MODEL_INVOCATION_FAILED',
      'MODEL_INVOCATION_VALIDATION_FAILED',
      'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
      'MODEL_INVOCATION_OUTCOME_UNKNOWN',
      'MODEL_INVOCATION_ABANDONED'
    )
      AND (
        (
          terminal.dispatch_status = 'NOT_DISPATCHED'
          AND (
            dispatch.event_id IS NOT NULL
            OR terminal.provider_request_id_hash IS NOT NULL
          )
        )
        OR (
          terminal.dispatch_status <> 'NOT_DISPATCHED'
          AND (
            (
              dispatch.event_id IS NULL
              AND NOT (
                terminal.event_type = 'MODEL_INVOCATION_OUTCOME_UNKNOWN'
                AND terminal.dispatch_status = 'DISPATCH_UNKNOWN'
                AND terminal.provider_request_id_hash IS NULL
              )
            )
            OR dispatch.dispatch_status IS DISTINCT FROM
                 terminal.dispatch_status
            OR terminal.occurred_at < dispatch.occurred_at
            OR (
              dispatch.provider_request_id_hash IS NOT NULL
              AND terminal.provider_request_id_hash IS DISTINCT FROM
                   dispatch.provider_request_id_hash
            )
          )
        )
      )
  ) THEN
    RAISE EXCEPTION 'MODEL_OBSERVABILITY_PREFLIGHT_DISPATCH_INVALID'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM tuge_model_invocation_projection AS projection
    FULL JOIN (
      SELECT * FROM tuge_model_invocation_event
      WHERE event_type = 'MODEL_INVOCATION_STARTED'
    ) AS started
      ON started.invocation_id = projection.invocation_id
    LEFT JOIN tuge_model_invocation_event AS dispatch
      ON dispatch.invocation_id = started.invocation_id
     AND dispatch.event_type IN (
       'MODEL_INVOCATION_DISPATCHED',
       'MODEL_INVOCATION_DISPATCH_UNKNOWN'
     )
    LEFT JOIN tuge_model_invocation_event AS terminal
      ON terminal.invocation_id = started.invocation_id
     AND terminal.event_type IN (
       'MODEL_INVOCATION_SUCCEEDED',
       'MODEL_INVOCATION_FAILED',
       'MODEL_INVOCATION_VALIDATION_FAILED',
       'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
       'MODEL_INVOCATION_OUTCOME_UNKNOWN',
       'MODEL_INVOCATION_ABANDONED'
     )
    WHERE projection.invocation_id IS NULL
       OR started.event_id IS NULL
       OR ROW(
         projection.logical_call_id,
         projection.attempt_no,
         projection.fallback_from_invocation_id,
         projection.tenant_id,
         projection.user_id,
         projection.feature_code,
         projection.task_id,
         projection.run_id,
         projection.stage_id,
         projection.request_id,
         projection.trace_id,
         projection.provider,
         projection.model_pack_id,
         projection.model_pack_version,
         projection.model_name,
         projection.deployment_name,
         projection.provider_region,
         projection.privacy_mode,
         projection.route_type,
         projection.model_config_id,
         projection.service_version,
         projection.started_at
       ) IS DISTINCT FROM ROW(
         started.logical_call_id,
         started.attempt_no,
         started.fallback_from_invocation_id,
         started.tenant_id,
         started.user_id,
         started.feature_code,
         started.task_id,
         started.run_id,
         started.stage_id,
         started.request_id,
         started.trace_id,
         started.provider,
         started.model_pack_id,
         started.model_pack_version,
         started.model_name,
         started.deployment_name,
         started.provider_region,
         started.privacy_mode,
         started.route_type,
         started.model_config_id,
         started.service_version,
         started.occurred_at
       )
       OR projection.dispatch_status IS DISTINCT FROM
          COALESCE(dispatch.dispatch_status, 'NOT_DISPATCHED')
       OR projection.lifecycle_status IS DISTINCT FROM
          CASE
            WHEN terminal.event_id IS NULL THEN 'RUNNING'
            ELSE 'TERMINAL'
          END
       OR projection.projection_version IS DISTINCT FROM
          (
            1
            + CASE WHEN dispatch.event_id IS NULL THEN 0 ELSE 1 END
            + CASE WHEN terminal.event_id IS NULL THEN 0 ELSE 1 END
          )
       OR projection.provider_request_id_hash IS DISTINCT FROM
          COALESCE(
            terminal.provider_request_id_hash,
            dispatch.provider_request_id_hash
          )
       OR projection.ingested_at IS DISTINCT FROM
          COALESCE(
            terminal.ingested_at,
            dispatch.ingested_at,
            started.ingested_at
          )
       OR projection.data_as_of IS DISTINCT FROM
          COALESCE(
            terminal.ingested_at,
            dispatch.ingested_at,
            started.ingested_at
          )
       OR (
         terminal.event_id IS NULL
         AND (
           projection.finished_at IS NOT NULL
           OR projection.outcome IS NOT NULL
           OR projection.error_code IS NOT NULL
           OR projection.retry_reason IS NOT NULL
           OR projection.input_token_count IS NOT NULL
           OR projection.output_token_count IS NOT NULL
           OR projection.latency_ms IS NOT NULL
           OR projection.time_to_first_token_ms IS NOT NULL
           OR projection.cost_amount IS NOT NULL
           OR projection.cost_currency IS NOT NULL
           OR projection.cost_source IS NOT NULL
           OR projection.pricing_version IS NOT NULL
           OR projection.cost_calculated_at IS NOT NULL
         )
       )
       OR (
         terminal.event_id IS NOT NULL
         AND ROW(
           projection.finished_at,
           projection.outcome,
           projection.error_code,
           projection.retry_reason,
           projection.input_token_count,
           projection.output_token_count,
           projection.latency_ms,
           projection.time_to_first_token_ms,
           projection.cost_amount,
           projection.cost_currency,
           projection.cost_source,
           projection.pricing_version,
           projection.cost_calculated_at
         ) IS DISTINCT FROM ROW(
           terminal.occurred_at,
           terminal.outcome,
           terminal.error_code,
           terminal.retry_reason,
           terminal.input_token_count,
           terminal.output_token_count,
           terminal.latency_ms,
           terminal.time_to_first_token_ms,
           terminal.cost_amount,
           terminal.cost_currency,
           terminal.cost_source,
           terminal.pricing_version,
           terminal.cost_calculated_at
         )
       )
  ) THEN
    RAISE EXCEPTION 'MODEL_OBSERVABILITY_PREFLIGHT_PROJECTION_INVALID'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

CREATE TABLE tuge_model_observability_sequence (
  sequence_name varchar(80) PRIMARY KEY,
  sequence_value bigint NOT NULL DEFAULT 0,
  CONSTRAINT ck_tuge_model_observability_sequence_value
    CHECK (sequence_value >= 0)
);

INSERT INTO tuge_model_observability_sequence (sequence_name, sequence_value)
VALUES ('event', 0);

ALTER TABLE tuge_model_invocation_event
  ADD COLUMN server_sequence bigint;

WITH lifecycle_order AS (
  SELECT
    event_id,
    invocation_id,
    event_type,
    occurred_at,
    ingested_at,
    MIN(ingested_at) FILTER (
      WHERE event_type = 'MODEL_INVOCATION_STARTED'
    ) OVER (PARTITION BY invocation_id) AS invocation_started_ingested_at
  FROM tuge_model_invocation_event
),
numbered AS (
  SELECT
    event_id,
    ROW_NUMBER() OVER (
      ORDER BY
        COALESCE(invocation_started_ingested_at, ingested_at),
        invocation_id,
        CASE
          WHEN event_type = 'MODEL_INVOCATION_STARTED' THEN 0
          WHEN event_type IN (
            'MODEL_INVOCATION_DISPATCHED',
            'MODEL_INVOCATION_DISPATCH_UNKNOWN'
          ) THEN 1
          ELSE 2
        END,
        ingested_at,
        occurred_at,
        event_id
    ) AS server_sequence
  FROM lifecycle_order
)
UPDATE tuge_model_invocation_event AS event
SET server_sequence = numbered.server_sequence
FROM numbered
WHERE numbered.event_id = event.event_id;

ALTER TABLE tuge_model_invocation_event
  ALTER COLUMN server_sequence SET NOT NULL,
  ADD CONSTRAINT ck_tuge_model_event_server_sequence
    CHECK (server_sequence >= 1);

CREATE UNIQUE INDEX uq_tuge_model_event_server_sequence
  ON tuge_model_invocation_event (server_sequence);

UPDATE tuge_model_observability_sequence
SET sequence_value = (
  SELECT COALESCE(MAX(server_sequence), 0)
  FROM tuge_model_invocation_event
)
WHERE sequence_name = 'event';

ALTER TABLE tuge_model_invocation_projection
  ADD COLUMN started_sequence bigint,
  ADD COLUMN dispatch_sequence bigint,
  ADD COLUMN terminal_sequence bigint;

UPDATE tuge_model_invocation_projection AS projection
SET
  started_sequence = facts.started_sequence,
  dispatch_sequence = facts.dispatch_sequence,
  terminal_sequence = facts.terminal_sequence
FROM (
  SELECT
    invocation_id,
    MIN(server_sequence) FILTER (
      WHERE event_type = 'MODEL_INVOCATION_STARTED'
    ) AS started_sequence,
    MIN(server_sequence) FILTER (
      WHERE event_type IN (
        'MODEL_INVOCATION_DISPATCHED',
        'MODEL_INVOCATION_DISPATCH_UNKNOWN'
      )
    ) AS dispatch_sequence,
    MIN(server_sequence) FILTER (
      WHERE event_type IN (
        'MODEL_INVOCATION_SUCCEEDED',
        'MODEL_INVOCATION_FAILED',
        'MODEL_INVOCATION_VALIDATION_FAILED',
        'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
        'MODEL_INVOCATION_OUTCOME_UNKNOWN',
        'MODEL_INVOCATION_ABANDONED'
      )
    ) AS terminal_sequence
  FROM tuge_model_invocation_event
  GROUP BY invocation_id
) AS facts
WHERE facts.invocation_id = projection.invocation_id;

ALTER TABLE tuge_model_invocation_projection
  ALTER COLUMN started_sequence SET NOT NULL,
  ADD CONSTRAINT ck_tuge_model_projection_started_sequence
    CHECK (started_sequence >= 1),
  ADD CONSTRAINT ck_tuge_model_projection_dispatch_sequence
    CHECK (
      dispatch_sequence IS NULL OR dispatch_sequence > started_sequence
    ),
  ADD CONSTRAINT ck_tuge_model_projection_terminal_sequence
    CHECK (
      terminal_sequence IS NULL OR terminal_sequence > started_sequence
    ),
  ADD CONSTRAINT ck_tuge_model_projection_causal_sequence
    CHECK (
      terminal_sequence IS NULL
      OR dispatch_sequence IS NULL
      OR terminal_sequence > dispatch_sequence
    );

ALTER TABLE tuge_model_invocation_event
  DROP CONSTRAINT ck_tuge_model_event_cost_metadata,
  DROP CONSTRAINT ck_tuge_model_event_terminal_semantics,
  ADD CONSTRAINT ck_tuge_model_event_schema
    CHECK (schema_version = 1),
  ADD CONSTRAINT ck_tuge_model_event_cost_metadata CHECK (
    (
      cost_amount IS NULL
      AND cost_currency IS NULL
      AND cost_source IS NULL
      AND pricing_version IS NULL
      AND cost_calculated_at IS NULL
    )
    OR (
      cost_amount IS NOT NULL
      AND cost_currency IS NOT NULL
      AND cost_source IS NOT NULL
    )
  ),
  ADD CONSTRAINT ck_tuge_model_event_currency CHECK (
    cost_currency IS NULL OR cost_currency ~ '^[A-Z]{3}$'
  ),
  ADD CONSTRAINT ck_tuge_model_event_provider_request_hash CHECK (
    provider_request_id_hash IS NULL
    OR provider_request_id_hash ~ '^[0-9a-f]{64}$'
  ),
  ADD CONSTRAINT ck_tuge_model_event_metadata_object CHECK (
    jsonb_typeof(metadata_json) = 'object'
  ),
  ADD CONSTRAINT ck_tuge_model_event_retry_reason CHECK (
    retry_reason IS NULL
    OR retry_reason IN (
      'PROVIDER_RETRY',
      'OUTPUT_REPAIR',
      'GUARDRAIL_RETRY'
    )
  ),
  ADD CONSTRAINT ck_tuge_model_event_ttft_latency CHECK (
    time_to_first_token_ms IS NULL
    OR latency_ms IS NULL
    OR time_to_first_token_ms <= latency_ms
  ),
  ADD CONSTRAINT ck_tuge_model_event_nonterminal_payload CHECK (
    event_type IN (
      'MODEL_INVOCATION_SUCCEEDED',
      'MODEL_INVOCATION_FAILED',
      'MODEL_INVOCATION_VALIDATION_FAILED',
      'MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED',
      'MODEL_INVOCATION_OUTCOME_UNKNOWN',
      'MODEL_INVOCATION_ABANDONED'
    )
    OR (
      input_token_count IS NULL
      AND output_token_count IS NULL
      AND latency_ms IS NULL
      AND time_to_first_token_ms IS NULL
      AND outcome IS NULL
      AND error_code IS NULL
      AND retry_reason IS NULL
      AND cost_amount IS NULL
      AND cost_currency IS NULL
      AND cost_source IS NULL
      AND pricing_version IS NULL
      AND cost_calculated_at IS NULL
    )
  ),
  ADD CONSTRAINT ck_tuge_model_event_uncertain_usage CHECK (
    dispatch_status = 'DISPATCHED'
    OR (
      input_token_count IS NULL
      AND output_token_count IS NULL
      AND time_to_first_token_ms IS NULL
      AND cost_amount IS NULL
      AND cost_currency IS NULL
      AND cost_source IS NULL
      AND pricing_version IS NULL
      AND cost_calculated_at IS NULL
    )
  ),
  ADD CONSTRAINT ck_tuge_model_event_hash_by_fact CHECK (
    event_type NOT IN (
      'MODEL_INVOCATION_STARTED',
      'MODEL_INVOCATION_DISPATCH_UNKNOWN'
    )
    OR provider_request_id_hash IS NULL
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
      AND dispatch_status = 'DISPATCHED'
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
  DROP CONSTRAINT ck_tuge_model_projection_cost_metadata,
  ADD CONSTRAINT ck_tuge_model_projection_cost_metadata CHECK (
    (
      cost_amount IS NULL
      AND cost_currency IS NULL
      AND cost_source IS NULL
      AND pricing_version IS NULL
      AND cost_calculated_at IS NULL
    )
    OR (
      cost_amount IS NOT NULL
      AND cost_currency IS NOT NULL
      AND cost_source IS NOT NULL
    )
  ),
  ADD CONSTRAINT ck_tuge_model_projection_currency CHECK (
    cost_currency IS NULL OR cost_currency ~ '^[A-Z]{3}$'
  ),
  ADD CONSTRAINT ck_tuge_model_projection_provider_request_hash CHECK (
    provider_request_id_hash IS NULL
    OR provider_request_id_hash ~ '^[0-9a-f]{64}$'
  ),
  ADD CONSTRAINT ck_tuge_model_projection_ttft_latency CHECK (
    time_to_first_token_ms IS NULL
    OR latency_ms IS NULL
    OR time_to_first_token_ms <= latency_ms
  ),
  ADD CONSTRAINT ck_tuge_model_projection_running_payload CHECK (
    lifecycle_status = 'TERMINAL'
    OR (
      outcome IS NULL
      AND error_code IS NULL
      AND retry_reason IS NULL
      AND input_token_count IS NULL
      AND output_token_count IS NULL
      AND latency_ms IS NULL
      AND time_to_first_token_ms IS NULL
      AND cost_amount IS NULL
      AND cost_currency IS NULL
      AND cost_source IS NULL
      AND pricing_version IS NULL
      AND cost_calculated_at IS NULL
    )
  ),
  ADD CONSTRAINT ck_tuge_model_projection_uncertain_usage CHECK (
    dispatch_status = 'DISPATCHED'
    OR (
      input_token_count IS NULL
      AND output_token_count IS NULL
      AND time_to_first_token_ms IS NULL
      AND cost_amount IS NULL
      AND cost_currency IS NULL
      AND cost_source IS NULL
      AND pricing_version IS NULL
      AND cost_calculated_at IS NULL
    )
  ),
  ADD CONSTRAINT ck_tuge_model_projection_retry_reason CHECK (
    retry_reason IS NULL
    OR retry_reason IN (
      'PROVIDER_RETRY',
      'OUTPUT_REPAIR',
      'GUARDRAIL_RETRY'
    )
  );

ALTER TABLE tuge_model_invocation_projection
  ADD CONSTRAINT fk_tuge_model_projection_fallback
  FOREIGN KEY (fallback_from_invocation_id)
  REFERENCES tuge_model_invocation_projection (invocation_id)
  ON DELETE RESTRICT
  DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION tuge_validate_model_event_fallback()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  previous_event tuge_model_invocation_event%ROWTYPE;
  started_event tuge_model_invocation_event%ROWTYPE;
  dispatch_event tuge_model_invocation_event%ROWTYPE;
BEGIN
  IF NEW.event_type = 'MODEL_INVOCATION_STARTED' THEN
    IF NEW.fallback_from_invocation_id IS NULL THEN
      RETURN NEW;
    END IF;
    SELECT *
    INTO previous_event
    FROM tuge_model_invocation_event
    WHERE invocation_id = NEW.fallback_from_invocation_id
      AND event_type = 'MODEL_INVOCATION_STARTED';
    IF NOT FOUND
      OR previous_event.tenant_id IS DISTINCT FROM NEW.tenant_id
      OR previous_event.logical_call_id IS DISTINCT FROM NEW.logical_call_id
      OR previous_event.attempt_no >= NEW.attempt_no
    THEN
      RAISE EXCEPTION 'MODEL_INVOCATION_FALLBACK_INVALID'
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;

  SELECT *
  INTO started_event
  FROM tuge_model_invocation_event
  WHERE invocation_id = NEW.invocation_id
    AND event_type = 'MODEL_INVOCATION_STARTED';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'MODEL_INVOCATION_STARTED_FACT_MISSING'
      USING ERRCODE = '23514';
  END IF;
  IF ROW(
    NEW.schema_version,
    NEW.logical_call_id,
    NEW.attempt_no,
    NEW.fallback_from_invocation_id,
    NEW.tenant_id,
    NEW.user_id,
    NEW.feature_code,
    NEW.task_id,
    NEW.run_id,
    NEW.stage_id,
    NEW.request_id,
    NEW.trace_id,
    NEW.provider,
    NEW.model_pack_id,
    NEW.model_pack_version,
    NEW.model_name,
    NEW.deployment_name,
    NEW.provider_region,
    NEW.privacy_mode,
    NEW.route_type,
    NEW.model_config_id,
    NEW.service_version
  ) IS DISTINCT FROM ROW(
    started_event.schema_version,
    started_event.logical_call_id,
    started_event.attempt_no,
    started_event.fallback_from_invocation_id,
    started_event.tenant_id,
    started_event.user_id,
    started_event.feature_code,
    started_event.task_id,
    started_event.run_id,
    started_event.stage_id,
    started_event.request_id,
    started_event.trace_id,
    started_event.provider,
    started_event.model_pack_id,
    started_event.model_pack_version,
    started_event.model_name,
    started_event.deployment_name,
    started_event.provider_region,
    started_event.privacy_mode,
    started_event.route_type,
    started_event.model_config_id,
    started_event.service_version
  ) THEN
    RAISE EXCEPTION 'MODEL_INVOCATION_FACTS_MISMATCH'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.server_sequence <= started_event.server_sequence
    OR NEW.occurred_at < started_event.occurred_at
  THEN
    RAISE EXCEPTION 'MODEL_INVOCATION_CAUSAL_SEQUENCE_INVALID'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.event_type IN (
    'MODEL_INVOCATION_DISPATCHED',
    'MODEL_INVOCATION_DISPATCH_UNKNOWN'
  ) THEN
    RETURN NEW;
  END IF;

  SELECT *
  INTO dispatch_event
  FROM tuge_model_invocation_event
  WHERE invocation_id = NEW.invocation_id
    AND event_type IN (
      'MODEL_INVOCATION_DISPATCHED',
      'MODEL_INVOCATION_DISPATCH_UNKNOWN'
    );
  IF NEW.dispatch_status = 'NOT_DISPATCHED' THEN
    IF FOUND OR NEW.provider_request_id_hash IS NOT NULL THEN
      RAISE EXCEPTION 'MODEL_INVOCATION_DISPATCH_FACT_INVALID'
        USING ERRCODE = '23514';
    END IF;
  ELSE
    IF NOT FOUND
      AND NEW.event_type = 'MODEL_INVOCATION_OUTCOME_UNKNOWN'
      AND NEW.dispatch_status = 'DISPATCH_UNKNOWN'
      AND NEW.provider_request_id_hash IS NULL
    THEN
      RETURN NEW;
    END IF;
    IF NOT FOUND
      OR dispatch_event.dispatch_status IS DISTINCT FROM NEW.dispatch_status
      OR dispatch_event.server_sequence >= NEW.server_sequence
      OR dispatch_event.occurred_at > NEW.occurred_at
      OR (
        dispatch_event.provider_request_id_hash IS NOT NULL
        AND dispatch_event.provider_request_id_hash IS DISTINCT FROM
             NEW.provider_request_id_hash
      )
    THEN
      RAISE EXCEPTION 'MODEL_INVOCATION_DISPATCH_FACT_INVALID'
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_tuge_model_event_validate_fallback
BEFORE INSERT ON tuge_model_invocation_event
FOR EACH ROW EXECUTE FUNCTION tuge_validate_model_event_fallback();

CREATE OR REPLACE FUNCTION tuge_reject_model_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'tuge_model_invocation_event is append-only'
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER trg_tuge_model_event_append_only
BEFORE UPDATE OR DELETE ON tuge_model_invocation_event
FOR EACH ROW EXECUTE FUNCTION tuge_reject_model_event_mutation();

CREATE TABLE tuge_model_observability_query_snapshot (
  high_watermark_handle varchar(84) PRIMARY KEY,
  query_snapshot_id varchar(80) NOT NULL,
  scope_hash varchar(64) NOT NULL,
  query_hash varchar(64) NOT NULL,
  cursor_signing_key varchar(64) NOT NULL,
  rows_json jsonb NOT NULL,
  row_count integer NOT NULL,
  storage_bytes bigint NOT NULL,
  snapshot_to timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  snapshot_mode varchar(40) NOT NULL,
  max_ingested_at timestamptz,
  max_event_id varchar(80),
  max_sequence bigint,
  data_through timestamptz,
  CONSTRAINT uq_tuge_model_query_snapshot_id UNIQUE (query_snapshot_id),
  CONSTRAINT ck_tuge_model_snapshot_row_count CHECK (row_count >= 0),
  CONSTRAINT ck_tuge_model_snapshot_storage_bytes CHECK (storage_bytes >= 0),
  CONSTRAINT ck_tuge_model_snapshot_mode CHECK (
    snapshot_mode IN (
      'MATERIALIZED_RESULT_SET',
      'APPEND_ONLY_HIGH_WATERMARK'
    )
  ),
  CONSTRAINT ck_tuge_model_snapshot_rows_match CHECK (
    jsonb_typeof(rows_json) = 'array'
    AND jsonb_array_length(rows_json) = row_count
  ),
  CONSTRAINT ck_tuge_model_snapshot_max_sequence CHECK (
    max_sequence IS NULL OR max_sequence >= 0
  )
);

CREATE INDEX idx_tuge_model_snapshot_reuse
  ON tuge_model_observability_query_snapshot (
    scope_hash,
    query_hash,
    created_at DESC
  );

CREATE INDEX idx_tuge_model_snapshot_expiry
  ON tuge_model_observability_query_snapshot (expires_at);
