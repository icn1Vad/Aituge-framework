ALTER TABLE proof_policy
  ADD COLUMN tenant_id text;

ALTER TABLE proof_document
  ADD COLUMN tenant_id text;

ALTER TABLE proof_ingestion_run
  ADD COLUMN tenant_id text;

UPDATE proof_policy SET tenant_id = '1' WHERE tenant_id IS NULL;
UPDATE proof_document SET tenant_id = '1' WHERE tenant_id IS NULL;
UPDATE proof_ingestion_run SET tenant_id = '1' WHERE tenant_id IS NULL;

ALTER TABLE proof_policy
  ALTER COLUMN tenant_id SET NOT NULL,
  ADD CONSTRAINT proof_policy_tenant_id_check
    CHECK (length(btrim(tenant_id)) BETWEEN 1 AND 64);
ALTER TABLE proof_document
  ALTER COLUMN tenant_id SET NOT NULL,
  ADD CONSTRAINT proof_document_tenant_id_check
    CHECK (length(btrim(tenant_id)) BETWEEN 1 AND 64);
ALTER TABLE proof_ingestion_run
  ALTER COLUMN tenant_id SET NOT NULL,
  ADD CONSTRAINT proof_ingestion_run_tenant_id_check
    CHECK (length(btrim(tenant_id)) BETWEEN 1 AND 64);

ALTER TABLE proof_document DROP CONSTRAINT proof_document_content_hash_key;
ALTER TABLE proof_document
  ADD CONSTRAINT proof_document_tenant_content_hash_key UNIQUE (tenant_id, content_hash);

CREATE INDEX idx_proof_policy_tenant_status
  ON proof_policy(tenant_id, status, updated_at DESC);
CREATE INDEX idx_proof_policy_tenant_category
  ON proof_policy(tenant_id, category_code, status);
CREATE INDEX idx_proof_document_tenant_policy
  ON proof_document(tenant_id, policy_id);
CREATE INDEX idx_proof_ingestion_run_tenant_started
  ON proof_ingestion_run(tenant_id, started_at DESC);

CREATE OR REPLACE FUNCTION proof_tenant_owns_document(target_document_id text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM proof_document d
    WHERE d.id = target_document_id
      AND d.tenant_id = current_setting('proof.tenant_id', true)
  );
$$;

CREATE OR REPLACE VIEW proof_tenant_audit_run_v AS
SELECT ar.*
FROM proof_audit_run ar
WHERE proof_tenant_owns_document(ar.document_id);

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
JOIN proof_document d ON d.policy_id = p.id AND d.tenant_id = p.tenant_id
LEFT JOIN proof_policy_level l ON l.code = p.level_code
JOIN proof_category c ON c.code = p.category_code
LEFT JOIN proof_retrieval_unit u ON u.policy_id = p.id
WHERE p.status = 'effective'
  AND p.tenant_id = current_setting('proof.tenant_id', true)
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
JOIN proof_document d ON d.id = u.document_id AND d.tenant_id = p.tenant_id
LEFT JOIN proof_policy_level l ON l.code = p.level_code
JOIN proof_category c ON c.code = p.category_code
WHERE p.status = 'effective'
  AND p.tenant_id = current_setting('proof.tenant_id', true);
