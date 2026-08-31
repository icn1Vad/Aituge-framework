-- Forward-only hardening for environments that may already have applied the
-- first legal-evidence projection migration during staged validation.

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

CREATE OR REPLACE FUNCTION legal_evidence_guard_published_children() RETURNS trigger
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
