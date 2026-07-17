CREATE EXTENSION IF NOT EXISTS pg_jieba;

ALTER TABLE proof_retrieval_unit
  ADD COLUMN search_vector tsvector;

CREATE OR REPLACE FUNCTION proof_refresh_retrieval_search_vector()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.search_vector := to_tsvector(
    'jiebacfg',
    COALESCE(NEW.clause_no_raw, '') || ' ' ||
    COALESCE(NEW.heading_path::text, '') || ' ' ||
    COALESCE(NEW.text, '')
  );
  RETURN NEW;
END;
$$;

CREATE TRIGGER proof_retrieval_search_vector_trigger
BEFORE INSERT OR UPDATE OF clause_no_raw, heading_path, text
ON proof_retrieval_unit
FOR EACH ROW
EXECUTE FUNCTION proof_refresh_retrieval_search_vector();

UPDATE proof_retrieval_unit
SET search_vector = to_tsvector(
  'jiebacfg',
  COALESCE(clause_no_raw, '') || ' ' ||
  COALESCE(heading_path::text, '') || ' ' ||
  COALESCE(text, '')
);

ALTER TABLE proof_retrieval_unit
  ALTER COLUMN search_vector SET NOT NULL;

CREATE INDEX idx_proof_retrieval_unit_search_vector
  ON proof_retrieval_unit USING gin(search_vector);

CREATE VIEW proof_sql_policy_v AS
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
GROUP BY p.id, d.id, l.name, c.name;

CREATE VIEW proof_sql_clause_v AS
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
JOIN proof_category c ON c.code = p.category_code;
