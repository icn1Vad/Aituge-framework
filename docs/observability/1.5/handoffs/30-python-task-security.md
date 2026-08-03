# Page 3 handoff: Python Task and security observability

## Scope and provenance

- Branch: `obs/30-python-task-security`
- Platform baseline: `7cc289e` (the frozen 1.5 platform baseline used by the parallel plan).
- Independently accepted implementation commit: `1af8937d70fb571240201bc3a52ebd40e411b668`.
- Worktree: `/home/aituge/worktrees/obs-python-task-security`
- This evidence synchronization is included in the current `obs/30-python-task-security` branch HEAD; resolve it with `git rev-parse HEAD` instead of embedding a self-referential commit SHA in this file.
- This branch was not pushed, merged, deployed, or validated against the formal environment.
- No compose, environment, credential, model configuration, frozen OpenAPI, or release-anchor file was changed.

## Delivered

- Internal Task, Run, Stage, Task Event, timeline, SSE, and security-audit query surfaces.
- Service JWT, mTLS identity boundary, signed scope/capability validation, tenant/object authorization, request correlation, and opaque snapshot handles.
- Stable filtering, deterministic ordering, materialized snapshots, snapshot reuse/capacity limits, partial-source semantics, and SSE resume/reset behavior.
- Task list materialization reads the current Task and current Run projection in one database statement, records the real read-time `dataAsOf`, and freezes the returned summaries. It does not exclude a Task merely because `updated_at > snapshotTo`, and it does not pretend to reconstruct a historical as-of projection.
- A versioned Task Event registry keyed by `(event_type, schema_version)`:
  - allowed and required metadata are explicit per event type;
  - ordinary unknown keys are removed with a low-cardinality warning/counter;
  - unknown sensitive material fails closed;
  - display text is deterministic and never sourced from caller-controlled free text.
- Identifier-only Task Event source schemas for `agent`, `artifact`, `external_executor`, `human`, `pipeline`, `task_manager`, and `tool`; the same sanitizer is used on write and read.
- Authoritative Task Event writes persist only a safe registry projection:
  - caller-controlled message text is not stored;
  - business delta/content/arguments/results remain available only in the immediate returned DTO and the existing live broker envelope;
  - provider token details are not projected;
  - only non-negative aggregate token counters are stored;
  - fixed identifiers, source fields, stream semantics, and error codes are bounded before persistence.
- Broker publication occurs only after the event transaction commits. Broker failures log only `TASK_EVENT_BROKER_PUBLISH_FAILED`; exception text, run identifiers, and event content are never logged.
- Run event sequence allocation uses one transactional `UPDATE ... RETURNING` bound to run and task. Active execution writes also bind owner, lease version, status, and lease expiry. SQLAlchemy identity-map state is synchronized without a second write.
- Task-level event sequence writes remain serialized on the authoritative Task row.
- Persistent one-time Scope JWT replay protection and service binding:
  - the signed Scope claim `serviceSubject` is compared in constant time with the already verified Service JWT subject, which itself must equal the trusted mTLS identity;
  - the one-time digest is exactly `sha256(jti)`, so changing to another otherwise-valid service identity cannot reuse the same Scope JTI;
  - a separate `sha256(service_subject)` audit field is stored; raw service subject and raw JTI are never persisted;
  - the accepted lifetime is stored through `exp + configured clock skew`, so replay remains denied throughout the whole token-acceptance window;
  - token swapping returns `403 INTERNAL_SCOPE_BINDING_INVALID`; a globally repeated JTI returns `401 INTERNAL_SCOPE_TOKEN_REPLAYED`;
  - replay-store failure returns `503 INTERNAL_SCOPE_REPLAY_GUARD_UNAVAILABLE`;
  - authentication never performs an unbounded global purge; expired claims use a separate bounded cleanup operation.
- Service JWTs remain short-lived and reusable; only the bound Scope JWT is one-time.
- Security audit persistence is injectable through independent append/query/retention session factories:
  - append commits before success is returned;
  - bounded retention delete commits before success is returned;
  - PostgreSQL role hardening proves append/read/retention least-privilege matrices; the retention role has SELECT/DELETE only and cannot execute the SECURITY DEFINER append function;
  - exact replay is idempotent and a payload mismatch is rejected as a conflict.
