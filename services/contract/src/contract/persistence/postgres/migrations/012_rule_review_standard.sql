ALTER TABLE contract_review_run
    ADD COLUMN IF NOT EXISTS rule_review_standard VARCHAR(16) NOT NULL DEFAULT 'neutral'
    CHECK (rule_review_standard IN ('neutral', 'strong', 'weak'));
