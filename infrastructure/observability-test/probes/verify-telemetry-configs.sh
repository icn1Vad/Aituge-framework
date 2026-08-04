#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
readonly REPO_ROOT="$(repo_root_from_probe)"
readonly STACK_DIR="${REPO_ROOT}/infrastructure/observability-test"
readonly OTEL_CONFIG="${STACK_DIR}/otel-collector/config.yml"
readonly ALLOY_CONFIG="${STACK_DIR}/alloy/config.alloy"
readonly OTEL_IMAGE="otel/opentelemetry-collector-contrib:0.153.0@sha256:666fb40ee1391aa9f0eddb06ea143ffe215d731ff02cf13ce4c2f44f2a3bf89a"
readonly ALLOY_REPOSITORY_TAG="grafana/alloy:v1.18.0"
readonly ALLOY_IMAGE="${CONTRACT_REVIEW_DEV_ALLOY_IMAGE:-}"
readonly RUN_ID="$(page6_new_run_id)"
readonly NETWORK="contract-review-code-dev-page6-telemetry-network-${RUN_ID}"
readonly OTEL_VALIDATE="contract-review-code-dev-page6-otel-validate-${RUN_ID}"
readonly OTEL_START="contract-review-code-dev-page6-otel-start-${RUN_ID}"
readonly OTEL_TRACE="contract-review-code-dev-page6-otel-trace-${RUN_ID}"
readonly ALLOY_VALIDATE="contract-review-code-dev-page6-alloy-validate-${RUN_ID}"
NETWORK_ID=
OTEL_VALIDATE_ID=
OTEL_START_ID=
OTEL_TRACE_ID=
ALLOY_VALIDATE_ID=

cleanup() {
    local rc=0
    page6_cleanup_registered || rc=1
    cleanup_probe_dir
    return "$rc"
}
create_probe_dir
probe_install_cleanup_traps cleanup

page6_verify_docker_daemon || probe_fail "TELEMETRY_DOCKER_TRUST_REJECTED"
page6_validate_local_image_reference "$OTEL_IMAGE" "otel/opentelemetry-collector-contrib:0.153.0" ||
    probe_fail "OTEL_APPROVED_IMAGE_DIGEST_INVALID"

grep -Fq 'transform/fail_closed_redaction:' "$OTEL_CONFIG" ||
    probe_fail "OTEL_FAIL_CLOSED_REDACTOR_MISSING"
grep -Fq 'error_mode: propagate' "$OTEL_CONFIG" ||
    probe_fail "OTEL_FAIL_CLOSED_MODE_MISSING"
grep -Fq 'keep_keys(resource.attributes, [' "$OTEL_CONFIG" ||
    probe_fail "OTEL_ATTRIBUTE_ALLOWLIST_MISSING"
grep -Fq 'filter/drop_exemplar_datapoints:' "$OTEL_CONFIG" ||
    probe_fail "OTEL_EXEMPLAR_FAIL_CLOSED_FILTER_MISSING"
grep -Fq 'Len(datapoint.exemplars) > 0' "$OTEL_CONFIG" ||
    probe_fail "OTEL_EXEMPLAR_FAIL_CLOSED_CONDITION_MISSING"
grep -Fq 'filter/drop_linked_spans:' "$OTEL_CONFIG" ||
    probe_fail "OTEL_LINKED_SPAN_FAIL_CLOSED_FILTER_MISSING"
grep -Fq 'Len(span.links) > 0' "$OTEL_CONFIG" ||
    probe_fail "OTEL_LINKED_SPAN_FAIL_CLOSED_CONDITION_MISSING"
grep -Fq 'filter/drop_invalid_spans:' "$OTEL_CONFIG" ||
    probe_fail "OTEL_SPAN_VALUE_ALLOWLIST_MISSING"
grep -Fq 'filter/drop_invalid_metric_schema:' "$OTEL_CONFIG" ||
    probe_fail "OTEL_METRIC_SCHEMA_ALLOWLIST_MISSING"
grep -Fq 'filter/drop_invalid_datapoints:' "$OTEL_CONFIG" ||
    probe_fail "OTEL_DATAPOINT_VALUE_ALLOWLIST_MISSING"
grep -Fq 'filter/drop_invalid_span_events:' "$OTEL_CONFIG" ||
    probe_fail "OTEL_SPAN_EVENT_VALUE_ALLOWLIST_MISSING"
