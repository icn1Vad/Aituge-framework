CREATE TABLE proof_ingestion_run (
  id text PRIMARY KEY,
  content_hash text NOT NULL,
  original_name text NOT NULL,
  policy_id text REFERENCES proof_policy(id) ON DELETE SET NULL,
  document_id text REFERENCES proof_document(id) ON DELETE SET NULL,
  parser_version text NOT NULL,
  clause_profile text NOT NULL,
  status text NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'succeeded', 'failed')),
  stage text NOT NULL DEFAULT 'deduplicate'
    CHECK (stage IN ('deduplicate', 'parse', 'split', 'store', 'persist', 'complete')),
  reused boolean NOT NULL DEFAULT false,
  block_count integer NOT NULL DEFAULT 0 CHECK (block_count >= 0),
  clause_count integer NOT NULL DEFAULT 0 CHECK (clause_count >= 0),
  warning_count integer NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
  error_code text,
  error_message text,
  error_details jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE INDEX idx_proof_ingestion_run_status ON proof_ingestion_run(status, started_at DESC);
CREATE INDEX idx_proof_ingestion_run_content_hash ON proof_ingestion_run(content_hash, started_at DESC);
