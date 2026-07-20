# Iron Ore Report Demo Implementation Plan

## Plan authority and stage gate

This document is the single implementation plan for the Python, Java, and isolated deployment work.
Before starting a new stage, the developer must read this file again, verify that the previous stage meets its exit criteria, and only then continue.

No local dependency installation, compilation, test execution, image build, or fat-package creation is allowed. Local work is limited to editing, review, Git commit, and Git push. Builds and tests run only inside Docker on the server.

## Current checkpoint (2026-07-20)

- Stages 1 through 6 are implemented and accepted in the isolated `/home/aituge/workspace/ai-feature-demo` environment.
- The isolated MySQL, Redis, Python, and Java containers are healthy. The final server Docker runs passed 20 Python tests, 49 Java tests, and the complete `continew-server` package build.
- The first real DAILY run safely exposed an Agent output-schema mismatch; the exact JSON Schema is now injected and schema failures use `IRON_REPORT_OUTPUT_SCHEMA_INVALID`.
- A fresh DAILY run then completed the full Java-to-Python chain with live research, 11 metrics, 5 sections, 7 sources, two dynamic PNG charts, DOCX, LibreOffice PDF, Java SHA-256 verification, private-file archive, and Java-only downloads. All four archived files matched the JSON checksum, download header checksum, and downloaded bytes.
- Idempotency now passes end to end: a new request returns `reused=false`, same-key/same-body replay returns the same task with `reused=true`, and same-key/different-body returns `409 IRON_REPORT_IDEMPOTENCY_CONFLICT`. The archived DAILY result also remained available through Java after the Python container was rebuilt.
- Model-wrapped JSON is now recovered only from one bounded fenced JSON object and still passes the strict registered schema; specific export errors are preserved.
- A rerun WEEKLY report passed with `researchStatus=DISABLED`, 18 metrics, 6 sections, 7 sources, DOCX/PDF/two charts, archive and download checksums. Visual inspection confirmed the first chart reaches July 17 and includes the 20-session moving average; the second uses three independently rebased series and explicitly states it is not a cross-currency price spread.
- An out-of-range Java request for 2026-07-18 passed with HTTP 422 and `IRON_REPORT_DATE_OUT_OF_RANGE`.
- A deliberately unreachable live-search endpoint exposed an unsafe core-tool failure shape (`result` became a string and downstream ReAct raised `AttributeError`). The `iron_report_research` business adapter now reuses the existing Aliyun client but always returns either structured `LIVE` results or fixed `news_snapshot.json` content with `FALLBACK`; the Agent no longer calls core `web_search` directly.
- Search degradation passed 20 Docker tests and a repeated real Java-to-Python failure E2E. Java task `869607775280300066` completed as `SUCCEEDED` with `researchStatus=FALLBACK`, 15 metrics, 6 sections, 5 sources, and 4 archived Artifacts. The original private search configuration was restored and the Python container returned healthy.
- Tenant and owner isolation passed through the real Java HTTP layer. Temporary rows with the same user/different tenant and the same tenant/different user both returned HTTP 404 with `IRON_REPORT_TASK_NOT_FOUND` and a ContiNew `X-Trace-Id`; all temporary rows were deleted.
- Full restart persistence passed after force-recreating all four isolated containers. The LIVE daily, DISABLED weekly, and FALLBACK daily tasks remained `SUCCEEDED`; all 12 archived files were downloaded again and their pre-restart checksum, response-header checksum, and actual-file SHA-256 were identical.
- The existing `frontnew-web`, `continew-dev-java`, `ai-framework-proof-1`, document-viewer test, DEV Redis, and DEV MySQL container identities and start times were unchanged across the complete verification run.
- Accepted runtime code commits are Python `a80087bac433029adbb5a6d59412c9939848debf` and Java `c8727613257b71af1b2180ac98d66de237670eb5`; later Python commits only update this acceptance record. No PR or merge was performed.
- At every remaining feature boundary, reread this file and update this checkpoint before proceeding.

## Confirmed baselines

- Python repository: `AI-tuge/Aituge-framework`
- Python base: `origin/proof@21a71c8f10a97a851deea11013a89a4bb0981902`
- Python branch: `feat/iron-report-demo`
- Python worktree: `E:\MyProjects\AIflamework\Aituge-framework-iron-report-demo`
- Java repository: `AI-tuge/Javabackend`
- Java base: `javabackend/main@30eef6fee6cfca728f35cea01d9d9893dd608ab2`
- Java branch: `feat/iron-report-demo`
- Java worktree: `E:\MyProjects\theone-worktrees\iron-report-demo`
- Prepared data source: `E:\MyProjects\iron&translate`