grep -Fq 'set(span.name, "redacted-span")' "$OTEL_CONFIG" ||
    probe_fail "OTEL_SPAN_NAME_NORMALIZATION_MISSING"
grep -Fq 'set(span.status.message, "")' "$OTEL_CONFIG" ||
    probe_fail "OTEL_SPAN_STATUS_REDACTION_MISSING"
grep -Fq 'set(spanevent.name, "redacted-event")' "$OTEL_CONFIG" ||
    probe_fail "OTEL_SPAN_EVENT_NAME_NORMALIZATION_MISSING"
grep -Fq 'set(metric.description, "")' "$OTEL_CONFIG" ||
    probe_fail "OTEL_METRIC_DESCRIPTION_REDACTION_MISSING"

page6_create_network "$NETWORK" "$RUN_ID" --internal
NETWORK_ID="$PAGE6_LAST_NETWORK_ID"

page6_create_container "$OTEL_VALIDATE" "$RUN_ID" --network none \
    -v "${OTEL_CONFIG}:/etc/otelcol-contrib/config.yml:ro" \
    "$OTEL_IMAGE" validate --config=/etc/otelcol-contrib/config.yml
OTEL_VALIDATE_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start -a "$OTEL_VALIDATE_ID" >/dev/null || probe_fail "OTEL_FIXED_IMAGE_VALIDATE_FAILED"
probe_pass "OTEL_FIXED_IMAGE_VALIDATE_OK"

page6_create_container "$OTEL_START" "$RUN_ID" --network "$NETWORK_ID" \
    -v "${OTEL_CONFIG}:/etc/otelcol-contrib/config.yml:ro" \
    "$OTEL_IMAGE" --config=/etc/otelcol-contrib/config.yml
OTEL_START_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start "$OTEL_START_ID" >/dev/null
running=0
for attempt in {1..20}; do
    page6_validate_container "$OTEL_START_ID"
    state=$(page6_docker inspect --format '{{.State.Status}}' "$OTEL_START_ID")
    if [[ $state == running ]]; then running=1; break; fi
    [[ $state == created || $state == restarting ]] || break
    sleep 0.2
done
if [[ $running -ne 1 ]]; then
    page6_container_logs "$OTEL_START_ID" >&2 || true
    probe_fail "OTEL_FIXED_IMAGE_START_FAILED"
fi
probe_pass "OTEL_FIXED_IMAGE_START_OK"

