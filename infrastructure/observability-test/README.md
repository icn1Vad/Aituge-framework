# Test observability infrastructure

Standalone, test-only observability foundation for the contract-review-code-dev test environment. It is its own Docker Compose project, contract-review-code-dev-observability. It is not an application overlay and it does not select, merge, render, hash, validate, recreate, or otherwise depend on any business Compose file.

Java, Framework, Proof, OCR, converter and future services may be released from different repositories and Compose roots. Updating one of those services must not require rebasing this observability stack or restarting a working observability component. Deploying or rolling back this stack must never recreate an existing business container.

The stack owns two internal networks:

- contract-review-code-dev-observability-ingest is the stable, test-only OTLP ingress boundary. A future normal business release may explicitly join it and send OTLP to the documented Collector endpoint.
- contract-review-code-dev-observability-backend is the storage and UI path for Loki, Tempo, Prometheus, Alertmanager, Grafana and the Collector backend. No business service may join it.

Grafana alone also joins contract-review-code-dev-observability-loopback. This
is a dedicated, non-internal Docker bridge required for Docker to establish the
published loopback listener; it is not an application or storage network and no
business service may join it. Grafana is published only as
127.0.0.1:13000 on the host. It is not
reachable from the LAN. An administrator accesses it through an SSH local port
forward (for example, ssh -L 13000:127.0.0.1:13000 cortex) and opens
http://127.0.0.1:13000 locally.

Existing business containers are not changed merely to deploy observability. Host capture supplies redacted Docker logs immediately. Application traces and metrics become available when their owning service is next normally released with the stable ingest attachment and OTLP configuration; they are never added by mutating a live business container from this project. Nothing here may be applied to the formal stack.

## Isolation baseline

- Host capture normally runs in the unit-fixed `PROJECT_DISCOVERY` mode. An ordinary future test release is automatically eligible after it passes all of: exact test Compose project, approved test name prefix, non-empty Compose service identity, approved test-network membership, and rejection of formal-network attachments. The candidate is re-inspected immediately before every `docker logs --follow`. This is bounded project discovery, not broad Docker discovery.
- The unit also loads one strict selector manifest alongside discovery. That manifest is only the legacy bridge for the standalone `frontnew-test` container; standard new services must not be added to it. Pure `STRICT_SELECTORS` mode remains available for isolated diagnostics.
- Discovery and selector configuration may identify containers, names, and networks only. It never reads container environment values, and it never uses a business Compose path, business secret, mutable current link, or image tag as a selection rule.
- The only unit-owned network allowlist extension is for a genuinely special test isolation network. That is a root-controlled unit/release change followed by normal validation; do not loosen discovery or add per-service entries for routine features.
- Alloy has no Docker socket and no Docker container-directory mount. It reads
  only redacted files from an explicit test-only host directory.
- Container IDs and image IDs must be full hex values. Discovery rechecks the
  running state, full ID, image ID, name, exact project, service and complete
  network set immediately before every `docker logs --follow`. Strict
  selectors additionally recheck environment, scope and capture opt-in.
  Only the full container ID becomes a file name.
- Every script rejects Docker environment overrides and pins the Docker binary,
  context, endpoint/TLS state, resolved socket and metadata, and daemon
  identity before Docker access.
- All observability containers, labels, networks, volumes, and storage are test-named. The two observability networks are internal and are owned by the standalone observability project.
- The eight observability services have fixed CPU and memory ceilings; their
  combined memory limit is about 4.2 GiB so they cannot consume the host unchecked.
- Loki, Tempo, Prometheus, and host capture retention are 10 days.
- Cursors, retry tokens, locators, access contexts, high-watermarks, credentials,
  prompts, responses, contract content, and request bodies are forbidden data.
- Caddy emits JSON access logs to stdout only after the complete request URI
  (path and query), client address fields, complete request-header map, request
  host, and request method are deleted. It records none of those fields.
  Request bodies are never configured as log fields. The host redactor remains
  a second fail-safe before Loki.

The test and formal projects still share a host Docker daemon. This is a shared
host trust boundary, not physical isolation. These controls isolate project
selection, networks, volumes, observability storage, and collected data.

## Required inputs

Deployment supplies these runtime inputs. Secret contents remain outside Git;
the approved non-secret image references are fixed in the verifier.

- `CONTRACT_REVIEW_DEV_TEST_SECRETS_DIR`: exactly
  `/home/aituge/contract-review-code-dev-test-private/secrets`; every path
  component is real and non-symlinked, the private root and secret directory
  are owned by aituge with mode 0700. The Grafana password file is regular, mode 0600, owner/group aituge, and single-link.
- `CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT`: real, non-symlinked, owned by
  `aituge:aituge`, mode `0700`, and exactly
  `/home/aituge/contract-review-code-dev-test-private/observability-logs/redacted`
  when the supplied systemd unit is used. The private root and
  `observability-logs` parent are also `aituge:aituge/0700`. Its
  `.capture-manager.lock`, when present, is a regular, non-symlinked,
  single-link file owned by `aituge:aituge` with mode `0600`.
