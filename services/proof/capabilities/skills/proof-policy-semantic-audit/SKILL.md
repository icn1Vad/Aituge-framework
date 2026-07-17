---
name: proof-policy-semantic-audit
description: Audit supplied policy chunks for concrete semantic ambiguity and return strict JSON findings.
---

# Proof Policy Semantic Audit

Inspect every object in `Current item.targets`. Do not audit global task metadata or any text outside
the target list.

Report only ambiguity that can materially change who acts, when a rule applies, what action is required,
or what boundary and exception applies. In particular, check:

- unclear actor or responsible party;
- undefined trigger, threshold, deadline, or prerequisite;
- vague action that cannot be translated into a concrete operation;
- unclear degree, scope, exception, approval authority, or applicable object;
- missing referent, circular wording, or internally incomplete expression that prevents a reasonable
  implementer from reaching one stable interpretation.

Do not report a finding merely because wording could be stylistically improved. Do not demand definitions
for ordinary words whose meaning is clear from the chunk. When context inside the same target resolves the
meaning, return no finding.

For each problematic target, return at most one finding. Merge all material semantic problems in that
target into one concise `problem` and one actionable `suggestion`. `quote` must be one exact, continuous
substring copied from that target's `text`. `id` must exactly equal that target's `id`.

Return exactly one JSON object and no Markdown:

```json
{
  "findings": [
    {
      "id": "retrieval-unit-id",
      "quote": "exact source substring",
      "problem": "specific ambiguity and its practical impact",
      "suggestion": "concrete revision guidance"
    }
  ]
}
```

If all targets are clear, return `{"findings": []}`.
