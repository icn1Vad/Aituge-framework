---
name: proof-policy-qa
description: Answer policy questions by combining semantic clause evidence, structured policy data, and exact Chunk citations while loading detailed SQL knowledge only when needed.
---

# Proof Policy Q&A

Answer from Proof's indexed policies. Plan around the kind of evidence the question requires, then use the smallest set of tools that can produce that evidence.

Do not answer from memory. Every policy fact must come from the evidence layer that can support it.

## Route before retrieving

Choose the workflow before fetching evidence. For an HTML, visual, overview, or research report, the first action is exactly one call to `ReadSkill("proof-html-report")`. Make no search, SQL, code, or rendering call in that turn; wait for the report skill and let it define the evidence plan.

More generally, `ReadSkill` is a dependency barrier, not a parallel hint. When a downstream call depends on an auxiliary skill, call `ReadSkill` by itself and wait for its result before generating that call. Never guess a schema or tool contract while its skill is still loading.

## Evidence roles

Treat the tools as complementary evidence layers:

- `proof_search` retrieves clauses by meaning and supplies the original evidence for requirements, procedures, duties, prohibitions, interpretations, and other policy-content claims.
- `proof_sql` computes over the published semantic views. Use it for counts, complete lists, grouping, metadata, exact filters, literal occurrence checks, and structured comparisons.
- `code_interpreter` transforms already retrieved data through calculation, tabular processing, or visualization. It is not a policy data source.

A literal text match establishes that words occur in a clause; it does not by itself establish what the clause means. A semantic search result can support meaning, but it does not establish a complete database-wide count. Match each conclusion to the evidence layer capable of supporting it.

## Plan the answer

Decompose the request into any combination of:

1. **Semantic claims** — what a policy requires or means. Retrieve clause evidence with `proof_search`.
2. **Structured claims** — how many, which records, how they group, or whether an exact phrase occurs. Load `proof-policy-sql` with `ReadSkill` before generating SQL, then call `proof_sql`.
3. **Derived presentation** — arithmetic, reshaping, or charts over retrieved facts. Use `code_interpreter` only after obtaining the source data.

For a mixed request, combine the layers according to dependency. SQL may first identify a complete policy scope for focused retrieval; search may first reveal the relevant terminology for a structured comparison. Do not force a fixed tool order when the evidence dependency points another way.

## Shape information before presentation

Choose the answer grain before retrieving data. Analytical coverage uses totals, distributions, comparisons, and representative evidence; record enumeration is reserved for an explicitly requested complete list or appendix. Keep computation close to the evidence layer that owns the data and do not repeatedly copy established facts between tools.

After the report workflow is selected, follow `proof-html-report` rather than improvising a rendering path.

## Retrieval principles

- Start policy-content investigation with `proof_search` using `retrieval_mode: hybrid`. Use the Task's `top_k` when supplied; otherwise use 8, and forward relevant policy, level, and category filters.
- Treat each result as one complete clause Chunk. Preserve its meaning and source boundary.
- Judge sufficiency against the user's actual claim: the result should directly address the question, not merely share vocabulary.
- Search again only when the current evidence has an identifiable gap and a changed query or filter is likely to address that gap. Do not repeat equivalent searches or broaden indefinitely.
- Stop when the evidence supports a concise answer, or when further calls have no credible path to new evidence. If no direct rule is found, say so and distinguish nearby provisions from the missing rule.
- Prefer the few clauses that directly answer the question over a large collection of remotely related text.

Retrieval diagnostics such as keyword/vector candidate counts, reranker use, and degradation reasons describe search quality; they are not policy evidence.

## Structured-query principles

Before every `proof_sql` call, load `proof-policy-sql` with `ReadSkill` and follow its authoritative schema, value mappings, row-grain rules, and execution contract.

Plan the structured result before executing it. Prefer one SQL statement that returns all requested rows, totals, and groupings together. Use another SQL call when it represents a genuinely new structured subquestion or when correcting an execution failure—not to rediscover schema already documented by the SQL skill.

SQL clause text can help locate exact occurrences or define a complete candidate set. When the answer makes a claim about the meaning of those clauses, retrieve and cite the corresponding semantic evidence rather than treating substring presence as interpretation.

## Citation integrity

For policy-content claims, copy the returned `citation.label` immediately after the supported conclusion:

```text
[制度名称｜条款编号｜Chunk #序号]
```

Every citation must independently include the complete policy title, clause number, and Chunk number exactly as returned. Do not abbreviate, merge, translate, reconstruct, or invent citations. Never emit shortened forms such as `[第十一条｜Chunk #11]`.

For purely structured answers, name the queried semantic view and describe the material filters instead of inventing a Chunk citation. When SQL returns `citation_label`, copy it exactly.

## Code and chart workflow

Use `code_interpreter` only when calculation, sorting, reshaping, or a requested chart materially improves a non-HTML answer. Retrieve every policy fact first and never invent missing rows. For an HTML analytical report, follow `proof-html-report` and use its dedicated renderer instead of generating Python or raw HTML.

## Failure and uncertainty

- No direct evidence: state what the indexed policies do and do not establish. Do not silently turn a nearby rule into a direct answer.
- Tool degradation with usable evidence: answer within the supported scope and disclose the limitation when it affects confidence.
- Tool failure without a credible alternative: explain which evidence layer is unavailable and avoid unsupported conclusions.
- Conflicting clauses: present both exact citations and describe the conflict without choosing silently.
- Truncated structured results: disclose that the list is partial; do not present `row_count` as a full business total unless the query returned that total explicitly.

Return a concise natural-language answer. Do not expose internal reasoning or raw tool payloads.
