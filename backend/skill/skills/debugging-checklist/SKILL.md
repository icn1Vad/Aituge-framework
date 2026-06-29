---
name: debugging-checklist
description: Diagnose failures by reproducing, isolating, fixing, and rerunning.
version: 0.1.0
tags: [debugging, testing]
---

# Debugging Checklist Skill

Use this as the primary skill when a task asks to investigate a failing test,
runtime exception, or unexpected behavior.

Follow this process:

1. Reproduce the failure with the narrowest command.
2. Locate the first application frame or failing assertion.
3. Make the smallest fix that explains the observed failure.
4. Rerun the reproducer and one nearby regression check.
