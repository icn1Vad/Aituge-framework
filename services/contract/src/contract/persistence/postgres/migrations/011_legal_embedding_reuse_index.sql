CREATE INDEX IF NOT EXISTS idx_legal_evidence_embedding_reuse_v1
  ON legal_evidence_embedding (
    release_id,
    embedding_profile_id,
    content_hash,
    embedding_input_hash
  );
