---
name: ai-search
description: Search-oriented chat skill for TaskManager tasks that must use configured web search tools and return structured, source-backed results.
tags: [search, web, task-manager]
---

# AI Search

Handle source-backed search requests through the tools configured for this run.
Plan freshness-first queries, inspect the returned evidence, and turn reliable
sources into concise, auditable result cards.

## Core rules

- Use web search for external, current, factual, or source-backed requests.
- Preserve the user's concrete intent and important entities in every query.
- Prefer primary, official, and authoritative sources when available.
- Inspect authority, freshness, relevance, duplication, and missing evidence.
- Never invent titles, URLs, source names, dates, quotations, or results.
- If the request is unreadable or materially ambiguous, return
  `status="needs_clarification"` with empty results.
- If search fails or produces no usable evidence, return `search_failed` or
  `partial` and explain the limitation.
- Rank results by usefulness for the stated goal, not provider order.
- Return exactly one JSON object matching the output contract.

## Working loop

1. Identify the user's concrete search goal.
2. Plan one to three executable search queries and explain each query briefly.
3. Call the configured search tool.
4. Filter duplicates, low-quality pages, stale sources, and irrelevant results.
5. Summarize confirmed evidence, weak evidence, and source-quality limitations.
6. Stop when the bounded answer is adequately supported.

## Output contract

```json
{
  "status": "success",
  "answer": "Concise source-backed answer.",
  "query_plan": {
    "user_goal": "What the user wanted to find.",
    "queries": ["search query used"],
    "query_reasons": ["Why this query was used"]
  },
  "evidence_summary": {
    "confirmed": ["Reliable finding from sources."],
    "weak_or_missing": ["Unclear, stale, or missing evidence."],
    "source_quality_notes": ["Authority and freshness notes."]
  },
  "results": [
    {
      "rank": 1,
      "title": "Source title",
      "url": "https://example.com/article",
      "source_name": "example.com",
      "content_excerpt": "Short source-backed summary.",
      "published_at": "",
      "relevance_score": 0.92,
      "recommendation": "keep",
      "reason": "Why this result supports the user goal."
    }
  ],
  "risks": ["Task-level caveats."],
  "follow_up_suggestions": []
}
```

`recommendation` must be `keep`, `maybe`, or `drop`; `relevance_score` must be
between 0 and 1.
