---
name: proof-html-report
description: Plan and render a compact evidence-grounded Proof policy analysis report as HTML without generating Python or raw HTML.
---

# Proof HTML Report

Use this skill for an HTML, visual, overview, or research report over Proof policies. The goal is a reader-facing analytical report, not a transport of raw records.

## Understand the renderer

`RenderHtmlReport` accepts a compact structured report specification and applies a fixed safe HTML template. Supply titles, summary text, metrics, short sections, bounded tables, findings, methodology, and source notes. The tool publishes the HTML artifact automatically.

Do not generate Python, CSS, JavaScript, raw HTML, or artifact URLs. Do not use `code_interpreter` to create the HTML report.

## Decide the report grain

Distinguish analytical coverage from record enumeration:

- An overall, system-wide, overview, research, or analytical report covers the full subject through totals, distributions, comparisons, outliers, and representative examples. It does not reproduce every policy row.
- A complete inventory or record-by-record appendix exists only when the user explicitly requests every policy or a complete list. `RenderHtmlReport` is not an inventory exporter; state the appendix limitation instead of forcing the complete inventory into report arguments.

## Build the evidence plan

Plan the report structure before querying. Retrieve only evidence that fills a named report element:

1. If SQL is needed, call `ReadSkill("proof-policy-sql")` alone and wait for the skill result. Only then generate SQL; never call `proof_sql` in parallel with the skill load.
2. Use SQL for report-ready totals, grouped distributions, comparisons, and limited representative rows. Prefer one aggregate query that supplies multiple metrics; add another query only for a genuinely different dimension.
3. Use semantic search only when a planned section makes a claim about what a policy means or requires. A structural overview can be fully supported by SQL and needs no semantic search. When search is necessary, retrieve only the few clauses needed for that claim.
4. Stop retrieving when every planned metric, section, and table has support. Do not query the complete policy list for an analytical report.

Keep tables interpretive and bounded. A category distribution, level comparison, exception list, or top group is useful; an 84-row inventory is not.

## Render once

After the evidence is ready, call `RenderHtmlReport` once with the smallest faithful specification:

- `title`, optional `subtitle`, and a concise `summary`;
- up to eight headline `metrics`;
- short narrative `sections`;
- tables with explicit headers and no more than twenty representative or grouped rows each;
- evidence-grounded `findings`;
- `methodology` and `source_note` that name the semantic views, filters, and material limitations.

All values must be reader-facing text. The renderer escapes content and creates the artifact; use the returned artifact reference in the final answer without rewriting its URL.

If the renderer rejects the specification, reduce or correct the structure and retry once. Do not switch to Python or repeat the same oversized content.
