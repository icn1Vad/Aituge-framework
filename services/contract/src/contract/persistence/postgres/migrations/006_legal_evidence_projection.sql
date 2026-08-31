CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_jieba;

CREATE TABLE legal_evidence_release (
  release_id text PRIMARY KEY,
  source_release_id text NOT NULL,
  source_manifest_sha256 char(64) NOT NULL,
  projection_version text NOT NULL,
  embedding_profile_id text NULL,
  status text NOT NULL CHECK (status IN ('STAGED', 'ACTIVE', 'RETIRED')),
  projection_status text NOT NULL DEFAULT 'BUILDING' CHECK (
    projection_status IN ('BUILDING', 'READY', 'FAILED')
  ),
  projected_unit_count bigint NOT NULL DEFAULT 0,
  projected_relation_count bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  projection_completed_at timestamptz NULL,
  activated_at timestamptz NULL
);

CREATE UNIQUE INDEX uq_legal_evidence_active_release
  ON legal_evidence_release ((status)) WHERE status = 'ACTIVE';

CREATE TABLE legal_evidence_unit (
  release_id text NOT NULL REFERENCES legal_evidence_release(release_id) ON DELETE CASCADE,
  unit_id text NOT NULL,
  instrument_id text NOT NULL,
  version_id text NOT NULL,
  source_node_ids text[] NOT NULL,
  title text NOT NULL,
  article_no text NULL,
  heading_path text[] NOT NULL DEFAULT '{}',
  content text NOT NULL,
  jurisdiction text NULL,
  authority_level text NULL,
  issuing_authority text NULL,
  effective_from date NULL,
  effective_to date NULL,
  validity_status text NULL CHECK (
    validity_status IS NULL OR validity_status IN (
      'ACTIVE', 'NOT_YET_EFFECTIVE', 'EXPIRED', 'REPEALED', 'UNKNOWN'
    )
  ),
  metadata_verification_status text NOT NULL DEFAULT 'UNVERIFIED' CHECK (
    metadata_verification_status IN ('VERIFIED', 'UNVERIFIED', 'REJECTED')
  ),
  official_source_url text NULL,
  content_hash char(64) NOT NULL,
  sequence integer NOT NULL,
  search_vector tsvector NOT NULL DEFAULT ''::tsvector,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (release_id, unit_id),
  UNIQUE (release_id, version_id, sequence),
  CHECK (cardinality(source_node_ids) > 0),
  CHECK (effective_from IS NULL OR effective_to IS NULL OR effective_from <= effective_to)
);

CREATE INDEX idx_legal_evidence_unit_source
  ON legal_evidence_unit(release_id, instrument_id, version_id, sequence);
CREATE INDEX idx_legal_evidence_unit_applicability
  ON legal_evidence_unit(release_id, jurisdiction, validity_status, effective_from, effective_to);
CREATE INDEX idx_legal_evidence_unit_search
  ON legal_evidence_unit USING gin (search_vector);

CREATE FUNCTION legal_evidence_refresh_search_vector() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.search_vector := to_tsvector(
    'jiebacfg',
    concat_ws(' ', NEW.title, NEW.article_no, array_to_string(NEW.heading_path, ' '), NEW.content)
  );
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_legal_evidence_search_vector
BEFORE INSERT OR UPDATE OF title, article_no, heading_path, content
ON legal_evidence_unit
FOR EACH ROW EXECUTE FUNCTION legal_evidence_refresh_search_vector();

CREATE TABLE legal_evidence_embedding (
  release_id text NOT NULL,
  unit_id text NOT NULL,
  embedding_profile_id text NOT NULL,
  provider text NOT NULL,
  model text NOT NULL,
  dimensions integer NOT NULL CHECK (dimensions = 1024),
  embedding vector(1024) NOT NULL,
  content_hash char(64) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (release_id, unit_id, embedding_profile_id),
  FOREIGN KEY (release_id, unit_id)
    REFERENCES legal_evidence_unit(release_id, unit_id) ON DELETE CASCADE
);

CREATE INDEX idx_legal_evidence_embedding_profile
  ON legal_evidence_embedding(release_id, embedding_profile_id);
CREATE INDEX idx_legal_evidence_embedding_hnsw
  ON legal_evidence_embedding USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 128);

CREATE TABLE legal_evidence_relation (
  release_id text NOT NULL REFERENCES legal_evidence_release(release_id) ON DELETE CASCADE,
  relation_id text NOT NULL,
  source_unit_id text NOT NULL,
  target_unit_id text NOT NULL,
  relation_type text NOT NULL CHECK (relation_type IN (
    'CITES', 'BASED_ON', 'IMPLEMENTS', 'INTERPRETS', 'AMENDS',
    'REPEALS', 'REPLACES', 'SUPPLEMENTS', 'EXCEPTION_TO', 'INTERNAL_REF'
  )),
  evidence_text text NOT NULL DEFAULT '',
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  verification_status text NOT NULL CHECK (
    verification_status IN ('VERIFIED', 'AUTO_VERIFIED', 'CANDIDATE')
  ),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (release_id, relation_id),
  FOREIGN KEY (release_id, source_unit_id)
    REFERENCES legal_evidence_unit(release_id, unit_id) ON DELETE CASCADE,
  FOREIGN KEY (release_id, target_unit_id)
    REFERENCES legal_evidence_unit(release_id, unit_id) ON DELETE CASCADE,
  CHECK (source_unit_id <> target_unit_id)
);

