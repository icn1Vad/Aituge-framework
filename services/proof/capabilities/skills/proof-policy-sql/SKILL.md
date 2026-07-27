---
name: proof-policy-sql
description: Generate PostgreSQL for Proof policy counts, lists, grouping, metadata, exact filters, literal substring matching, and SQL correction. Read this skill before every proof_sql call, including the SQL stage of a mixed structured-and-evidence question.
---

# Proof Policy SQL

Generate one read-only PostgreSQL query over Proof's published semantic views, then call `proof_sql` exactly once. Apply this schema as authoritative; do not spend tool calls rediscovering documented fields or enum values.

## Query workflow

1. Translate the user's business wording with the mappings below.
2. Choose the policy-grain or clause-grain view. Never join the two published views: the clause view already contains all published policy metadata, and joining can multiply rows.
3. Combine all requested counts, rows, and aggregates into one `SELECT` or `WITH ... SELECT` statement without comments or multiple statements.
4. Select explicit columns. Add deterministic tie-breakers to every ordered list.
5. Call `proof_sql` once with the original question and generated SQL. Make one correction call only when execution itself fails.
6. Answer from returned rows. Never claim a full total from only a top-N subtotal.

Do not use exploratory SQL to inspect documented enums. Do not call `proof_sql` again merely because a valid query returned zero rows.

## Execution contract

- PostgreSQL syntax only. The statement must start with `SELECT` or `WITH`.
- Accessible relations are only `proof_sql_policy_v` and `proof_sql_clause_v`.
- The server runs a read-only transaction, enforces the statement timeout, and returns at most 100 rows subject to a response-size limit.
- A trailing semicolon is accepted and removed, but an embedded semicolon or a second statement is rejected.
- Chinese patterns such as `LIKE '%金额%'` and `ILIKE '%审批%'` are supported.
- Prefer `LIKE` for literal Chinese substrings. Use `ILIKE` when case-insensitive Latin matching is also intended.
- Avoid `SELECT *`, unbounded text-heavy results, and returning full `text` unless the user explicitly asks for it.

## Row grain and shared semantics

`proof_sql_policy_v` contains one row per effective policy document. `policy_id` and `document_id` identify the row; `policy_title` is not unique because different policies or versions may share a title.

`proof_sql_clause_v` contains one row per retrieval unit belonging to an effective policy. `retrieval_unit_id` is the row identifier. Use `policy_id` when counting policies and `retrieval_unit_id` or `COUNT(*)` when counting clause units.

Both views already filter to `policy_status = 'effective'`. Therefore:

- “当前制度”“现行制度”“已生效制度” require no extra status predicate.
- If a predicate is useful for clarity, use `policy_status = 'effective'`, never `policy_status = '已生效'`.
- Draft and expired policies are intentionally inaccessible through these views. State this limitation instead of inventing a query path.

## Business value mappings

Policy level values:

| User wording | `level_code` | `level_name` |
|---|---|---|
| 一级制度 | `upper` | `一级制度` |
| 二级制度 | `peer` | `二级制度` |
| 三级制度 | `lower` | `三级制度` |

Use `level_code` for exact filtering. For the business order 一级 → 二级 → 三级, use:

```sql
ORDER BY CASE level_code WHEN 'upper' THEN 1 WHEN 'peer' THEN 2 WHEN 'lower' THEN 3 ELSE 4 END
```

Stable status/profile values:

- `policy_status`: `effective` in the published views.
- `document_status`: `pending_embedding`, `indexed`, `embedding_failed`.
- `embedding_status`: `pending`, `indexed`, `failed`, `embedding_too_long`.
- `structure_profile`: commonly `article`, `decimal_outline`, or `mixed`.
- `unit_type`: commonly `article` or `decimal_outline`; treat future values as valid text.

Leaf category mappings:

| `category_code` | `category_name` |
|---|---|
| `procurement_supply` | 采购、招投标与供应商 |
| `contract_transaction` | 合同与关联交易 |
| `external_investment` | 对外投资管理 |
| `subsidiary_equity` | 子公司与参股企业管理 |
| `financing_guarantee` | 融资、担保与募集资金 |
| `budget_expense` | 预算与费用报销 |
| `recruitment_employment` | 招聘与劳动人事 |
| `compensation_performance` | 薪酬、绩效与奖惩 |
| `attendance_leave` | 考勤与休假 |
| `digital_it` | 信息化与IT资源 |
| `audit_control` | 内部审计与内部控制 |
| `corporate_governance` | 董事会与会议治理 |
| `asset_administration` | 资产与行政物资 |
| `other` | 其他制度 |

Prefer `category_code` for an exact known category. Categories are administratively extensible, so use `category_name` literal matching for unfamiliar user wording rather than guessing a new code.

## Complete schema

### `proof_sql_policy_v`

