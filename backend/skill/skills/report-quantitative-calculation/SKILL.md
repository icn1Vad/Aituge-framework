---
name: report-quantitative-calculation
description: Use code to calculate report metrics, checks, and derived quantities.
version: 0.1.0
tags: [report, calculation, code]
---

# Report Quantitative Calculation Skill

Use this auxiliary skill when a report contains numbers, tables, estimates,
rates, percentages, rankings, or scenarios that should be calculated rather than
reasoned about informally.

If `LimitedLocalPythonInterpreter` is available, call it before writing the
quantitative part of the report.

## Calculation Workflow

1. Translate the user's numbers or assumptions into a small Python data
   structure.
2. Compute the required metrics with code.
3. Print the intermediate values that matter for auditability.
4. Use the computed outputs in the report.

## Useful Calculations

- Totals, subtotals, shares, ratios, and percentages.
- Growth rates, deltas, averages, medians, and rankings.
- Scenario tables with base, upside, and downside cases.
- Sensitivity checks for assumptions that materially change the conclusion.

If no code tool is available, do the arithmetic explicitly in prose and mark it
as not code-verified.