- Frozen internal SSE contract:
  - `Last-Event-ID` is accepted only when the checkpoint itself and every persisted event through that checkpoint form a complete, tenant-bound, replayable `status/reference` history;
  - a delta checkpoint, a later status checkpoint that would skip an earlier delta/snapshot, a sequence gap, unavailable/corrupt post-handshake data, or an unrecoverable source error produces the stable reset contract;
  - internal post-handshake source failures never expose raw database exception text;
  - safe terminal Run status and terminal marker are included when available.
- The existing business TaskManager stream gained deterministic reset behavior for persisted non-replayable history and sequence gaps while preserving the live broker envelope. It does **not** yet guarantee a stable reset for every database/source exception after the business-stream handshake; that integration gap is explicitly left for Page 8 and is not claimed as completed here.

## Verification evidence

All test commands ran on `afs2600151` in disposable server-side containers. The worktree was mounted read-only at `/app`, `PYTHONDONTWRITEBYTECODE=1` was set, `/tmp` was tmpfs, and `--confcutdir=tests` was used. Networkless suites used `--network none`. No dependency was downloaded to the Windows D: drive and no production/formal resource was used.

### Static checks

Ruff executable used:

```text
/home/aituge/.local/share/miniforge3/pkgs/ruff-0.16.1-h462bb3b_0/python-scripts/ruff
version: 0.16.1
sha256: 6793127d0f66ca40137bf68ec254b609079121f9dd6d07ee0ab8ccc56b273f68
owner/mode: aituge:aituge 0755
```

The focused Page 3 security/SSE gate was run against:

```text
backend/task_manager/observability_internal/auth.py
backend/task_manager/observability_internal/security.py
backend/task_manager/observability_internal/sse.py
tests/observability_page3_helpers.py
tests/test_internal_observability_postgres.py
tests/test_internal_observability_security_boundaries.py
tests/test_internal_observability_sse.py
```

Results:

```text
ruff check: All checks passed!
git diff --check: passed
```

A server-side baseline-versus-current diagnostic covered all 18 changed Python files with Ruff JSON and formatter output. The committed `86d080d` versions were extracted only to an exact temporary server directory and removed afterward:

```text
baseline Ruff findings: 97
current Ruff findings:  76
new finding signatures: 0
current findings on added lines: 0
baseline format: 2 files would be reformatted, 16 already formatted
current format:  2 files would be reformatted, 16 already formatted
```

The two remaining formatter-red files are the same pre-existing baseline files (`backend/task_manager/api.py` and `tests/test_task_manager_pipeline.py`). Seven Page-3-owned changed files that were newly formatter-red were formatted explicitly; no repository-wide formatter, unsafe fix, or mass rewrite of stored baseline files was run.

### Tests

- Focused Scope/SSE/Task materialization regression: `18 passed`.
- Page 3 internal contract plus stream-safety suite, network disabled: `41 passed, 3 skipped`.
  - The three skips are the explicitly PostgreSQL-gated tests and were executed separately against real PostgreSQL.
- Complete TaskManager suite, network disabled: `44 passed, 3 skipped`.
  - `PYTHONPATH` included the three service source roots required by the existing application import graph.
- Real isolated PostgreSQL Page 3 suite: `3 passed`.
  - exact append/read/retention table and function privilege matrices, including `has_function_privilege` and an actual retention-role append-function denial;
  - committed append persistence from a fresh read connection;
  - exact replay, tamper conflict, and committed bounded retention;
  - two-engine concurrent identical Scope JTI claim: exactly one success and one stable replay rejection;
  - raw JTI/service subject absence, bounded cleanup, Task/Run sequence concurrency, snapshot locking, filters, and SSE.
- The clock-skew boundary test proves a token first used at `exp - 1` remains a replay at `exp + 2` with five seconds of accepted skew and is cleaned only after `exp + skew`.
- Two valid service identities prove that a Scope Token cannot be rebound to another mTLS/Service identity and that the same JTI cannot be reused by changing service identity.
- Internal SSE tests prove that a delta Last-Event-ID and a later status checkpoint after a delta both require reset; relationship corruption, invalid source data, timeout, and database/source failure are also converted to a stable reset event.
- Task projection tests prove that a row updated after `snapshotTo` remains visible in the first current-projection materialization and that status changes after page one do not alter its frozen later page.
- Existing warnings are SQLModel `session.execute()` deprecation messages and Starlette multipart deprecation; no test failure was hidden.

