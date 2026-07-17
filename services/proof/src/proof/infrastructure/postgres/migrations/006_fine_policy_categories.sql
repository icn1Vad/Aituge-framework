INSERT INTO proof_category (code, name, description) VALUES
  ('procurement_supply', '采购、招投标与供应商', '采购、招投标和供应商准入、评价与管理制度'),
  ('contract_transaction', '合同与关联交易', '合同订立、履行以及关联交易管理制度'),
  ('external_investment', '对外投资管理', '对外投资立项、决策、实施和退出制度'),
  ('subsidiary_equity', '子公司与参股企业管理', '子公司、参股企业和股权单位管理制度'),
  ('financing_guarantee', '融资、担保与募集资金', '融资、对外担保和募集资金管理制度'),
  ('budget_expense', '预算与费用报销', '全面预算、费用控制和报销管理制度'),
  ('recruitment_employment', '招聘与劳动人事', '人员招聘、劳动关系和人事管理制度'),
  ('compensation_performance', '薪酬、绩效与奖惩', '薪酬、绩效考核和奖惩管理制度'),
  ('attendance_leave', '考勤与休假', '考勤、休假和假期管理制度'),
  ('digital_it', '信息化与IT资源', '信息化建设、信息技术和IT资源管理制度'),
  ('audit_control', '内部审计与内部控制', '内部审计、内部控制和内控评价制度'),
  ('corporate_governance', '董事会与会议治理', '董事会、议事规则和会议治理制度'),
  ('asset_administration', '资产与行政物资', '固定资产、车辆、办公用品和行政物资制度'),
  ('other', '其他制度', '尚未归入业务细分类的制度')
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description;

WITH classified AS (
  SELECT
    id,
    CASE
      WHEN title ILIKE ANY (ARRAY['%采购%', '%招标%', '%投标%', '%供应商%'])
        THEN 'procurement_supply'
      WHEN title ILIKE ANY (ARRAY['%合同%', '%关联交易%'])
        THEN 'contract_transaction'
      WHEN title ILIKE ANY (ARRAY['%对外投资%', '%投资管理%'])
        THEN 'external_investment'
      WHEN title ILIKE ANY (ARRAY['%参股企业%', '%子公司%'])
        THEN 'subsidiary_equity'
      WHEN title ILIKE ANY (ARRAY['%融资%', '%担保%', '%募集资金%'])
        THEN 'financing_guarantee'
      WHEN title ILIKE ANY (ARRAY['%预算%', '%费用报销%'])
        THEN 'budget_expense'
      WHEN title ILIKE ANY (ARRAY['%招聘%', '%劳动人事%'])
        THEN 'recruitment_employment'
      WHEN title ILIKE ANY (ARRAY['%薪酬%', '%绩效%', '%奖惩%'])
        THEN 'compensation_performance'
      WHEN title ILIKE ANY (ARRAY['%考勤%', '%休假%', '%假期%'])
        THEN 'attendance_leave'
      WHEN title ILIKE ANY (ARRAY['%信息化%', '%IT资源%', '%信息技术%'])
        THEN 'digital_it'
      WHEN title ILIKE ANY (ARRAY['%内部审计%', '%内部控制%', '%内控%'])
        THEN 'audit_control'
      WHEN title ILIKE ANY (ARRAY['%董事会%', '%会议管理%', '%议事规则%'])
        THEN 'corporate_governance'
      WHEN title ILIKE ANY (ARRAY['%固定资产%', '%车辆%', '%办公用品%', '%行政物资%'])
        THEN 'asset_administration'
      ELSE NULL
    END AS category_code
  FROM proof_policy
)
UPDATE proof_policy AS policy
SET category_code = classified.category_code,
    updated_at = now()
FROM classified
WHERE policy.id = classified.id
  AND classified.category_code IS NOT NULL
  AND policy.category_code IS DISTINCT FROM classified.category_code;

ALTER TABLE proof_policy
  ALTER COLUMN category_code SET DEFAULT 'other';

DELETE FROM proof_category AS category
WHERE category.code IN ('governance', 'finance', 'general')
  AND NOT EXISTS (
    SELECT 1
    FROM proof_policy AS policy
    WHERE policy.category_code = category.code
  );
