UPDATE proof_policy_level
SET name = CASE code
  WHEN 'upper' THEN '一级制度'
  WHEN 'peer' THEN '二级制度'
  WHEN 'lower' THEN '三级制度'
  ELSE name
END
WHERE code IN ('upper', 'peer', 'lower');
