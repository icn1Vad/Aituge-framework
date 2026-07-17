-- Existing uploads predate the review workflow and must remain visible after
-- retrieval starts enforcing policy status.
UPDATE proof_policy
SET status = 'effective', updated_at = now()
WHERE status = 'draft';

CREATE TABLE proof_audit_run (
  id text PRIMARY KEY,
  document_id text NOT NULL UNIQUE REFERENCES proof_document(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  framework_task_id text,
  framework_run_id text,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_proof_audit_run_status ON proof_audit_run(status);

CREATE TABLE proof_audit_finding (
  audit_run_id text NOT NULL REFERENCES proof_audit_run(id) ON DELETE CASCADE,
  retrieval_unit_id text NOT NULL REFERENCES proof_retrieval_unit(id) ON DELETE CASCADE,
  quote text NOT NULL,
  problem text NOT NULL,
  suggestion text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (audit_run_id, retrieval_unit_id)
);

CREATE OR REPLACE VIEW proof_sql_policy_v AS
SELECT
  p.id AS policy_id,
  p.title AS policy_title,
  p.version AS policy_version,
  p.status AS policy_status,
  p.level_code,
  l.name AS level_name,
  p.category_code,
  c.name AS category_name,
  d.id AS document_id,
  d.original_name,
  d.file_type,
  d.status AS document_status,
  d.structure_profile,
  jsonb_array_length(d.parse_warnings) AS warning_count,
  COUNT(u.id)::integer AS clause_count,
  p.created_at,
  p.updated_at
FROM proof_policy p
JOIN proof_document d ON d.policy_id = p.id
LEFT JOIN proof_policy_level l ON l.code = p.level_code
JOIN proof_category c ON c.code = p.category_code
LEFT JOIN proof_retrieval_unit u ON u.policy_id = p.id
WHERE p.status = 'effective'
GROUP BY p.id, d.id, l.name, c.name;

CREATE OR REPLACE VIEW proof_sql_clause_v AS
SELECT
  u.id AS retrieval_unit_id,
  u.document_id,
  u.policy_id,
  p.title AS policy_title,
  p.version AS policy_version,
  p.status AS policy_status,
  p.level_code,
  l.name AS level_name,
  p.category_code,
  c.name AS category_name,
  d.original_name,
  d.status AS document_status,
  d.structure_profile,
  u.clause_no_raw,
  u.clause_ordinal,
  u.unit_type,
  u.heading_path,
  u.page_start,
  u.page_end,
  u.paragraph_start,
  u.paragraph_end,
  u.text,
  u.text_hash,
  u.embedding_status,
  '[' || p.title || '｜' || COALESCE(NULLIF(u.clause_no_raw, ''), '未编号条款') ||
    '｜Chunk #' || u.clause_ordinal || ']' AS citation_label
FROM proof_retrieval_unit u
JOIN proof_policy p ON p.id = u.policy_id
JOIN proof_document d ON d.id = u.document_id
LEFT JOIN proof_policy_level l ON l.code = p.level_code
JOIN proof_category c ON c.code = p.category_code
WHERE p.status = 'effective';
