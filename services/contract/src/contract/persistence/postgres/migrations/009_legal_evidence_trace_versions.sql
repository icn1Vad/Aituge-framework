-- Trace every graph edge and freeze every projection/model component version.

ALTER TABLE legal_evidence_release
  ADD COLUMN IF NOT EXISTS relation_extractor_version text
    NOT NULL DEFAULT 'legal-relation-extractor-v1';

ALTER TABLE legal_evidence_release
  ADD COLUMN IF NOT EXISTS embedding_model_version text NULL;

ALTER TABLE legal_evidence_relation
  ADD COLUMN IF NOT EXISTS source_node_id text NULL;

ALTER TABLE legal_evidence_relation
  ADD COLUMN IF NOT EXISTS evidence_start bigint NULL;

ALTER TABLE legal_evidence_relation
  ADD COLUMN IF NOT EXISTS evidence_end bigint NULL;

ALTER TABLE legal_evidence_relation
  ADD COLUMN IF NOT EXISTS extractor_version text
    NOT NULL DEFAULT 'legal-relation-extractor-v1';

ALTER TABLE legal_evidence_relation
  ADD CONSTRAINT ck_legal_evidence_relation_span
  CHECK (
    (evidence_start IS NULL AND evidence_end IS NULL)
    OR (evidence_start >= 0 AND evidence_end >= evidence_start)
  ) NOT VALID;

ALTER TABLE legal_evidence_relation
  VALIDATE CONSTRAINT ck_legal_evidence_relation_span;

CREATE TABLE legal_evidence_relation_review_queue (
  release_id text NOT NULL REFERENCES legal_evidence_release(release_id) ON DELETE CASCADE,
  review_id text NOT NULL,
  source_unit_id text NOT NULL,
  source_node_id text NULL,
  target_title text NOT NULL,
  target_article_no text NULL,
  proposed_relation_type text NOT NULL,
  evidence_text text NOT NULL,
  evidence_start bigint NOT NULL CHECK (evidence_start >= 0),
  evidence_end bigint NOT NULL CHECK (evidence_end >= evidence_start),
  extractor_version text NOT NULL,
  review_reason text NOT NULL CHECK (
    review_reason IN ('AMBIGUOUS_TARGET', 'UNMATCHED_TARGET')
  ),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (release_id, review_id),
  FOREIGN KEY (release_id, source_unit_id)
    REFERENCES legal_evidence_unit(release_id, unit_id) ON DELETE CASCADE
);

CREATE INDEX idx_legal_evidence_relation_review_reason
  ON legal_evidence_relation_review_queue(release_id, review_reason, target_title);

CREATE OR REPLACE FUNCTION legal_evidence_guard_release_identity() RETURNS trigger
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
    OLD.relation_extractor_version IS DISTINCT FROM NEW.relation_extractor_version OR
    OLD.embedding_profile_id IS DISTINCT FROM NEW.embedding_profile_id OR
    OLD.embedding_model_version IS DISTINCT FROM NEW.embedding_model_version OR
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

CREATE TRIGGER trg_legal_evidence_relation_review_immutable
BEFORE INSERT OR UPDATE OR DELETE ON legal_evidence_relation_review_queue
FOR EACH ROW EXECUTE FUNCTION legal_evidence_guard_published_children();