CREATE INDEX idx_legal_evidence_relation_source
  ON legal_evidence_relation(release_id, source_unit_id, relation_type);
CREATE INDEX idx_legal_evidence_relation_target
  ON legal_evidence_relation(release_id, target_unit_id, relation_type);

CREATE TABLE legal_evidence_projection_checkpoint (
  source_release_id text PRIMARY KEY,
  projection_release_id text NOT NULL,
  last_version_id text NULL,
  last_sequence integer NULL,
  projected_unit_count bigint NOT NULL DEFAULT 0,
  projected_relation_count bigint NOT NULL DEFAULT 0,
  status text NOT NULL CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
  error_code text NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE legal_evidence_plan_snapshot (
  review_id text NOT NULL,
  generation_id text NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),
  release_id text NULL REFERENCES legal_evidence_release(release_id),
  request_hash char(71) NOT NULL CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
  bundle_hash char(71) NOT NULL CHECK (bundle_hash ~ '^sha256:[0-9a-f]{64}$'),
  bundle_json jsonb NOT NULL,
  status text NOT NULL CHECK (status IN (
    'READY', 'DEGRADED', 'NO_ACTIVE_RELEASE', 'NO_RELEVANT_EVIDENCE',
    'PLANNER_FAILED'
  )),
  degraded boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (review_id, generation_id, attempt_no)
);

CREATE INDEX idx_legal_evidence_snapshot_release
  ON legal_evidence_plan_snapshot(release_id, created_at);

CREATE FUNCTION legal_evidence_guard_release_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND OLD.status IN ('ACTIVE', 'RETIRED') THEN
    RAISE EXCEPTION 'published legal evidence releases are immutable';
  END IF;
  IF TG_OP = 'UPDATE' AND (
    OLD.release_id IS DISTINCT FROM NEW.release_id OR
    OLD.source_release_id IS DISTINCT FROM NEW.source_release_id OR
    OLD.source_manifest_sha256 IS DISTINCT FROM NEW.source_manifest_sha256 OR
    OLD.projection_version IS DISTINCT FROM NEW.projection_version OR
    OLD.embedding_profile_id IS DISTINCT FROM NEW.embedding_profile_id OR
    OLD.created_at IS DISTINCT FROM NEW.created_at
  ) THEN
    RAISE EXCEPTION 'legal evidence release identity is immutable';
  END IF;
  IF TG_OP = 'UPDATE'
     AND OLD.status IS DISTINCT FROM NEW.status
     AND NOT (
       (OLD.status = 'STAGED' AND NEW.status = 'ACTIVE') OR
       (OLD.status = 'ACTIVE' AND NEW.status = 'RETIRED') OR
       (OLD.status = 'RETIRED' AND NEW.status = 'ACTIVE')
     ) THEN
    RAISE EXCEPTION 'invalid legal evidence release state transition: % -> %',
      OLD.status, NEW.status;
  END IF;
  IF TG_OP = 'UPDATE'
     AND NEW.status = 'ACTIVE'
     AND NEW.projection_status <> 'READY' THEN
    RAISE EXCEPTION 'only a READY legal evidence projection can be activated';
  END IF;
  IF TG_OP = 'UPDATE'
     AND OLD.status IN ('ACTIVE', 'RETIRED')
     AND (
       OLD.projection_status IS DISTINCT FROM NEW.projection_status OR
       OLD.projected_unit_count IS DISTINCT FROM NEW.projected_unit_count OR
       OLD.projected_relation_count IS DISTINCT FROM NEW.projected_relation_count OR
       OLD.projection_completed_at IS DISTINCT FROM NEW.projection_completed_at
     ) THEN
    RAISE EXCEPTION 'published legal evidence projection metadata is immutable';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER trg_legal_evidence_release_identity
BEFORE UPDATE OR DELETE ON legal_evidence_release
FOR EACH ROW EXECUTE FUNCTION legal_evidence_guard_release_identity();

CREATE FUNCTION legal_evidence_guard_published_children() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  old_release_status text;
  new_release_status text;
  old_projection_status text;
  new_projection_status text;
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    SELECT status, projection_status
    INTO old_release_status, old_projection_status
    FROM legal_evidence_release
    WHERE release_id = OLD.release_id;
  END IF;
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    SELECT status, projection_status
    INTO new_release_status, new_projection_status
    FROM legal_evidence_release
    WHERE release_id = NEW.release_id;
  END IF;
  IF old_release_status IN ('ACTIVE', 'RETIRED')
     OR new_release_status IN ('ACTIVE', 'RETIRED')
     OR old_projection_status = 'READY'
     OR new_projection_status = 'READY' THEN
    RAISE EXCEPTION 'sealed or published legal evidence projection rows are immutable';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER trg_legal_evidence_unit_immutable
BEFORE INSERT OR UPDATE OR DELETE ON legal_evidence_unit
FOR EACH ROW EXECUTE FUNCTION legal_evidence_guard_published_children();

CREATE TRIGGER trg_legal_evidence_embedding_immutable
BEFORE INSERT OR UPDATE OR DELETE ON legal_evidence_embedding
FOR EACH ROW EXECUTE FUNCTION legal_evidence_guard_published_children();

CREATE TRIGGER trg_legal_evidence_relation_immutable
BEFORE INSERT OR UPDATE OR DELETE ON legal_evidence_relation
FOR EACH ROW EXECUTE FUNCTION legal_evidence_guard_published_children();