| Column | PostgreSQL type | Meaning |
|---|---|---|
| `policy_id` | `text` | Policy identifier |
| `policy_title` | `text` | Display title; not unique |
| `policy_version` | `text` | Version label |
| `policy_status` | `text` | Always `effective` in this view |
| `level_code` | `text` | `upper`, `peer`, or `lower` |
| `level_name` | `text` | Chinese level name |
| `category_code` | `text` | Leaf business category code |
| `category_name` | `text` | Chinese business category name |
| `document_id` | `text` | Uploaded document identifier |
| `original_name` | `text` | Original uploaded filename |
| `file_type` | `text` | Source file type |
| `document_status` | `text` | Document indexing state |
| `structure_profile` | `text` | Parser structure profile |
| `warning_count` | `integer` | Number of parse warnings |
| `clause_count` | `integer` | Retrieval-unit count for this policy document |
| `created_at` | `timestamptz` | Policy creation timestamp |
| `updated_at` | `timestamptz` | Policy update timestamp |

### `proof_sql_clause_v`

| Column | PostgreSQL type | Meaning |
|---|---|---|
| `retrieval_unit_id` | `text` | Unique clause/retrieval-unit identifier |
| `document_id` | `text` | Uploaded document identifier |
| `policy_id` | `text` | Policy identifier |
| `policy_title` | `text` | Policy display title |
| `policy_version` | `text` | Version label |
| `policy_status` | `text` | Always `effective` in this view |
| `level_code` | `text` | `upper`, `peer`, or `lower` |
| `level_name` | `text` | Chinese level name |
| `category_code` | `text` | Leaf business category code |
| `category_name` | `text` | Chinese business category name |
| `original_name` | `text` | Original uploaded filename |
| `document_status` | `text` | Document indexing state |
| `structure_profile` | `text` | Parser structure profile |
| `clause_no_raw` | `text` | Source clause number or outline label |
| `clause_ordinal` | `integer` | Stable order within one document |
| `unit_type` | `text` | Retrieval-unit structure type |
| `heading_path` | `jsonb` | Heading hierarchy as a JSON array |
| `page_start` | `integer` | First source page, nullable |
| `page_end` | `integer` | Last source page, nullable |
| `paragraph_start` | `integer` | First paragraph index, nullable |
| `paragraph_end` | `integer` | Last paragraph index, nullable |
| `text` | `text` | Complete retrieval-unit text |
| `text_hash` | `text` | Content hash |
| `embedding_status` | `text` | Embedding state |
| `citation_label` | `text` | Exact `[制度名称｜条款编号｜Chunk #序号]` label |

## Reliable query patterns

Return a list and its total in one query:

```sql
SELECT policy_id, policy_title, category_name, COUNT(*) OVER () AS total_count
FROM proof_sql_policy_v
WHERE level_code = 'upper'
ORDER BY policy_title, policy_id
```

Return top groups without confusing their subtotal with the full total:

```sql
WITH grouped AS (
  SELECT category_code, category_name, COUNT(*) AS matching_clause_count
  FROM proof_sql_clause_v
  WHERE text LIKE '%金额%'
  GROUP BY category_code, category_name
)
SELECT category_code, category_name, matching_clause_count,
       SUM(matching_clause_count) OVER () AS total_matching_clause_count
FROM grouped
ORDER BY matching_clause_count DESC, category_code
LIMIT 5
```

Return clause labels deterministically:

```sql
SELECT policy_id, retrieval_unit_id, policy_title, clause_no_raw, citation_label
FROM proof_sql_clause_v
WHERE text ILIKE '%审批%'
ORDER BY policy_title, policy_id, clause_ordinal, retrieval_unit_id
LIMIT 10
```

For mixed structured-and-evidence questions, use SQL only for the structured part and return `policy_id` values. Then let the primary skill call `proof_search` for clause meaning and original evidence. SQL substring matches are not a substitute for semantic evidence.

Count distinct policies containing an explicit phrase without joining views:

```sql
WITH matched_policies AS (
  SELECT DISTINCT policy_id, policy_title, level_name, category_name
  FROM proof_sql_clause_v
  WHERE text LIKE '%供应商准入%'
)
SELECT policy_id, policy_title, level_name, category_name,
       COUNT(*) OVER () AS total_policy_count
FROM matched_policies
ORDER BY policy_title, policy_id
```

## Result interpretation

- `row_count` is the number of rows returned after server limits, not automatically the requested business total.
- If `truncated` is true, disclose that the list is partial. Use an aggregate/window column when the full total matters.
- Distinguish “rows/documents,” `COUNT(DISTINCT policy_id)`, and `COUNT(DISTINCT policy_title)` explicitly.
- Copy `citation_label` exactly when displaying clause-list results. Do not reconstruct it.
- For purely structured answers, cite the semantic view and describe the applied filters; do not invent a Chunk citation.
- If execution fails, use the returned error to correct the query once. If the correction also fails, stop and report the structured query as unavailable.
