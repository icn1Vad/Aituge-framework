---
name: report-code-verification
description: Use code to verify assumptions, examples, or reproducible snippets in a report.
version: 0.1.0
tags: [report, verification, code]
---

# Report Code Verification Skill

Use this auxiliary skill when a report includes code-like logic, formulas,
pseudocode, parsing rules, transformations, or claims that can be checked with a
small executable example.

If `LimitedLocalPythonInterpreter` is available, call it to run a minimal
verification before finalizing the relevant report section.

## Verification Workflow

1. Isolate the smallest claim that can be checked.
2. Create a minimal input example.
3. Run code that validates the expected output.
4. Report whether the check passed and what remains unverified.

Do not let verification replace the report. Use it to support the relevant
finding, then continue writing all required report sections.
