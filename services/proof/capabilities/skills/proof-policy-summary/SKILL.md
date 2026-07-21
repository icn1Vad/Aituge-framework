---
name: proof-policy-summary
description: Read one complete company policy and produce a clear, source-grounded preliminary analysis report covering its positioning, scope, governance roles, operating flow, core rules, controls, and exceptions.
---

# Proof preliminary policy analysis

Read every item in `summary_chunks` as one ordered policy. Reconstruct how the policy is intended to operate and
write a concise preliminary analysis for a reader who has not read the source. This stage explains the policy;
semantic ambiguity, executability defects, and cross-policy conflicts belong to separate audit stages.

## Analysis workflow

1. Read all chunks before writing. Identify the policy's management subject, intended objective, applicable scope,
   governed activities, and overall operating model.
2. Identify the principal roles or departments and summarize each role's material authority, responsibilities,
   obligations, and handoffs as one coherent description. Merge closely related duties instead of classifying every
   sentence separately as a responsibility, right, or obligation.
3. Reconstruct the main process in source order: trigger, application or initiation, review, approval, execution,
   recording or reporting, supervision, and close-out. Include only stages actually supported by the policy.
4. Summarize the rules that materially determine an outcome: thresholds, time limits, approval conditions, channels,
   required records, prohibitions, control separation, escalation rules, and consequences. Combine rules that form
   one operating requirement instead of emitting one item per clause.
5. Identify explicit exceptions, special situations, simplified paths, emergency handling, and the conditions that
   activate them.
6. Synthesize the above into `plain_summary`, then populate the remaining fields as a compact semantic outline. Do
   not reproduce the report sentence by sentence in the structured fields.

## `plain_summary` report standard

Write in Chinese unless the policy is predominantly in another language. Use short paragraphs with the following
plain-text section labels, combining adjacent sections when that produces a clearer and less repetitive overview.
Omit a section when the source provides no meaningful information:

1. `一、制度定位与总体框架` — what the policy governs, why it exists, and its overall management approach.
2. `二、适用范围与管理对象` — covered organizations, people, activities, transactions, or scenarios.
3. `三、治理结构与职责分工` — principal roles, their authority, obligations, and collaboration relationships.
4. `四、核心运行机制` — the end-to-end process or decision path in operational order.
5. `五、关键规则与控制要求` — thresholds, deadlines, approval levels, mandatory channels, prohibitions, and
   control measures.
6. `六、例外情形与执行边界` — expressly stated exceptions, special handling, and the factual boundary of what the
   policy does and does not specify.
7. `七、综合执行图景` — a concise synthesis of how an actual matter would move through the policy from trigger to
   completion.

The report must be information-dense rather than exhaustive. Keep it materially shorter than the source and never
pad it with generic management language. Preserve material numbers, conditions, role names, and rule direction, but
summarize supporting detail when it does not need to be explained separately.

## Grounding rules

- Use only statements supported by the supplied chunks.
- Never invent a responsible party, right, obligation, threshold, deadline, process step, exception, or scope.
- Do not label the policy compliant, non-compliant, correct, risky, ambiguous, or defective.
- If the policy does not state something explicitly, leave the corresponding nullable field or array empty.
- `plain_summary` may factually say that the text does not specify a scope, role, process, or exception when that
  boundary is important to understanding the policy, but must not turn the observation into an audit conclusion.
- Use the chunks as evidence while reasoning, but do not copy chunk IDs, clause IDs, citation labels, or any other
  retrieval identifiers into the output. This overview is reader-facing and intentionally contains no identifiers.
- Prefer role or department names over personal names unless the policy itself assigns a named office holder.
- Combine duplicate descriptions of the same role into one useful role summary.
- Merge related process steps and rules when their combined meaning is clearer than a fragmented list. Do not mirror
  the chunks, enumerate every clause, or split one coherent requirement into many small items.
- Return a complete JSON object. If the policy is detailed, compress secondary detail before expanding the output.

## Output contract

Return exactly one JSON object and no Markdown:

```json
{
  "plain_summary": "一、制度定位与总体框架\n...\n\n二、适用范围与管理对象\n...",
  "purpose": "The explicitly stated or directly supported purpose.",
  "scope": ["A concise summary of related applicable organizations, activities, matters, or people."],
  "concerned_roles": [{
    "role": "Role or department",
    "summary": "A combined description of the role's material authority, responsibilities, obligations, and handoffs."
  }],
  "key_rules": ["A summarized material threshold, deadline, approval condition, or control requirement."]
}
```

Use `null` for `purpose` when no purpose is supported. Use empty arrays for unsupported list sections. There is no
required item count: include what is material, merge what does not need separate explanation, and stop when the
reader has a complete operational overview.
