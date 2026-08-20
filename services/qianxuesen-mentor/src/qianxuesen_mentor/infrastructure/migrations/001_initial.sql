CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_jieba;

CREATE TABLE IF NOT EXISTS qxs_document (
  id text PRIMARY KEY,
  title text NOT NULL,
  original_name text NOT NULL UNIQUE,
  author text,
  publication_year integer,
  category text NOT NULL,
  source_kind text NOT NULL,
  page_count integer NOT NULL CHECK (page_count > 0),
  storage_path text NOT NULL,
  file_size bigint,
  content_sha256 text,
  document_status text NOT NULL DEFAULT 'cataloged',
  ocr_status text NOT NULL DEFAULT 'pending',
  index_status text NOT NULL DEFAULT 'pending',
  parser_version text NOT NULL DEFAULT 'qxs-pdf-v1',
  chunker_version text NOT NULL DEFAULT 'qxs-parent-child-v1',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS qxs_page (
  document_id text NOT NULL REFERENCES qxs_document(id) ON DELETE CASCADE,
  page_no integer NOT NULL CHECK (page_no > 0),
  native_text text NOT NULL DEFAULT '',
  ocr_text text NOT NULL DEFAULT '',
  effective_text text NOT NULL DEFAULT '',
  extraction_method text NOT NULL DEFAULT 'pending',
  processing_status text NOT NULL DEFAULT 'pending',
  quality_score double precision NOT NULL DEFAULT 0,
  layout_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_code text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (document_id, page_no)
);

CREATE TABLE IF NOT EXISTS qxs_book_chunk (
  id text PRIMARY KEY,
  document_id text NOT NULL REFERENCES qxs_document(id) ON DELETE CASCADE,
  ordinal integer NOT NULL,
  chapter text NOT NULL DEFAULT '',
  heading_path jsonb NOT NULL DEFAULT '[]'::jsonb,
  page_start integer NOT NULL,
  page_end integer NOT NULL,
  content text NOT NULL,
  parent_content text NOT NULL DEFAULT '',
  text_hash text NOT NULL,
  embedding_status text NOT NULL DEFAULT 'pending',
  search_vector tsvector GENERATED ALWAYS AS (
    to_tsvector('jiebacfg', coalesce(chapter, '') || ' ' || coalesce(content, ''))
  ) STORED,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, ordinal)
);

CREATE TABLE IF NOT EXISTS qxs_book_embedding (
  chunk_id text PRIMARY KEY REFERENCES qxs_book_chunk(id) ON DELETE CASCADE,
  profile_id text NOT NULL,
  model text NOT NULL,
  dimensions integer NOT NULL,
  embedding vector NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS qxs_fact (
  id text PRIMARY KEY,
  subject text NOT NULL DEFAULT '钱学森',
  predicate text NOT NULL,
  value text NOT NULL,
  event_date date,
  event_year integer,
  document_id text NOT NULL REFERENCES qxs_document(id) ON DELETE CASCADE,
  chunk_id text NOT NULL REFERENCES qxs_book_chunk(id) ON DELETE CASCADE,
  page_start integer NOT NULL,
  page_end integer NOT NULL,
  source_count integer NOT NULL DEFAULT 1,
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  card_status text NOT NULL DEFAULT 'auto_published',
  generation_version text NOT NULL DEFAULT 'fact-rules-v1',
  search_vector tsvector GENERATED ALWAYS AS (
    to_tsvector('jiebacfg', coalesce(subject, '') || ' ' || coalesce(predicate, '') || ' ' || coalesce(value, ''))
  ) STORED,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS qxs_principle (
  id text PRIMARY KEY,
  title text NOT NULL,
  summary text NOT NULL,
  application text NOT NULL DEFAULT '',
  constraints text NOT NULL DEFAULT '',
  document_id text NOT NULL REFERENCES qxs_document(id) ON DELETE CASCADE,
  chunk_id text NOT NULL REFERENCES qxs_book_chunk(id) ON DELETE CASCADE,
  page_start integer NOT NULL,
  page_end integer NOT NULL,
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  card_status text NOT NULL DEFAULT 'auto_published',
  generation_version text NOT NULL DEFAULT 'principle-rules-v1',
  search_vector tsvector GENERATED ALWAYS AS (
    to_tsvector('jiebacfg', coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(application, ''))
  ) STORED,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS qxs_card_embedding (
  evidence_type text NOT NULL CHECK (evidence_type IN ('fact', 'principle')),
  card_id text NOT NULL,
  profile_id text NOT NULL,
  model text NOT NULL,
  dimensions integer NOT NULL,
  embedding vector NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (evidence_type, card_id)
);

CREATE TABLE IF NOT EXISTS qxs_ingestion_run (
  id text PRIMARY KEY,
  document_id text REFERENCES qxs_document(id) ON DELETE SET NULL,
  status text NOT NULL,
  current_page integer NOT NULL DEFAULT 0,
  total_pages integer NOT NULL DEFAULT 0,
  processed_pages integer NOT NULL DEFAULT 0,
  ocr_pages integer NOT NULL DEFAULT 0,
  failed_pages integer NOT NULL DEFAULT 0,
  error_summary text,
  started_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_qxs_document_category ON qxs_document(category);
CREATE INDEX IF NOT EXISTS idx_qxs_page_status ON qxs_page(processing_status);
CREATE INDEX IF NOT EXISTS idx_qxs_chunk_document ON qxs_book_chunk(document_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_qxs_chunk_search ON qxs_book_chunk USING gin(search_vector);
CREATE INDEX IF NOT EXISTS idx_qxs_fact_search ON qxs_fact USING gin(search_vector);
CREATE INDEX IF NOT EXISTS idx_qxs_principle_search ON qxs_principle USING gin(search_vector);
CREATE INDEX IF NOT EXISTS idx_qxs_fact_year ON qxs_fact(event_year);

CREATE OR REPLACE VIEW qxs_sql_document_v AS
SELECT d.id AS document_id, d.title, d.author, d.publication_year, d.category,
       d.source_kind, d.page_count, d.document_status, d.ocr_status, d.index_status,
       count(DISTINCT p.page_no)::integer AS processed_page_count,
       count(DISTINCT c.id)::integer AS chunk_count, d.updated_at
FROM qxs_document d
LEFT JOIN qxs_page p ON p.document_id = d.id AND p.processing_status IN ('extracted', 'ocr_success', 'needs_ocr')
LEFT JOIN qxs_book_chunk c ON c.document_id = d.id
GROUP BY d.id;

CREATE OR REPLACE VIEW qxs_sql_fact_v AS
SELECT f.id AS fact_id, f.subject, f.predicate, f.value, f.event_date, f.event_year,
       f.confidence, f.card_status, d.title AS document_title, f.page_start, f.page_end
FROM qxs_fact f JOIN qxs_document d ON d.id = f.document_id;

CREATE OR REPLACE VIEW qxs_sql_principle_v AS
SELECT p.id AS principle_id, p.title, p.summary, p.application, p.constraints,
       p.confidence, p.card_status, d.title AS document_title, p.page_start, p.page_end
FROM qxs_principle p JOIN qxs_document d ON d.id = p.document_id;
