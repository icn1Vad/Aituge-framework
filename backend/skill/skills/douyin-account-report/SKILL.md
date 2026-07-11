---
name: douyin-account-report
description: Generate a fact-grounded Douyin account operations report from all available account data or an explicit period.
version: 0.1.0
tags: [douyin, analytics, report, task-manager]
---

# Douyin Account Report Skill

Use this skill when TaskManager asks for a Douyin account operations report.
The default scope is all available data. Do not narrow the analysis to one
month unless `analysis_scope` is `month`, and do not narrow it to a custom
period unless `analysis_scope` is `custom_range`.

## Grounding Rules

Only analyze fields present in the task input. Do not invent:

- Fan count, new fans, play count, likes, comments, shares, collects, or rates.
- Audience age, gender, region, interests, traffic source, or active time.
- Comment text, sentiment, keywords, user questions, or typical comments.
- Benchmark comparisons when benchmark data is absent.
- Month-over-month comparisons when previous-period data is absent.
- Script quality, hook quality, narrative structure, or retention causes when
  full script text and retention evidence are absent.

If useful data is missing, write the limitation clearly and still produce a
partial report from the available evidence.

## Report Depth

For `report_depth=deep`, write a complete report with enough substance for a
human operator to use directly. Expand each section with concrete findings,
evidence, limitations, and next actions. Avoid one-sentence sections.

For `report_depth=standard`, keep sections shorter but still include evidence
and next actions.

## Tool Use

For the first TaskManager version, do not call any tool. Do not call the code
tool. Do not call `ReadSkill`. Use the provided `metrics_summary`,
`content_items`, `top_contents`, and `low_contents` directly.

If a simple rate is already provided, copy it. If a rate is not provided and the
calculation is not essential, describe the evidence qualitatively instead of
running code.

## Required Output

Return exactly one JSON object. Do not wrap it in Markdown.

The JSON object must contain:

- `status`: `success`, `partial_data`, or `insufficient_data`.
- `account_id`, `account_name`, `platform`, `analysis_scope`.
- `covered_period`: the data range or an explanation such as all available
  account data.
- `warnings` and `missing_fields`.
- `executive_summary`: answer-first account conclusion.
- `key_metrics`: the most important numeric fields copied or derived from the
  input.
- `sections`: an object with these section keys when evidence exists:
  - `overall_performance`
  - `growth_and_traffic`
  - `content_performance`
  - `interaction_and_comments`
  - `publishing_rhythm`
  - `risks_and_limitations`
- `top_content_analysis`: list of high-performing content observations.
- `low_content_analysis`: list of low-performing content observations.
- `data_limitations`: missing or weak data that affects confidence.
- `next_month_actions`: practical actions for the next operating cycle.
- `export_markdown`: set this to an empty string in the first version. Markdown
  export should be rendered by backend code from structured fields later.

Each section value should contain:

- `title`
- `summary`
- `findings`
- `evidence`
- `limitations`
- `next_actions`

## Writing Standard

Write in Chinese unless the task input explicitly requests another language.
Make the report longer than a short summary, but keep every claim grounded in
the provided data. Use phrases like "当前数据只能说明" or "暂未获取到" when
evidence is partial.

Keep JSON stable. Do not put long multi-line Markdown, code fences, triple
quotes, or unescaped double quotes inside string values.

Keep the complete JSON under 6000 Chinese characters. `top_content_analysis`
must contain at most 3 items, and `low_content_analysis` must contain at most 2
items. Each top or low content item should only include concise fields such as
`id`, `title`, `play_count`, `reason`, and `recommended_action`.
