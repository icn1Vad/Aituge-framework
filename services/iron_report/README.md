# Iron Report Demo Service

Independent iron ore daily/weekly report business service mounted on the existing Aituge Framework runtime.

## Boundaries

- Java is the only frontend-facing service and owns authentication, tenant isolation, business task history, archive status, and downloads.
- This service owns prepared-data access, Framework task dispatch, ReAct analysis, dynamic charts, and DOCX/PDF generation.
- Framework core files are not modified.
- The bundled data is an immutable internal demo fixture copied from `E:\MyProjects\iron&translate`; `SHA256SUMS` is verified during startup.

## Runtime configuration

Copy `.env.example` only on the server and provide secrets through the isolated deployment environment. Never commit real credentials.

The model profile is created in the isolated Framework database from the configured model ID,
provider model name, OpenAI-compatible base URL, and API key. The optional Aliyun search provider is
created from its configured endpoint and key. Credentials are encrypted before database storage and
remain server environment variables; none are hard-coded.

The report uses the existing scheduler/ReAct Task type. Because scheduler Tasks do not invoke a result
sink automatically, the business Service idempotently starts export when status/result synchronization
observes a validated Framework result. This keeps Framework core unchanged while still registering every
chart, DOCX, and PDF as a task-owned Artifact.

## Internal API

- `POST /v1/iron-reports` returns HTTP 202.
- `GET /v1/iron-reports/{reportId}` returns aggregate Python status.
- `GET /v1/iron-reports/{reportId}/result` returns the structured report.
- `GET /v1/iron-reports/{reportId}/artifacts` lists charts and export files.
- `GET /v1/iron-reports/{reportId}/artifacts/{artifactId}/content` downloads one task-owned artifact.

All business endpoints require `X-Internal-Token`, `X-User-Id`, `X-Tenant-Id`, and `X-Request-Id` from Java.
