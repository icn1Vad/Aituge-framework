# Rule authoring: implementation and verification checkpoint

This is the initial checkpoint. Its pending-draft and fixed-release limitations
are superseded by [the dynamic snapshot follow-up](rule-dynamic-snapshot-20260906.md).
The current branch saves confirmed authoring rules as active and freezes current
database rules for each new Java review task.

Scope: conversational rule input, a user-confirmed rule card, related-rule
candidate retrieval, and saving a private pending rule to the existing Java
library. This is not publication/activation, graph-edge maintenance, or a new
contract review engine. No live model or deployed upload-to-review acceptance is
claimed. Work is on `feature/rule-authoring-ai` in separate worktrees based on the
2026-09-06 checkpoint; the original reliability task's checkout was not edited.

## User flow and shared form reuse

The rule-library page has an `AI 辅助新增` entry. A bounded conversation proposes
the existing rule fields, asks for missing information, and lets the user edit
the card. Contract classification uses the existing frontend taxonomy. Stance,
standard and type start blank; they are not silently guessed as neutral.

`backend/rule_authoring_form.py` registers a rule workflow with the same
`service.structured_form` field definitions, registry and explicit-edit matcher
already used by reimbursement. For example, `把规则名称改成采购预付款保障`
updates the current card with zero model calls. This does not copy the travel
business workflow or import its database/service implementation from another
branch. The structured-form package now lazily imports agent tools so field
definitions and deterministic edits do not require the tool runtime.

Other messages make one bounded call through the existing tenant/model-pack
`LlmRuntime` with observability finalization, no tools, no provider retry, and
no repair loop. Invalid fields, unknown classification paths, invented reference
basis and bad dates are rejected without overwriting the frontend card.
Human confirmation is still required: schema validation alone does not prove
that an AI interpretation is substantively correct.

## APIs and persistence

Java business APIs (all require rule-create permission):

- `POST /business/review-rules/authoring/assist`: messages, current draft,
  classification paths. Returns a proposed draft, reply, questions, search terms
  and recorded usage. User/tenant/model-mode context comes from the existing
  trusted Java context, not the request body.
- `POST /business/review-rules/authoring/related`: additionally requires
  rule-list permission. Loads candidates from the actual rule table within the
  current tenant/platform scope. Other users' pending AI drafts are excluded.
- `POST /business/review-rules/authoring/save`: existing `ReviewRuleSaveReq`
  plus `Idempotency-Key`. Forces `ai_assisted` / `pending`; returns the saved
  rule. The existing tenant/source unique index and a payload fingerprint make
  retries return the same rule and reject changed payloads under the same key.

Framework internal routes are `/v1/internal/rule-authoring/assist` and
`/v1/internal/rule-authoring/related`, registered in `local_code_chat_app.py`.
They require the existing Framework internal token and nonempty tenant/user
headers. Java reuses `business.agent.qa` connection settings and the selected
AI mode; no new model credential or direct browser model call is introduced.

No database migration is added. The existing `source_dataset`, `source_rule_id`
and `source_payload_json` fields store the authoring retry identity/fingerprint.
The existing source unique index must be present. This reuses the existing rule
table rather than a parallel rule store. AI pending drafts are creator-private
in list, detail and related search. Published/shared and imported rule visibility
is preserved. The rule detail mapper now returns the previously omitted
`contractTypePath`; frontend editing preserves date/region metadata.

After save, the frontend reads the actual Java detail endpoint back and checks
content, method, classification, source and pending status. Lost create responses
reuse the frozen request/key; lost readback responses retry the read only.

## Related rules: precise capability boundary

This version uses AI-proposed short query terms (when a conversation turn was
used), SQL keyword candidate recall, and deterministic short-rule text ranking.
It extracts and reuses policy-review normalization/ngram primitives in
`aituge_model/text_similarity.py`. Existing policy similarity scoring is
unchanged and its regression tests pass. Search itself makes zero model or
embedding calls.

It does **not** connect the existing policy pgvector index to business rules:
that index contains policy units, not the rule table. It does not claim vector
semantic recall, exhaustiveness, or an authoritative relationship decision.
Candidates may differ in stance, standard, number or classification; those
differences are displayed for comparison. At most 300 SQL candidates are ranked
and 10 returned. Truncation and no-match states are explicit. No graph edge,
replacement, merge or conflict relationship is silently written.

