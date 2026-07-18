---
name: proof-policy-summary
description: Read one complete company policy and produce a clear, source-grounded preliminary analysis report covering its positioning, scope, governance roles, operating flow, core rules, controls, and exceptions.
---

# Proof preliminary policy analysis

Read every item in `summary_chunks` as one ordered policy. Reconstruct how the policy is intended to operate and
write a professional preliminary analysis report for a reader who has not read the source. This stage explains the
policy; semantic ambiguity, executability defects, and cross-policy conflicts belong to separate audit stages.

## Analysis workflow

1. Read all chunks before writing. Identify the policy's management subject, intended objective, applicable scope,
   governed activities, and overall operating model.
2. Map every named role or department to its responsibilities, decision rights, obligations, prohibitions, handoffs,
   and approval authority. Distinguish an organizer, reviewer, approver, executor, supervisor, and record keeper.
3. Reconstruct the main process in source order: trigger, application or initiation, review, approval, execution,
   recording or reporting, supervision, and close-out. Include only stages actually supported by the policy.
4. Extract the rules that materially determine an outcome: thresholds, time limits, approval conditions, channels,
   required records, prohibitions, control separation, escalation rules, and consequences.
5. Identify explicit exceptions, special situations, simplified paths, emergency handling, and the conditions that
   activate them.
6. Synthesize the above into `plain_summary` as a readable preliminary analysis report, then populate the structured
   fields as its factual source index.

## `plain_summary` report standard

Write in Chinese unless the policy is predominantly in another language. Use 5 to 8 short paragraphs with these
plain-text section labels, omitting only a section for which the source provides no meaningful information:

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

The report must be information-dense rather than verbose. For a normal multi-clause policy, target roughly 800 to
2500 Chinese characters. For a very short policy, remain proportional to the source and never pad with generic
management language. Preserve material numbers, conditions, role names, and rule direction.

## Grounding rules

- Use only statements supported by the supplied chunks.
- Never invent a responsible party, right, obligation, threshold, deadline, process step, exception, or scope.
- Do not label the policy compliant, non-compliant, correct, risky, ambiguous, or defective.
- If the policy does not state something explicitly, leave the corresponding nullable field or array empty.
- `plain_summary` may factually say that the text does not specify a scope, role, process, or exception when that
  boundary is important to understanding the policy, but must not turn the observation into an audit conclusion.
- Every structured fact must cite one or more exact chunk IDs from `summary_chunks` in `source_ids`.
- `plain_summary` is the reader-facing preliminary analysis report; all of its concrete facts must also appear in
  sourced fields.
- Prefer role or department names over personal names unless the policy itself assigns a named office holder.
- Combine duplicate descriptions of the same role, but preserve distinct rights, responsibilities, and obligations.
- Cover all material operating rules while merging repetition. Do not merely paraphrase the source clause by clause.

## Output contract

Return exactly one JSON object and no Markdown:

```json
{
  "plain_summary": "一、制度定位与总体框架\n...\n\n二、适用范围与管理对象\n...",
  "purpose": {"text": "The explicitly stated or directly supported purpose.", "source_ids": ["chunk-id"]},
  "scope": [{"text": "An applicable organization, activity, matter, or person.", "source_ids": ["chunk-id"]}],
  "concerned_roles": [{
    "role": "Role or department",
    "source_ids": ["chunk-id"],
    "responsibilities": [{"text": "What the role is responsible for organizing or deciding.", "source_ids": ["chunk-id"]}],
    "rights": [{"text": "A power, approval authority, entitlement, or discretion.", "source_ids": ["chunk-id"]}],
    "obligations": [{"text": "A required action, prohibition, reporting duty, or deadline.", "source_ids": ["chunk-id"]}]
  }],
  "key_process": [{"text": "One material process step in policy order.", "source_ids": ["chunk-id"]}],
  "key_rules": [{"text": "One material threshold, deadline, approval condition, or control rule.", "source_ids": ["chunk-id"]}],
  "exceptions": [{"text": "One explicitly stated exception or special handling path.", "source_ids": ["chunk-id"]}]
}
```

Use `null` for `purpose` when no purpose is supported. Use empty arrays for all unsupported list sections.
