# Database rule updates without image rebuilds

This follow-up supersedes the initial rule-authoring checkpoint's pending-draft
policy and local-release limitation. It implements the user's current choice:
confirmed AI authoring saves are active immediately; no new personal/team
approval workflow is added. Existing tenant isolation, applicability checks and
explicit inactive states remain intact. Existing pending/expired data is not
bulk-activated.

## Runtime path

1. `ContractReviewTaskServiceImpl.createTask` calls the common `RuleSnapshotApi`
   inside the existing task/outbox transaction, before inserting the task.
2. The business implementation queries current active platform/tenant rules at
   the requested review standard. It captures complete fields, versions, source
   date and a content hash. Party/type resolution happens later, against this
   frozen set. Capture failure prevents task insertion/outbox enqueue.
3. The JSON is inserted into `biz_contract_review_task.rule_snapshot_json`.
   Idempotent task creation returns the existing task without recapturing.
   Ordinary list/detail reads exclude the large field, and entity updates can
   never overwrite it. A new task captures new data.
4. Framework reads `GET /internal/contract-review-rule-snapshots/{businessTaskId}`
   from Java with internal-service authentication and the task's trusted tenant.
   The endpoint returns only the persisted snapshot, never today's replacement.
   Missing snapshots (including pre-migration tasks), invalid hashes, malformed
   JSON, foreign tenants, duplicate IDs and HTTP failures do not fall back to a
   local release.
5. Each review uses its own `RuleLibraryShadow`/snapshot view on the shared
   executor. Source date comes from task creation, so retries across dates do not
   change applicability. The existing result cache includes the rule bundle
   hash. Formal review's AI party step gets role options from that same snapshot.
   Preflight party recognition is independent because it precedes task creation.

The source cap is 50,000 rules and 32 MiB per standard/task. Exceeding either is
an explicit error, not silent truncation. Existing bounded evidence selection
and model budgets are unchanged; being current does not mean every rule applies
to every contract or that existing evidence budgets are removed.

## First rollout (not performed by this task)

Deploy the Java, Framework and frontend changes together once. Apply the new
Liquibase changeset included by the Java master changelog:

`db/changelog/mysql/contract/contract_rule_snapshot.sql`

This adds one nullable LONGTEXT field. It does not backfill historical tasks
with current rule data. Finish old in-flight tasks under their old configuration
before switching the executor, or retain an explicitly configured legacy worker
for them. Historical completed results are unchanged.

Framework capability settings:

```text
RULE_LIBRARY_REVIEW_MODE=ACTIVE
RULE_LIBRARY_REVIEW_SOURCE=JAVA
RULE_LIBRARY_JAVA_BASE_URL=<internal Java origin, including any configured context path>
RULE_LIBRARY_JAVA_INTERNAL_TOKEN=<same secret as business.contract.agent.internal-token>
RULE_LIBRARY_REVIEW_CACHE_DIR=<existing writable persistent rule review cache directory>
```

`JAVA` is the default source when rule execution is enabled. `OFF` still disables
the existing rule-review feature. `LOCAL` must be explicitly selected for legacy
local-release operation and is never a fallback from a failed Java request.
The Java endpoint has explicit internal token checks, bypasses the normal login
session and response envelope for this route, and disables response caching and
request logging. It is not added to the browser's proxy allowlist.

After that first rollout, rule saves/edits only update the database. New review
tasks capture the committed data with no image rebuild or process restart.

## Validation performed

- Java: 37 focused tests pass, covering authoring/status, applicability,
  task creation, dispatch/outbox regressions and the hot-update flow. The hot
  flow uses the production rule/task services, real MyBatis SQL and isolated
  H2 in MySQL mode; it executes the new migration. Contract-file/preflight,
  transaction callback orchestration and outbox delivery are test fixtures.
  Production Spring transaction behavior on MySQL is not claimed as verified.
- Python: 99 focused tests pass; two opt-in cases are skipped in that command.
  Covers snapshot validation, reviewer inputs/cache replay, date stability,
  formal-stage failure before review model calls, and independent preflight.
  The real-Java opt-in test was also separately executed and passed.
- Cross-process: the Java hot-update test exposes its real internal controller
  over loopback HTTP. The production Python client and executor read task 1
  (payment after 10 days), task 2 (after 20 days), and task 1 again. A recording
  model substitute proves that the new content enters the review prompt and
  produces validated decisions; replay retains the original result with no
  additional model call. The same Java process and Python executor are retained.
- Frontend: 22 tests, TypeScript, and Next.js production build pass. Browser
  authoring → Java MVC → SQL → Framework fixture passes, including the new
  active status, lost create/read responses and no duplicate rows.

The real model, live MySQL, deployed authentication/reverse proxy, and deployed
full upload-to-review acceptance have not been run. No paid model/embedding
endpoint, existing service, container/image or database was changed.

## Reproduction

Run the existing authoring reproduction commands, adding these Java test classes:

```text
RuleSnapshotHotUpdateTest,ContractReviewTaskServiceImplTest,ContractReviewDispatchServiceTest,ContractReviewDispatchServiceOutboxTest
```

Use `-pl continew-business -am`; its test-only dependency includes contract tests.
Add the following Python files (PYTHONPATH also includes `services/contract/src`
and `services/contract/tests`):

```text
services/contract/tests/test_live_rule_snapshot.py
services/contract/tests/test_rule_library_reviewer.py
services/contract/tests/test_rule_library_shadow.py
services/contract/tests/test_rule_evidence_planner.py
services/contract/tests/test_task_rule_standard.py
services/contract/tests/test_party_ai_resolver.py
tests/test_contract_capability.py
```

For cross-process verification, set `RULE_SNAPSHOT_TEST_CONTROL_DIR` to a new
isolated directory in both Java and Python. Run `RuleSnapshotHotUpdateTest` in
Java; it writes `tasks.json` and serves loopback HTTP for up to 10 minutes. Run
`test_live_rule_snapshot.py` in Python; the opt-in case writes `result.json`.
Create `stop` in the control directory to stop the Java fixture. No credentials
are needed; its fixed internal token is test-only. Use a fresh directory on rerun.