One focused regression initially exposed that changing a validation helper to `TypeError` made an invalid mTLS identity fall through as HTTP 500. The authenticator now intentionally catches both `TypeError` and `ValueError`; the focused test and both complete suites passed afterward.

## Test-resource cleanup

The real PostgreSQL proof used only these exact ephemeral resources:

```text
container: contract-review-dev-page3-postgres-test-20260802
network:   contract-review-dev-page3-pg-net-test-20260802
storage:   tmpfs only; no Docker volume and no host port
```

After the final `3 passed` run, the exact container and network were removed. Verification returned no container whose name starts with `contract-review-dev-page3` and `NETWORK_ABSENT`. The tmpfs test database was intentionally destroyed and is not recoverable. No other test-stack container or network was modified.

## Security invariants proven

- Direct database assertions confirm raw business markers do not appear in persisted message, payload, token usage, source, or fixed fields.
- The immediate detached Task Event DTO preserves legal business delta/message/token details for the existing caller path and is not a SQLAlchemy-mapped object.
- The existing broker envelope preserves legal live message/payload behavior but does not gain a token-usage field.
- Database replay exposes only the safe projection.
- Sensitive payload/token/fixed fields fail closed with stable errors that never echo rejected material.
- A malicious broker exception containing a capability token cannot place that token into application logs.
- Source capacity overflow never evicts another authorization scope's snapshot.
- Replay-guard rows contain only global JTI and service-identity digests plus accepted-until time; authentication cannot delete unrelated expired rows.
- Security audit application paths cannot update or delete audit rows; retention deletion uses a separate bounded role and code path and cannot invoke the append function.
- Internal observability SSE post-handshake source failure never leaks raw database, capability, tenant, Task, or Run data through an exception response. This invariant does not cover the legacy business TaskManager stream.

## RED / deployment-owner actions

1. The immediate Task Event broker is not a durable business-content replay store. If product requirements demand durable reconnect replay of raw model deltas, build a separate encrypted, short-retention, strict-ACL business stream. Never place raw content back into `tuge_task_event`.
2. Wire the internal router only behind the authenticated Java-to-Python route and enforce real mTLS at the proxy/service-mesh layer; the OpenAPI extension alone does not enable mTLS. The Page 8 Java Scope signer must emit the signed `serviceSubject` claim equal to its verified Service JWT/mTLS identity.
3. Provision independent production connection pools/credentials for audit append, observability query, retention, and Scope JTI replay guard. Run the PostgreSQL role-hardening function during deployment.
4. Schedule bounded Scope JTI cleanup, snapshot cleanup, and audit retention jobs; publish capacity, cleanup, replay-guard-unavailable, and retention metrics/alerts.
5. Preserve the `reset-required` recovery contract in Java and the frontend: discard stale incremental state and reload through the paginated endpoint.
6. Configure proxy/access-log redaction for cursor, retry token, locator, access context, high-watermark, Authorization, cookie, and internal scope values.
7. Run the frozen OpenAPI reconciliation, Java-Python contract tests, standard OpenAPI validation, and integration/formal wiring in the owning Page 7/Page 8 tasks. No production or formal validation is claimed here.
8. Resolve the frozen contract conflict for `sourceIpMasked`: code and persistence accept the 76-character HMAC form with an 80-character bound, while the frozen internal OpenAPI query parameter still declares `maxLength: 64`. The contract owner must align it before code generation.
9. Decide whether the repository-wide Ruff configuration should retain the broad rule set. The focused security/SSE files are green, while the documented 18-file diagnostic remains red and must not be presented as a passing repository gate.
10. The business `/task-manager/stream` endpoint still serializes `str(exc)` from its local `event_stream`, and database/source failures from `_stream_run_sse` can still escape instead of producing a stable reset or fixed error envelope. Page 8 must remove raw exception exposure and normalize those failures; Page 3 does not claim that legacy business-stream boundary is fixed.