readonly OTEL_IP="$(page6_docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$OTEL_START_ID")"
[[ "$OTEL_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] ||
    probe_fail "OTEL_CONTAINER_IP_INVALID"
readonly CANARY="$(python3 -c 'import secrets; print("PAGE6_" + secrets.token_hex(24))')"
readonly METRIC_PAYLOAD="${PROBE_DIR}/metric.json"
readonly METRIC_OUTPUT="${PROBE_DIR}/metrics.txt"
readonly METRIC_RESPONSE="${PROBE_DIR}/metric-response.txt"
python3 - "$METRIC_PAYLOAD" "$CANARY" <<'PY'
import json
import pathlib
import sys
import time

payload_path = pathlib.Path(sys.argv[1])
canary = sys.argv[2]
now = str(time.time_ns())
payload = {
    "resourceMetrics": [{
        "resource": {
            "attributes": [
                {"key": "service.name", "value": {"stringValue": "page6-metric"}},
                {"key": "db_password", "value": {"stringValue": canary}},
                {"key": "url.full", "value": {"stringValue": canary}},
            ]
        },
        "schemaUrl": canary,
        "scopeMetrics": [{
            "scope": {
                "name": canary,
                "version": canary,
                "attributes": [
                    {"key": "scope_secret", "value": {"stringValue": canary}}
                ],
            },
            "schemaUrl": canary,
            "metrics": [{
                "name": "page6_safe_metric",
                "description": canary,
                "unit": "1",
                "gauge": {
                    "dataPoints": [{
                        "attributes": [
                            {
                                "key": "operation",
                                "value": {"stringValue": "PAGE6_PROBE"},
                            },
                            {
                                "key": "provider",
                                "value": {"stringValue": "deepseek"},
                            },
                            {
                                "key": "model_name",
                                "value": {"stringValue": "deepseek-v4-pro"},
                            },
                            {
                                "key": "feature_code",
                                "value": {"stringValue": "CONTRACT_REVIEW_PROBE"},
                            },
                            {
                                "key": "privacy_mode",
                                "value": {"stringValue": "PRIVATE"},
                            },
                            {
                                "key": "route_type",
                                "value": {"stringValue": "EXTERNAL"},
                            },
                            {"key": "url.full", "value": {"stringValue": canary}},
                            {"key": "http.target", "value": {"stringValue": canary}},
                            {"key": "db.statement", "value": {"stringValue": canary}},
                            {
                                "key": "exception.message",
                                "value": {"stringValue": canary},
                            },
                            {"key": "gen_ai.prompt", "value": {"stringValue": canary}},
                            {"key": "authorization", "value": {"stringValue": canary}},
                        ],
                        "timeUnixNano": now,
                        "asDouble": 7.0,
                        "exemplars": [{
                            "filteredAttributes": [
                                {
                                    "key": "access_token",
                                    "value": {"stringValue": canary},
                                }
                            ],
                            "timeUnixNano": now,
                            "asDouble": 7.0,
                            "spanId": "00" * 8,
                            "traceId": "00" * 16,
                        }],
                    }]
                },
            }],
        }],
    }]
}
metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
safe_datapoint = metrics[0]["gauge"]["dataPoints"][0]
exemplars = safe_datapoint.pop("exemplars")
metrics.append({
    "name": "page6_exemplar_canary_metric",
    "description": "This point must be dropped fail-closed",
    "unit": "1",
    "gauge": {
        "dataPoints": [{
            "attributes": [{
                "key": "operation",
                "value": {"stringValue": "PAGE6_PROBE"},
            }],
            "timeUnixNano": now,
            "asDouble": 9.0,
            "exemplars": exemplars,
        }]
    },
})
metrics.extend([
    {
        "name": "page6_invalid_value_metric",
        "description": "Allowed keys with invalid values must be dropped",
        "unit": "1",
        "gauge": {
            "dataPoints": [{
                "attributes": [
                    {"key": "operation", "value": {"stringValue": canary}},
                    {"key": "provider", "value": {"stringValue": canary}},
                    {"key": "model_name", "value": {"stringValue": canary}},
                ],
                "timeUnixNano": now,
                "asDouble": 10.0,
            }]
        },
    },
    {
        "name": canary,
        "description": "Unregistered metric name must be dropped",
        "unit": "1",
        "gauge": {
            "dataPoints": [{
                "attributes": [{
                    "key": "operation",
                    "value": {"stringValue": "PAGE6_PROBE"},
                }],
                "timeUnixNano": now,
                "asDouble": 11.0,
            }]
        },
    },
    {
        "name": "page6_invalid_unit_metric",
        "description": "Unregistered unit must be dropped",
        "unit": canary,
        "gauge": {
            "dataPoints": [{
                "attributes": [{
                    "key": "operation",
                    "value": {"stringValue": "PAGE6_PROBE"},
                }],
                "timeUnixNano": now,
                "asDouble": 12.0,
            }]
        },
    },
])
payload["resourceMetrics"].append({
    "resource": {
        "attributes": [{
            "key": "service.name",
            "value": {"stringValue": canary},
        }]
    },
    "scopeMetrics": [{
        "scope": {"name": "page6-probe"},
        "metrics": [{
            "name": "page6_service_value_metric",
            "description": "Unregistered service name must drop the datapoint",
            "unit": "1",
            "gauge": {
                "dataPoints": [{
                    "attributes": [{
                        "key": "operation",
                        "value": {"stringValue": "PAGE6_PROBE"},
                    }],
                    "timeUnixNano": now,
                    "asDouble": 13.0,
                }]
            },
        }],
    }],
})
payload_path.write_text(json.dumps(payload), encoding="utf-8")
PY
chmod 600 -- "$METRIC_PAYLOAD"

accepted=0
for attempt in {1..30}; do
    code="$(
        probe_http_request "http://${OTEL_IP}:4318/v1/metrics" \
            --silent --output "$METRIC_RESPONSE" --write-out '%{http_code}' \
            -H 'Content-Type: application/json' \
            --data-binary "@${METRIC_PAYLOAD}" || true
    )"
    if [[ "$code" == 200 ]]; then
        accepted=1
        break
    fi
    sleep 0.2
done
if [[ "$accepted" -ne 1 ]]; then
    if ! grep -Fq -- "$CANARY" "$METRIC_RESPONSE" 2>/dev/null; then
        sed -n '1,20p' "$METRIC_RESPONSE" >&2 || true
    fi
    page6_container_logs "$OTEL_START_ID" > "${PROBE_DIR}/otel-start.log" 2>&1 || true
    if ! grep -Fq -- "$CANARY" "${PROBE_DIR}/otel-start.log"; then
        tail -n 40 "${PROBE_DIR}/otel-start.log" >&2 || true
    fi
    probe_fail "OTEL_REAL_METRIC_NOT_ACCEPTED"
fi

visible=0
for attempt in {1..30}; do
    probe_http_request "http://${OTEL_IP}:9464/metrics" \
        --silent --show-error --output "$METRIC_OUTPUT" || true
    if grep -Fq 'page6_safe_metric' "$METRIC_OUTPUT" 2>/dev/null &&
        grep -Fq 'operation="PAGE6_PROBE"' "$METRIC_OUTPUT" 2>/dev/null &&
        grep -Fq 'provider="deepseek"' "$METRIC_OUTPUT" 2>/dev/null &&
        grep -Fq 'model_name="deepseek-v4-pro"' "$METRIC_OUTPUT" 2>/dev/null &&
        grep -Fq 'feature_code="CONTRACT_REVIEW_PROBE"' "$METRIC_OUTPUT" 2>/dev/null &&
        grep -Fq 'privacy_mode="PRIVATE"' "$METRIC_OUTPUT" 2>/dev/null &&
        grep -Fq 'route_type="EXTERNAL"' "$METRIC_OUTPUT" 2>/dev/null
    then
        visible=1
        break
    fi
    sleep 0.5
done
[[ "$visible" -eq 1 ]] || probe_fail "OTEL_SAFE_METRIC_NOT_EXPORTED"
if grep -Fq -- "$CANARY" "$METRIC_OUTPUT"; then
    probe_fail "OTEL_METRIC_CANARY_LEAK"
fi
if grep -Fq 'page6_exemplar_canary_metric' "$METRIC_OUTPUT"; then
    probe_fail "OTEL_EXEMPLAR_DATAPOINT_NOT_DROPPED"
fi
for dropped_metric in page6_invalid_value_metric page6_invalid_unit_metric +    page6_service_value_metric
do
    if grep -Fq -- "$dropped_metric" "$METRIC_OUTPUT"; then
        probe_fail "OTEL_INVALID_METRIC_NOT_DROPPED"
    fi
done
probe_pass "OTEL_METRIC_VALUE_ALLOWLIST_OK"
for forbidden in url_full http_target db_statement exception_message gen_ai_prompt \
    authorization access_token scope_secret db_password
do
    if grep -Fq -- "$forbidden" "$METRIC_OUTPUT"; then
        probe_fail "OTEL_FORBIDDEN_METRIC_ATTRIBUTE_EXPORTED"
    fi
done
probe_pass "OTEL_REAL_METRIC_REDACTION_OK"

readonly TRACE_CONFIG="${PROBE_DIR}/otel-trace-probe.yml"
readonly TRACE_PAYLOAD="${PROBE_DIR}/trace.json"
readonly TRACE_OUTPUT="${PROBE_DIR}/trace.log"
python3 - "$OTEL_CONFIG" "$TRACE_CONFIG" "$TRACE_PAYLOAD" "$CANARY" <<'PY'
import base64
import json
import pathlib
import sys
import time
import yaml

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
payload_path = pathlib.Path(sys.argv[3])
canary = sys.argv[4]
config = yaml.safe_load(source.read_text(encoding="utf-8"))
config["exporters"]["debug/probe"] = {"verbosity": "detailed"}
config["service"]["pipelines"]["traces"]["exporters"] = ["debug/probe"]
config["service"]["pipelines"]["metrics"]["exporters"] = ["debug/probe"]
config["exporters"].pop("otlp/tempo", None)
target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
now = time.time_ns()
trace_id = "11" * 16
span_id = "22" * 8
payload = {
    "resourceSpans": [{
        "resource": {
            "attributes": [
                {"key": "service.name", "value": {"stringValue": "page6-trace"}},
                {"key": "db_password", "value": {"stringValue": canary}},
                {"key": "url.full", "value": {"stringValue": canary}},
            ]
        },
        "schemaUrl": canary,
        "scopeSpans": [{
            "scope": {
                "name": canary,
                "version": canary,
                "attributes": [
                    {"key": "scope_secret", "value": {"stringValue": canary}}
                ],
            },
            "schemaUrl": canary,
            "spans": [{
                "traceId": trace_id,
                "spanId": span_id,
                "traceState": "page6=sensitive-trace-state",
                "name": canary,
                "kind": 2,
                "startTimeUnixNano": str(now),
                "endTimeUnixNano": str(now + 1_000_000),
                "status": {"message": canary, "code": 2},
                "attributes": [
                    {
                        "key": "feature_code",
                        "value": {"stringValue": "CONTRACT_REVIEW_PROBE"},
                    },
                    {
                        "key": "tenant_id",
                        "value": {"stringValue": "123456789012345678"},
                    },
                    {
                        "key": "request_id",
                        "value": {"stringValue": "11" * 16},
                    },
                    {
                        "key": "task_id",
                        "value": {"stringValue": "1235209075512332288"},
                    },
                    {
                        "key": "run_id",
                        "value": {
                            "stringValue": "33333333-3333-4333-8333-333333333333"
                        },
                    },
                    {
                        "key": "scope_type",
                        "value": {"stringValue": "TENANT"},
                    },
                    {
                        "key": "provider",
                        "value": {"stringValue": "deepseek"},
                    },
                    {
                        "key": "model_name",
                        "value": {"stringValue": "deepseek-v4-pro"},
                    },
                    {
                        "key": "model_pack_id",
                        "value": {"stringValue": "pack_contract_review"},
                    },
                    {
                        "key": "privacy_mode",
                        "value": {"stringValue": "PRIVATE"},
                    },
                    {
                        "key": "route_type",
                        "value": {"stringValue": "EXTERNAL"},
                    },
                    {
                        "key": "http.request.method",
                        "value": {"stringValue": "POST"},
                    },
                    {
                        "key": "http.response.status_code",
                        "value": {"intValue": "200"},
                    },
                    {"key": "url.full", "value": {"stringValue": canary}},
                    {"key": "http.target", "value": {"stringValue": canary}},
                    {"key": "db.statement", "value": {"stringValue": canary}},
                    {"key": "authorization", "value": {"stringValue": canary}},
                    {"key": "cookie", "value": {"stringValue": canary}},
                    {"key": "gen_ai.prompt", "value": {"stringValue": canary}},
                ],
                "events": [{
                    "timeUnixNano": str(now + 500_000),
                    "name": canary,
                    "attributes": [
                        {
                            "key": "exception.type",
                            "value": {"stringValue": "Page6SafeError"},
                        },
                        {
                            "key": "exception.message",
                            "value": {"stringValue": canary},
                        },
                        {
                            "key": "exception.stacktrace",
                            "value": {"stringValue": canary},
                        },
                    ],
                }, {
                    "timeUnixNano": str(now + 600_000),
                    "name": "invalid-allowed-value-event",
                    "attributes": [
                        {
                            "key": "exception.type",
                            "value": {"stringValue": canary},
                        },
                    ],
                }],
            }],
        }],
    }]
}
spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
spans.extend([
    {
        "traceId": "ab" * 16,
        "spanId": "bc" * 8,
        "name": "invalid-allowed-values-span",
        "kind": 2,
        "startTimeUnixNano": str(now + 1_000_000),
        "endTimeUnixNano": str(now + 1_500_000),
        "attributes": [
            {"key": "feature_code", "value": {"stringValue": canary}},
            {"key": "provider", "value": {"stringValue": canary}},
            {"key": "model_name", "value": {"stringValue": canary}},
            {"key": "tenant_id", "value": {"stringValue": canary}},
            {
                "key": "model_pack_id",
                "value": {"stringValue": "pack_should_never_export"},
            },
            {
                "key": "http.request.method",
                "value": {"stringValue": canary},
            },
            {
                "key": "http.response.status_code",
                "value": {"intValue": "999"},
            },
        ],
    },
    {
        "traceId": "33" * 16,
        "spanId": "44" * 8,
        "name": "linked-sensitive-span",
        "kind": 2,
        "startTimeUnixNano": str(now + 2_000_000),
        "endTimeUnixNano": str(now + 3_000_000),
        "attributes": [{
            "key": "feature_code",
            "value": {"stringValue": "page6_link_sensitive_should_drop"},
        }],
        "links": [{
            "traceId": "55" * 16,
            "spanId": "66" * 8,
            "traceState": "page6=sensitive-link-state",
            "attributes": [{
                "key": "contract.content",
                "value": {"stringValue": canary},
            }],
        }],
    },
    {
        "traceId": "77" * 16,
        "spanId": "88" * 8,
        "name": "linked-clean-span",
        "kind": 2,
        "startTimeUnixNano": str(now + 4_000_000),
        "endTimeUnixNano": str(now + 5_000_000),
        "attributes": [{
            "key": "feature_code",
            "value": {"stringValue": "page6_link_safe_should_survive"},
        }],
        "links": [{
            "traceId": "99" * 16,
            "spanId": "aa" * 8,
        }],
    },
])
payload["resourceSpans"].append({
    "resource": {
        "attributes": [{
            "key": "service.name",
            "value": {"stringValue": canary},
        }]
    },
    "scopeSpans": [{
        "scope": {"name": "page6-probe"},
        "spans": [{
            "traceId": "cd" * 16,
            "spanId": "de" * 8,
            "name": "invalid-service-name-span",
            "kind": 2,
            "startTimeUnixNano": str(now + 6_000_000),
            "endTimeUnixNano": str(now + 7_000_000),
            "attributes": [{
                "key": "feature_code",
                "value": {"stringValue": "CONTRACT_REVIEW_PROBE"},
            }, {
                "key": "model_pack_id",
                "value": {"stringValue": "pack_service_should_never_export"},
            }],
        }],
    }],
})
payload_path.write_text(json.dumps(payload), encoding="utf-8")
PY
chmod 0644 -- "$TRACE_CONFIG"
chmod 0600 -- "$TRACE_PAYLOAD"

page6_create_container "$OTEL_TRACE" "$RUN_ID" --network "$NETWORK_ID" \
    -v "${TRACE_CONFIG}:/etc/otelcol-contrib/config.yml:ro" \
    "$OTEL_IMAGE" --config=/etc/otelcol-contrib/config.yml
OTEL_TRACE_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start "$OTEL_TRACE_ID" >/dev/null
trace_running=0
for attempt in {1..20}; do
    page6_validate_container "$OTEL_TRACE_ID"
    trace_state="$(page6_docker inspect --format '{{.State.Status}}' "$OTEL_TRACE_ID")"
    if [[ "$trace_state" == running ]]; then
        trace_running=1
        break
    fi
    [[ "$trace_state" == created || "$trace_state" == restarting ]] || break
    sleep 0.2
done
if [[ "$trace_running" -ne 1 ]]; then
    page6_container_logs "$OTEL_TRACE_ID" >&2 || true
    probe_fail "OTEL_TRACE_FIXED_IMAGE_START_FAILED"
fi
readonly OTEL_TRACE_IP="$(page6_docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$OTEL_TRACE_ID")"
[[ "$OTEL_TRACE_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] ||
    probe_fail "OTEL_TRACE_CONTAINER_IP_INVALID"

trace_accepted=0
for attempt in {1..30}; do
    code="$(
        probe_http_request "http://${OTEL_TRACE_IP}:4318/v1/traces" \
            --silent --output /dev/null --write-out '%{http_code}' \
            -H 'Content-Type: application/json' \
            --data-binary "@${TRACE_PAYLOAD}" || true
    )"
    if [[ "$code" == 200 ]]; then
        trace_accepted=1
        break
    fi
    sleep 0.2
done
[[ "$trace_accepted" -eq 1 ]] || probe_fail "OTEL_REAL_TRACE_NOT_ACCEPTED"

debug_metric_accepted=0
for attempt in {1..30}; do
    code="$(
        probe_http_request "http://${OTEL_TRACE_IP}:4318/v1/metrics" \
            --silent --output /dev/null --write-out '%{http_code}' \
            -H 'Content-Type: application/json' \
            --data-binary "@${METRIC_PAYLOAD}" || true
    )"
    if [[ "$code" == 200 ]]; then
        debug_metric_accepted=1
        break
    fi
    sleep 0.2
