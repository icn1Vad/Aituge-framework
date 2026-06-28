---
name: report-generator
description: Generate a complete multi-section report without stopping after one section.
version: 0.1.0
tags: [report, writing, task]
---

# Report Generator Skill

Use this as the primary skill when the task asks for a report, research memo,
analysis brief, project review, or any answer that should contain multiple
sections.

Include the marker `skill-active: report-generator` in the response when this
skill is active.

## Completion Contract

The report is not complete until every required section has been written.

Do not stop after an outline, a plan, or the first section. Do not say that the
next section will be written later. If the available information is incomplete,
write the section anyway using clearly labeled assumptions, gaps, or "to verify"
items.

Before finalizing, check that all required sections are present. If any section
is missing, continue writing that section before giving the final answer.

## Default Report Sections

Write these sections in order unless the task gives a different structure:

1. Executive Summary
2. Context and Scope
3. Analysis and Findings
4. Risks, Open Questions, and Next Actions

## Auxiliary Skill Use

If auxiliary skills are available and the `ReadSkill` tool is available, use
`ReadSkill` before applying a relevant auxiliary skill. Do not treat the
auxiliary skill index as the full instruction.

For the default report structure, each section must be written from its matching
auxiliary skill:

1. Before writing Executive Summary, call `ReadSkill` for
   `report-executive-summary`.
2. Before writing Context and Scope, call `ReadSkill` for
   `report-context-scope`.
3. Before writing Analysis and Findings, call `ReadSkill` for
   `report-analysis-findings`.
4. Before writing Risks, Open Questions, and Next Actions, call `ReadSkill` for
   `report-risk-actions`.

Do not write a section from this primary skill alone when its matching
auxiliary skill is available.

Call `ReadSkill` for one auxiliary skill at a time. Do not issue multiple
`ReadSkill` calls in parallel.

For reports involving numbers, calculations, charts, executable checks, or code
artifacts:

- Read `report-quantitative-calculation` before doing metric calculations.
- Read `report-chart-figure` before generating charts or figures.
- Read `report-code-verification` before using code to verify a claim.
- After reading the relevant auxiliary skill, call the available code tool when
  the section requires calculation, chart generation, or verification.

## Early Stop Guard

At the end, run this self-check internally:

- Did I write all required sections?
- Did I avoid promising future continuation?
- Did I turn missing data into assumptions, caveats, or next actions?
- Is the report usable as a complete deliverable now?

If the answer to any item is no, continue writing before finalizing.