## Verification performed

- Python: 21 passing tests (17 authoring and 4 existing policy similarity).
  Includes full-current-card followups, zero-model explicit edits, untrusted
  identity rejection, invalid/invented model output, timeout/no-retry behavior,
  comparison of numeric/stance/standard differences, and input budgets.
- Java: 12 passing Maven tests for authoring persistence/client/permissions and
  existing applicability. Persistence tests use actual MyBatis SQL over an
  isolated H2 database in MySQL mode, including roundtrip, optimistic version
  rejection, tenant/creator isolation and idempotent save. This is not a live
  MySQL/deployed authentication-stack acceptance run.
- Frontend: 22 focused tests, TypeScript, lint as part of a successful production
  Next.js build. HTTP tests verify the real business route contracts.
- Browser: production `RuleAuthoringModal` and `BusinessReviewRuleApi` → local
  Java MVC/controllers/service → actual JDBC/MyBatis persistence → actual
  Framework routes. Only the model and login identity are offline fixtures.
  Checks include extraction, explicit edit, error preservation, real candidates,
  confirmation invalidation after edits, mobile width, lost create response
  replay and lost readback recovery without duplicate database rows.

### Reproducing offline checks

Python, from the Framework root (PowerShell, use an isolated environment):

```powershell
$env:PYTHONPATH = '.;backend;backend/single-agent;services/proof/src'
python -m pytest --noconftest -o addopts='' tests/rule_authoring services/proof/tests/test_similarity.py -q
```

The selected tests need the repository's FastAPI/Pydantic, model-observability
and proof parsing/database-import dependencies. `--noconftest` deliberately
avoids unrelated root single-agent database initialization. No service database
or paid endpoint is contacted.

Java, from the Java root, with the configured JDK/Maven:

```text
mvn -pl continew-business -am test -Dtest=RuleAuthoringBusinessFlowTest,RuleAuthoringClientTest,RuleAuthoringPermissionTest,ReviewRuleApplicabilityTest -Dsurefire.failIfNoSpecifiedTests=false -Dspotless.skip=true -DskipTests=false
```

Frontend: run its existing typecheck/test/build commands; targeted tests are
`src/features/rules`, `BusinessReviewRuleApi.test.ts`, `continewAuthProxy.test.ts`.

Browser harness (three loopback-only processes):

1. In Framework, set the Python path above, `RULE_AUTHORING_OFFLINE_TEST=1`,
   `FRAMEWORK_INTERNAL_TOKEN=offline-test-token`; start
   `python -m uvicorn tests.rule_authoring.offline_server:app --host 127.0.0.1 --port 18151`.
2. In Java, set `RULE_AUTHORING_BROWSER_TEST=1`,
   `RULE_AUTHORING_TEST_FRAMEWORK_URL=http://127.0.0.1:18151`, and an isolated
   `RULE_AUTHORING_TEST_CONTROL_DIR`; run the Maven command above selecting
   `RuleAuthoringBrowserHarnessTest` instead. It writes `java-port.txt` and exits
   when a `stop` file appears in the control directory (or after 20 minutes).
3. In frontend, set `RULE_AUTHORING_TEST_JAVA_PORT` to that port and run
   `node scripts/rule-authoring-browser/serve.mjs`. Set
   `RULE_AUTHORING_TEST_OUTPUT` to an isolated artifact directory, optionally
   `PLAYWRIGHT_MODULE` to the installed Playwright module path, and run
   `node scripts/rule-authoring-browser/verify.mjs` with Chrome installed.

The fixture is excluded from production and must never be registered there.
Stop these test processes after verification. Do not touch 13005/13007/13009.

## Remaining acceptance / coordination

Actual model quality, real MySQL behavior and deployed login/permission/proxy
configuration still need separately authorized acceptance. Docker was unavailable
during this task; no existing service was started, restarted, built or deployed,
and no paid model/embedding was called. Source implementation and offline
business verification are complete; production acceptance is not.

Downstream activation must still refresh the contract executor's configured
rule snapshot. Saving a pending rule here does not automatically activate it
for contract review. Publishing governance and persistent verified rule
relationships remain outside this task's first-three-step scope.