## Non-negotiable boundaries

- Add an independent `services/iron_report` business service. Do not modify Framework core behavior.
- Reuse CapabilityRegistry, ReactAgent, ToolRegistry, the existing Aliyun search client through the business-safe research adapter, `code_interpreter`, TaskManager, and Artifact.
- Do not reuse Douyin-specific report logic.
- Do not modify translation implementation, BabelDOC implementation, current DEV, production, or the running Proof containers.
- Frontend calls Java only. Python endpoints are internal.
- Model ID, model endpoint, model key, search endpoint, and search key are configuration or private runtime data; never hard-code them.
- Use the prepared dataset unchanged. Preserve its source metadata, hashes, and quality report.
- Do not add a VLM chart-review loop.

## Stage 1 - Python business service

Deliverables:

- `services/iron_report` package, API, configuration, errors, schemas, data repository, service, and tests.
- Strict `asOfDate` validation.
- Separate `reportDate` from `dataAsOfDate`.
- Return `IRON_REPORT_DATE_OUT_OF_RANGE` when the requested date cannot be served.
- `POST /v1/iron-reports` returns HTTP 202.
- Unified error envelope, idempotency, tenant/user isolation, and Artifact error codes.
- Search failure degrades to `news_snapshot.json` and does not automatically fail the report.

Exit criteria:

- Code review confirms no Framework core file was changed.
- Task input/output schemas and error contracts are explicit.
- Prepared data is copied without transformation and can be hash-checked in the server image.

## Stage 2 - ReAct analysis, charts, and exports

Deliverables:

- Capability entry, iron report Agent, Skill, and `iron_market_data` HTTP tool.
- ReAct uses real data, configured search through `iron_report_research`, and code_interpreter.
- Daily and weekly structured reports.
- Dynamic PNG charts published as Framework Artifacts.
- DOCX generated with `python-docx`.
- PDF generated primarily by LibreOffice Headless from the DOCX.
- DOCX/PDF registered as task-owned Artifacts.

Exit criteria:

- Missing or failed online search uses the fixed news snapshot with an explicit degraded flag.
- A missing required chart or failed LibreOffice conversion produces a stable, retry-aware error.
- No ReportLab primary export path exists.

## Stage 3 - Java business domain

Deliverables:

- `ironreport` Controller, Service, Mapper, entity, query, request, response, enums, and constants.
- `IronReportAgentClient` and `PythonIronReportAgentClient`.
- Sa-Token permissions, trusted user/tenant context, idempotency, task history, and tenant isolation.
- Liquibase task/artifact tables and permission nodes.
- Java downloads Python Artifacts, verifies SHA-256, and archives them through ContiNew FileApi.
- Task uses detailed `ARCHIVING` stage.
- Java marks the task `SUCCEEDED` only after every required file is verified and archived.

Exit criteria:

- Python success alone never makes the Java task successful.
- Archive retries are idempotent and do not create duplicate file records.
- Frontend download reads the archived ContiNew file, not a temporary Python file.

## Stage 4 - Isolated Docker deployment definition

Deliverables:

- `iron-report-demo-python`, `continew-ai-demo-java`, isolated MySQL, isolated Redis, and `ai-feature-demo-net` definitions.
- Independent Compose, env example, ports, volumes, health checks, and read-only test-data mount/copy.
- No translation or BabelDOC implementation changes. A future integration branch merges the public Compose.
- Python image installs `python-docx` and LibreOffice Writer only during server Docker build.

Exit criteria:

- No dependency on current DEV/production databases, Redis, source directories, or containers.
- Secrets are absent from Git and injected through the independent server environment/private config.

## Stage 5 - Review, commit, and push

Deliverables:

- Inspect both worktree statuses and diffs.
- Run `git diff --check` locally only; do not compile or install.
- Commit only intended files.
- Push both `feat/iron-report-demo` branches.
- Do not open a PR unless explicitly requested.

Exit criteria:

- Both remote branches exist at the reviewed commits.
- No unrelated local changes are included.

## Stage 6 - Server-only build and verification

Deliverables:

- Use `/home/aituge/workspace/ai-feature-demo` only.
- First bootstrap may clone into an empty independent directory; later updates use `git pull --ff-only`.
- Docker build, Docker up, and all automated/smoke tests run on the server.
- Verify daily report, weekly report, charts, DOCX, LibreOffice PDF, SHA-256, archive, download, idempotency, tenant isolation, failure handling, and restart persistence.

Exit criteria:

- Required containers are healthy.
- Both end-to-end report types finish with Java status `SUCCEEDED` only after `ARCHIVING`.
- Existing DEV, production, and Proof container identities remain unchanged.
