WITH ranked_effective AS (
  SELECT id,
         row_number() OVER (
           PARTITION BY tenant_id, normalized_title
           ORDER BY version_seq DESC, updated_at DESC, id DESC
         ) AS priority
  FROM proof_policy
  WHERE status = 'effective'
    AND normalized_title <> ''
)
UPDATE proof_policy policy
SET status = 'expired',
    updated_at = now()
FROM ranked_effective ranked
WHERE policy.id = ranked.id
  AND ranked.priority > 1;

CREATE UNIQUE INDEX proof_policy_one_effective_normalized_title
  ON proof_policy(tenant_id, normalized_title)
  WHERE status = 'effective'
    AND normalized_title <> '';