done
[[ "$debug_metric_accepted" -eq 1 ]] ||
    probe_fail "OTEL_DEBUG_METRIC_NOT_ACCEPTED"
trace_visible=0
for attempt in {1..30}; do
    page6_container_logs "$OTEL_TRACE_ID" > "$TRACE_OUTPUT" 2>&1 || true
    if grep -Fq 'CONTRACT_REVIEW_PROBE' "$TRACE_OUTPUT" &&
        grep -Fq 'Page6SafeError' "$TRACE_OUTPUT" &&
        grep -Fq 'tenant_id: Str(123456789012345678)' "$TRACE_OUTPUT" &&
        grep -Fq 'provider: Str(deepseek)' "$TRACE_OUTPUT" &&
        grep -Fq 'model_name: Str(deepseek-v4-pro)' "$TRACE_OUTPUT" &&
        grep -Fq 'privacy_mode: Str(PRIVATE)' "$TRACE_OUTPUT" &&
        grep -Fq 'route_type: Str(EXTERNAL)' "$TRACE_OUTPUT" &&
        grep -Fq 'redacted-span' "$TRACE_OUTPUT" &&
        grep -Fq 'redacted-event' "$TRACE_OUTPUT" &&
        grep -Fq 'redacted-scope' "$TRACE_OUTPUT" &&
        grep -Fq 'PAGE6_PROBE' "$TRACE_OUTPUT"
    then
        trace_visible=1
        break
    fi
    sleep 0.5
