ALTER TABLE proof_audit_run
  ADD COLUMN summary_status text NOT NULL DEFAULT 'pending'
    CHECK (summary_status IN ('pending', 'running', 'completed', 'failed')),
  ADD COLUMN summary_content jsonb,
  ADD COLUMN summary_error_message text;

UPDATE proof_audit_run
SET summary_status = 'failed',
    summary_error_message = 'Legacy audit run did not produce a policy summary.';

ALTER TABLE proof_audit_finding
  ADD COLUMN category text;

UPDATE proof_audit_finding
SET category = CASE
  WHEN problem LIKE '可执行性缺口：%' OR problem LIKE '可执行性：%'
    THEN 'executability_gap'
  ELSE 'semantic_ambiguity'
END
WHERE category IS NULL;

ALTER TABLE proof_audit_finding
  ALTER COLUMN category SET NOT NULL,
  ADD CONSTRAINT proof_audit_finding_category_check
    CHECK (category IN ('semantic_ambiguity', 'executability_gap'));
