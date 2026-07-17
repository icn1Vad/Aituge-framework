CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE proof_policy_level (
  code text PRIMARY KEY,
  name text NOT NULL,
  sort_rank integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO proof_policy_level (code, name, sort_rank) VALUES
  ('upper', '上级制度', 300),
  ('peer', '本级/同级制度', 200),
  ('lower', '下级制度', 100)
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  sort_rank = EXCLUDED.sort_rank;

CREATE TABLE proof_category (
  code text PRIMARY KEY,
  name text NOT NULL,
  description text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO proof_category (code, name, description) VALUES
  ('governance', '公司治理', '章程、议事规则和公司治理制度'),
  ('finance', '资金、投资与交易', '资金、投资、担保、募集资金和交易制度'),
  ('general', '综合及其他', '尚未归入其他类别的制度')
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description;

CREATE TABLE proof_policy (
  id text PRIMARY KEY,
  title text NOT NULL,
  version text NOT NULL DEFAULT '1.0',
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'effective', 'expired')),
  level_code text REFERENCES proof_policy_level(code),
  category_code text NOT NULL DEFAULT 'general' REFERENCES proof_category(code),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_proof_policy_level ON proof_policy(level_code);
CREATE INDEX idx_proof_policy_category ON proof_policy(category_code);
CREATE INDEX idx_proof_policy_status ON proof_policy(status);

CREATE TABLE proof_document (
  id text PRIMARY KEY,
  policy_id text NOT NULL REFERENCES proof_policy(id) ON DELETE CASCADE,
  content_hash text NOT NULL UNIQUE,
  original_name text NOT NULL,
  file_type text NOT NULL,
  storage_path text NOT NULL,
  status text NOT NULL DEFAULT 'pending_embedding'
    CHECK (status IN ('pending_embedding', 'indexed', 'embedding_failed')),
  parser_version text NOT NULL,
  chunker_version text NOT NULL,
  parse_warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  embedding_error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_proof_document_policy ON proof_document(policy_id);
CREATE INDEX idx_proof_document_status ON proof_document(status);

CREATE TABLE proof_document_block (
  id text PRIMARY KEY,
  document_id text NOT NULL REFERENCES proof_document(id) ON DELETE CASCADE,
  ordinal integer NOT NULL,
  block_type text NOT NULL,
  text text NOT NULL,
  page_no integer,
  paragraph_index integer,
  heading_path jsonb NOT NULL DEFAULT '[]'::jsonb,
  char_start integer,
  char_end integer,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(document_id, ordinal)
);

CREATE INDEX idx_proof_block_document ON proof_document_block(document_id, ordinal);

CREATE TABLE proof_retrieval_unit (
  id text PRIMARY KEY,
  document_id text NOT NULL REFERENCES proof_document(id) ON DELETE CASCADE,
  policy_id text NOT NULL REFERENCES proof_policy(id) ON DELETE CASCADE,
  clause_no_raw text NOT NULL,
  clause_ordinal integer NOT NULL,
  text text NOT NULL,
  heading_path jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_block_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  page_start integer,
  page_end integer,
  paragraph_start integer,
  paragraph_end integer,
  char_start integer,
  char_end integer,
  text_hash text NOT NULL,
  embedding_status text NOT NULL DEFAULT 'pending'
    CHECK (embedding_status IN ('pending', 'indexed', 'failed', 'embedding_too_long')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(document_id, clause_ordinal)
);

CREATE INDEX idx_proof_unit_document ON proof_retrieval_unit(document_id, clause_ordinal);
CREATE INDEX idx_proof_unit_policy ON proof_retrieval_unit(policy_id);
CREATE INDEX idx_proof_unit_embedding_status ON proof_retrieval_unit(embedding_status);

CREATE TABLE proof_retrieval_embedding (
  retrieval_unit_id text PRIMARY KEY REFERENCES proof_retrieval_unit(id) ON DELETE CASCADE,
  profile_id text NOT NULL,
  provider text NOT NULL,
  model text NOT NULL,
  dimensions integer NOT NULL CHECK (dimensions > 0),
  embedding vector NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_proof_embedding_profile ON proof_retrieval_embedding(profile_id);