done
[[ "$trace_visible" -eq 1 ]] || probe_fail "OTEL_SAFE_UNLINKED_TRACE_NOT_EXPORTED"
if grep -Fq -- "$CANARY" "$TRACE_OUTPUT"; then
    probe_fail "OTEL_TRACE_CANARY_LEAK"
fi
for forbidden in url.full http.target db.statement exception.message \
    exception.stacktrace gen_ai.prompt authorization cookie scope_secret db_password \
    sensitive-trace-state sensitive-link-state
do
    if grep -Fq -- "$forbidden" "$TRACE_OUTPUT"; then
        probe_fail "OTEL_FORBIDDEN_TRACE_ATTRIBUTE_EXPORTED"
    fi
done
for dropped_value in pack_should_never_export pack_service_should_never_export
do
    if grep -Fq -- "$dropped_value" "$TRACE_OUTPUT"; then
        probe_fail "OTEL_INVALID_ALLOWED_VALUE_NOT_DROPPED"
    fi
done
probe_pass "OTEL_TRACE_VALUE_ALLOWLIST_OK"
probe_pass "OTEL_METRIC_STRUCTURE_REDACTION_OK"
probe_pass "OTEL_UNLINKED_TRACE_REDACTION_OK"

if grep -Fq 'page6_link_sensitive_should_drop' "$TRACE_OUTPUT" ||
    grep -Fq 'page6_link_safe_should_survive' "$TRACE_OUTPUT"
