ALTER TABLE proof_policy
  ADD COLUMN family_id text,
  ADD COLUMN version_seq integer NOT NULL DEFAULT 0,
  ADD COLUMN supersedes_policy_id text REFERENCES proof_policy(id) ON DELETE SET NULL,
  ADD COLUMN similarity_state text NOT NULL DEFAULT 'clear'
    CHECK (similarity_state IN ('clear', 'decision_required', 'new_version', 'separate')),
  ADD COLUMN similarity_report jsonb NOT NULL DEFAULT '{}'::jsonb;

UPDATE proof_policy
SET family_id = id,
    version_seq = 0,
    version = 'v1.0.0';

ALTER TABLE proof_policy
  ALTER COLUMN family_id SET NOT NULL,
  ADD CONSTRAINT proof_policy_version_format_check
    CHECK (version = 'v1.0.' || version_seq::text),
  ADD CONSTRAINT proof_policy_tenant_family_version_key
    UNIQUE (tenant_id, family_id, version_seq);

CREATE UNIQUE INDEX proof_policy_one_effective_family
  ON proof_policy(tenant_id, family_id)
  WHERE status = 'effective';

CREATE INDEX idx_proof_policy_tenant_family
  ON proof_policy(tenant_id, family_id, version_seq DESC);

ALTER TABLE proof_document
  ADD COLUMN normalized_text_hash text,
  ADD COLUMN normalized_text_length integer
    CHECK (normalized_text_length IS NULL OR normalized_text_length >= 0);

ALTER TABLE proof_retrieval_unit
  DROP CONSTRAINT proof_retrieval_unit_embedding_status_check;
ALTER TABLE proof_retrieval_unit
  ADD CONSTRAINT proof_retrieval_unit_embedding_status_check
    CHECK (embedding_status IN (
      'pending', 'indexed', 'failed', 'embedding_too_long', 'retired'
    ));
