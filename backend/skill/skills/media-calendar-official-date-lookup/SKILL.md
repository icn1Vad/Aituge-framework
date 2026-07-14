---
name: media-calendar-official-date-lookup
description: Find one annual industry-calendar announcement and extract multiple requested phase dates using only allow-listed official web sources.
tags: [calendar, official-source, web-search, task-manager]
---

# Industry Calendar Official Date Lookup

You verify exactly one annual industry-calendar event and extract every requested phase from the same official announcement when possible. Use `aliyun-websearch` before answering.

## Non-negotiable rules

- Search for the supplied event and cycle year. Prefer `preferred_official_domains`, then the remaining `official_domains`.
- Search for an event-level announcement or schedule before searching individual phases. Reuse one official page for multiple phases when its text supports them.
- Keep the search bounded: run one event-level official-announcement query, then at most one refined query only if the first successful search returns no plausible official announcement.
- If those successful searches show that the current-cycle announcement is not published, return `awaiting_official` immediately.
- Accept evidence only when the final URL host equals or is a subdomain of one of `official_domains`.
- A search snippet may help locate a page, but do not confirm a date from a non-official mirror, media article, forum, training site, or generated summary.
- Return exactly one `phase_results` item for every requested phase and no unrequested phase.
- Every confirmed date must be explicitly present in `evidence_excerpt` beside wording that identifies that phase.
- The date must describe the requested phase, not the article publication date or another phase mentioned on the same page.
- Do not infer a missing start/deadline from historical patterns, a forecast, or general knowledge.
- For `official_page_check`, inspect phase `known_source_url` values first and search again only when those pages no longer give current-cycle evidence.
- If an official page gives a single-day phase, set `start_date` and `end_date` to that date.
- If evidence is official but the phase/date mapping is ambiguous, return that phase as `candidate`; if no allow-listed official evidence is found, return it as `awaiting_official`.
- Return one JSON object only. No Markdown and no prose outside JSON.

## Output contract

```json
{
  "status": "completed",
  "phase_results": [
    {
      "phase_key": "registration_start",
      "status": "confirmed",
      "found": true,
      "official": true,
      "start_date": "2026-07-01",
      "end_date": "2026-07-01",
      "deadline_at": null,
      "source_url": "https://official.example/page",
      "source_title": "Official page title",
      "source_published_at": "2026-06-30",
      "extraction_method": "official_search",
      "evidence_summary": "The official announcement explicitly states the registration start date.",
      "evidence_excerpt": "报名时间自2026年7月1日起至8月10日止。",
      "warnings": []
    }
  ],
  "source_urls": ["https://official.example/page"],
  "warnings": []
}
```

Use event status `completed` when all phases are confirmed, `partial` for a mixture, `not_found` when all phases await publication, and `search_failed` only when the web search itself fails. Never fill a date from a forecast or general knowledge.
