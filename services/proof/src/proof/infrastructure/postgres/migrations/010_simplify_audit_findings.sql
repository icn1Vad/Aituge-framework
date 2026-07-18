ALTER TABLE proof_audit_finding
  DROP COLUMN quote;

ALTER TABLE proof_conflict_audit_finding
  ADD COLUMN problem text;

UPDATE proof_conflict_audit_finding
SET problem = contradiction
WHERE problem IS NULL;

ALTER TABLE proof_conflict_audit_finding
  ALTER COLUMN problem SET NOT NULL,
  DROP COLUMN mechanism,
  DROP COLUMN matter,
  DROP COLUMN scope_overlap,
  DROP COLUMN contradiction,
  DROP COLUMN evidence,
  DROP COLUMN severity,
  DROP COLUMN confidence;
