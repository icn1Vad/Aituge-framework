---
name: report-chart-figure
description: Use code to create report charts or figures when visuals clarify the analysis.
version: 0.1.0
tags: [report, chart, code]
---

# Report Chart Figure Skill

Use this auxiliary skill when a report would benefit from a chart, figure, or
visual comparison.

If `LimitedLocalPythonInterpreter` is available, call it to generate the chart.
Prefer simple, readable charts over decorative visuals.

## Chart Workflow

1. Choose the chart type that matches the question:
   - bar chart for category comparison;
   - line chart for trends over time;
   - scatter chart for relationships;
   - histogram for distributions.
2. Write Python code using matplotlib or another available plotting library.
3. Save the figure as `.png` or `.svg`.
4. Print a short interpretation of the figure.
5. Reference the generated figure in the report.

If chart generation is not possible, describe the intended chart and still
complete the report.