- The platform source is an immutable, versioned, non-symlinked release artifact, never a worktree, `current` symlink, or mutable
  checkout. The capture unit executes
  `/opt/contract-review-code-dev-observability/releases/observability-page8-v1.5.2/infrastructure/observability-test`.
  Page 8 stages that exact release as `root:root`, removes group/other write
  access, verifies its manifest before installation, and never overwrites that release ID.
- The capture unit has no `EnvironmentFile`. Its root-owned contents directly fix
  the non-secret mode, approved extra test network, legacy selector path, redacted
  log root and a safe system PATH. It does not load a mutable workspace file.
- The selector file is only the legacy `frontnew-test` bridge evaluated alongside project discovery.
  A normal new project service is discovered automatically; it does not need a
  selector, service-unit edit, or observability Compose change.
- Five image variables are mandatory and have no mutable-tag defaults:
  `CONTRACT_REVIEW_DEV_LOKI_IMAGE`,
  `CONTRACT_REVIEW_DEV_ALLOY_IMAGE`,
  `CONTRACT_REVIEW_DEV_TEMPO_IMAGE`,
  `CONTRACT_REVIEW_DEV_ALERTMANAGER_IMAGE`, and
  `CONTRACT_REVIEW_DEV_GRAFANA_IMAGE`. Page 8 may write them only to a
  test-only deployment manifest after an approved pull. Each value must be the
  documented repository and frozen tag followed by `@sha256:<approved-digest>`.
  The digest, local image ID, tag binding, and RepoDigest must all agree.
  Runtime values must exactly match the committed approval constants and must
  not be invented, copied from an unverified cache, or shared with the formal
  stack. Compose uses `pull_policy: never`.

- `CONTRACT_REVIEW_DEV_GRAFANA_ADMIN_USER`: non-default admin name; the
  password is provided only by a Docker secret file.

The Grafana process does not mount the host-backed 0600 secret directly. A
networkless, read-only, capability-minimized one-shot init service copies it
into a dedicated Docker volume as mode 0400, owner 472:0. Grafana runs as
472:0 and mounts that derived volume read-only. Page 8 must verify the fixed
image still uses UID 472, exercise the init/read path without printing data,
and force-recreate the init service whenever the password rotates.

Validators print stable codes only, never secret names, contents, or hashes.

The existing workspace is mode `0775` and its `aituge` group contains
another account. Page 6 does not chmod, chown, or otherwise mutate that shared
workspace. Secret and redacted-log writable roots therefore live under the
separate `/home/aituge/contract-review-code-dev-test-private` boundary,
anchored by `/home/aituge` mode `0750`. The redactor and capture launcher
open every component with `openat(O_NOFOLLOW)` and retain directory FDs for
all writes, rotation, cleanup, and manager locking. The launcher binds the
root device/inode into the manager environment; follow workers and retention
cleanup inherit and revalidate that same FD. A later valid-directory rename
or replacement cannot redirect output.

The launcher invokes every Python entry point with `/usr/bin/python3 -E -s -B`,
removes `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONINSPECT`,
`LD_PRELOAD`, `LD_LIBRARY_PATH`, `BASH_ENV`, and `ENV` from the inherited
environment, and the systemd candidate repeats that boundary with
`UnsetEnvironment=`. User-site packages and a malicious `sitecustomize` cannot
enter the capture process.

## Fixed components

| Component | Image | Exposure |
| --- | --- | --- |
| Loki | required `CONTRACT_REVIEW_DEV_LOKI_IMAGE` for `grafana/loki:3.7.2@sha256:<approved-digest>` | internal |
| Alloy | required `CONTRACT_REVIEW_DEV_ALLOY_IMAGE` for `grafana/alloy:v1.18.0@sha256:<approved-digest>` | internal |
| Tempo | required `CONTRACT_REVIEW_DEV_TEMPO_IMAGE` for `grafana/tempo:2.10.5@sha256:<approved-digest>` | internal |
| OTel Collector | `otel/opentelemetry-collector-contrib:0.153.0@sha256:666fb40ee1391aa9f0eddb06ea143ffe215d731ff02cf13ce4c2f44f2a3bf89a` | internal |
| Prometheus | `prom/prometheus:v3.13.0@sha256:b96d6068885d3045ae74e149890aa574948f6fcaa6005b82171c6b2be7947998` | internal |
| Alertmanager | required `CONTRACT_REVIEW_DEV_ALERTMANAGER_IMAGE` for `prom/alertmanager:v0.32.1@sha256:<approved-digest>` | internal/null receiver |
| Grafana secret init | required `CONTRACT_REVIEW_DEV_GRAFANA_IMAGE` for `grafana/grafana:13.1.0@sha256:<approved-digest>` | no network; one shot |
| Grafana | same required Grafana digest reference | host loopback only (127.0.0.1:13000 by default) |

The committed OTel and Prometheus references are dual-bound to the
frozen tag and a RepoDigest already present and revalidated on the server.
No digest is recorded for a missing image. `verify-image-supply-chain.sh`
rejects mutable tags, wrong repositories/tags, ambiguous digests, absent local
images, and any mismatch between the exact reference, tag image ID, and
RepoDigest. The observability Compose cannot render for deployment until all
five approved digest variables exist.

