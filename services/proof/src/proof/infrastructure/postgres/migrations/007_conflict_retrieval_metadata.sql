ALTER TABLE proof_category
  ADD COLUMN parent_code text,
  ADD COLUMN level smallint NOT NULL DEFAULT 1;

INSERT INTO proof_category (code, name, description, level, parent_code) VALUES
  ('governance_oversight', '公司治理与监督', '公司治理、组织权责、审计与内部控制制度', 1, NULL),
  ('capital_finance', '投融资与资本', '投资、融资、担保和资本运作制度', 1, NULL),
  ('procurement_transactions', '采购与交易', '采购、供应商、合同和关联交易制度', 1, NULL),
  ('finance_assets', '财务与资产', '预算、费用、资产和行政物资制度', 1, NULL),
  ('human_resources', '人力资源', '招聘、人事、薪酬、绩效和考勤制度', 1, NULL),
  ('digital_operations', '数字化与运营', '信息化、信息技术和数字化运营制度', 1, NULL)
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description,
  level = EXCLUDED.level,
  parent_code = EXCLUDED.parent_code;

UPDATE proof_category
SET level = 2,
    parent_code = CASE code
      WHEN 'procurement_supply' THEN 'procurement_transactions'
      WHEN 'contract_transaction' THEN 'procurement_transactions'
      WHEN 'external_investment' THEN 'capital_finance'
      WHEN 'subsidiary_equity' THEN 'governance_oversight'
      WHEN 'financing_guarantee' THEN 'capital_finance'
      WHEN 'budget_expense' THEN 'finance_assets'
      WHEN 'recruitment_employment' THEN 'human_resources'
      WHEN 'compensation_performance' THEN 'human_resources'
      WHEN 'attendance_leave' THEN 'human_resources'
      WHEN 'digital_it' THEN 'digital_operations'
      WHEN 'audit_control' THEN 'governance_oversight'
      WHEN 'corporate_governance' THEN 'governance_oversight'
      WHEN 'asset_administration' THEN 'finance_assets'
    END
WHERE code IN (
  'procurement_supply',
  'contract_transaction',
  'external_investment',
  'subsidiary_equity',
  'financing_guarantee',
  'budget_expense',
  'recruitment_employment',
  'compensation_performance',
  'attendance_leave',
  'digital_it',
  'audit_control',
  'corporate_governance',
  'asset_administration'
);

ALTER TABLE proof_category
  ADD CONSTRAINT proof_category_parent_fk
    FOREIGN KEY (parent_code) REFERENCES proof_category(code) ON DELETE RESTRICT,
  ADD CONSTRAINT proof_category_level_check
    CHECK (level IN (1, 2)),
  ADD CONSTRAINT proof_category_tree_shape_check
    CHECK (
      (level = 1 AND parent_code IS NULL)
      OR (level = 2 AND parent_code IS NOT NULL)
    );

CREATE INDEX idx_proof_category_parent
  ON proof_category(parent_code, code);

ALTER TABLE proof_policy
  ADD COLUMN normalized_title text NOT NULL DEFAULT '';

CREATE INDEX idx_proof_policy_normalized_title
  ON proof_policy(normalized_title, status);