then
    probe_fail "OTEL_LINKED_SPAN_FAIL_CLOSED_FILTER_BYPASSED"
fi
probe_pass "OTEL_LINKED_SPAN_FAIL_CLOSED_OK"

probe_pass "OTEL_SPAN_LINKS_DISABLED_OK"

red_status=0

if [[ -z "$ALLOY_IMAGE" ]]; then
    probe_fail "ALLOY_APPROVED_IMAGE_REFERENCE_MISSING" || true
    red_status=1
elif ! page6_validate_approved_image_reference "$ALLOY_IMAGE" "$ALLOY_REPOSITORY_TAG"
then
    probe_fail "ALLOY_APPROVED_IMAGE_REFERENCE_INVALID" || true
    red_status=1
elif ! page6_validate_local_image_reference "$ALLOY_IMAGE" "$ALLOY_REPOSITORY_TAG"; then
    probe_fail "ALLOY_APPROVED_IMAGE_LOCAL_CONSISTENCY_INVALID" || true
    red_status=1
else
    page6_create_container "$ALLOY_VALIDATE" "$RUN_ID" --network none \
        -v "${ALLOY_CONFIG}:/etc/alloy/config.alloy:ro" \
        "$ALLOY_IMAGE" fmt --test /etc/alloy/config.alloy
    ALLOY_VALIDATE_ID="$PAGE6_LAST_CONTAINER_ID"
    if page6_docker start -a "$ALLOY_VALIDATE_ID" >/dev/null; then
        probe_pass "ALLOY_FIXED_IMAGE_VALIDATE_OK"
    else
        probe_fail "ALLOY_FIXED_IMAGE_VALIDATE_FAILED" || true
        red_status=1
    fi
fi

if [[ "$red_status" -ne 0 ]]; then
    exit 1
fi