Logs flow from Docker's bounded `json-file` driver (20 MiB x 5, compressed)
through the host selector and fail-safe redactor into 0600 bounded files. Alloy
tails that directory read-only and forwards to Loki. `docker logs --since 10s`
provides at-least-once reconnect behavior and can duplicate boundary records.
Every emitted record has `stream=APPLICATION`; no exactly-once claim is made,
and correlation/event IDs should be used for query-side tolerance when
available.

Alloy reads only the active `*.jsonl` files. It deliberately does not read
rotated `.jsonl.1` through `.jsonl.4` files because this candidate has no
validated fingerprint/positions scheme that can do so without duplicates.
Consequently, if Alloy is unavailable across one or more 20 MiB rotations,
records that still exist in rotated host files can be absent from Loki. This
is a fail-open, best-effort observability gap, not lossless delivery. The
administrator API must expose the affected source as delayed/incomplete via
`sourceStatus`, and Page 8 must alert on lag and exercise an outage-across-
rotation capacity/failure test before deployment.

Span Links are intentionally disabled by approved policy. This Collector version cannot preserve Link identity while safely removing Link attributes, so linked Spans are dropped fail-closed. Asynchronous work is correlated by taskId and runId in the administrator timeline, not by a long-lived linked Trace. Unlinked synchronous Spans and metrics may enter OTel when their owning application release is configured for the ingest network.

OTel validates code-owned service names, metric names/units, schema IDs, scope, provider/model, route/privacy, HTTP, feature, status, and operation values before retaining their allowlisted keys. Invalid Spans, Span events, Metrics, or Datapoints are dropped fail-closed.

OTel Collector version 0.153.0 cannot safely replace an exemplar slice through OTTL. The test configuration therefore fails closed by dropping an entire metric datapoint whenever it contains one or more exemplars. Exemplar-free safe datapoints continue to Prometheus. This deliberately trades exemplar-bearing metric data for leakage prevention; application instrumentation must not attach exemplars in this test environment until a fixed Collector path is validated.

## Validation

Run only on the server:

```bash
infrastructure/observability-test/docker-trust.sh
infrastructure/observability-test/probes/verify-docker-safety.sh
infrastructure/observability-test/probes/validate-compose.sh
infrastructure/observability-test/probes/verify-isolation.sh
infrastructure/observability-test/probes/verify-redaction.sh
infrastructure/observability-test/probes/verify-gateway.sh
infrastructure/observability-test/probes/verify-sse.sh
infrastructure/observability-test/probes/verify-telemetry-configs.sh
infrastructure/observability-test/probes/verify-image-supply-chain.sh
```

`verify-telemetry-configs.sh` proves a safe metric and an unlinked Span remain
exportable, while sensitive key/value, schema-name/unit, exemplar, linked-Span, and service-name canaries never reach either exporter. A linked-Span canary is expected to be dropped and the approved policy emits OTEL_SPAN_LINKS_DISABLED_OK; it is not a deployment failure. The image supply chain probe remains red until all five required approved digest references and local images exist.

The probes do not deploy. Transient resources use random run IDs and
Page-6-only names and labels. Before each create, the exact unique name and run
ID enter a quarantine ledger. Whether Docker CLI returns success or failure,
the helper independently re-lists that exact name and validates its full ID,
name, and all Page-6 labels. A matching resource created despite a non-zero CLI
result is recovered and removed only by full ID. A same-name resource with
different identity becomes a stable manual-cleanup red state and is never
deleted. Quarantine clears only after the exact name is proven absent. The
fake-Docker probe covers no-resource failure, non-zero-but-created recovery,
collision, invalid IDs, initial verification failure, drift, and retry.

## Deployment behaviour

Deploy only compose.observability-test.yml under its explicit observability project name. The deployment preflight validates the standalone stack, fixed images, private log/secret roots and its own internal networks. It must not receive an application Compose file, application overlay, application release hash, or application service list.

Install host capture separately from the immutable `/opt/contract-review-code-dev-observability/releases/observability-page8-v1.5.2` release. The root-owned unit carries its non-secret capture settings itself rather than sourcing a mutable environment file. Once it is running, approved existing test containers begin contributing redacted logs without a business restart. A future application release can opt in to traces and metrics by attaching only to the observability ingest network and configuring its OTLP exporter to the stable Collector endpoint. It must not attach to the observability backend network, receive observability storage credentials, or be recreated by this stack.

For a standard new feature, use the normal test Compose project and one approved test network: its redacted logs enter automatically through project discovery, and its trace/metrics attachment is declared by that feature's own normal release. Do not create a feature-specific observability table, selector, overlay, release dependency, or long-lived Compose coupling. Only a feature that genuinely uses a new isolated test network needs a reviewed update to the root-controlled capture-unit network allowlist.

Rollback is standalone: stop or roll back only the observability project and host-capture release after preserving required evidence. Do not use Compose down, wildcard cleanup, or a shared application project command against business containers.
