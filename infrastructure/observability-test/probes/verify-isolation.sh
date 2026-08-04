#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
readonly REPO_ROOT="$(repo_root_from_probe)"
readonly STACK_DIR="${REPO_ROOT}/infrastructure/observability-test"
readonly OBS_COMPOSE="${STACK_DIR}/compose.observability-test.yml"
readonly CAPTURE="${STACK_DIR}/log-capture/capture-test-container-logs.sh"
readonly RUN_ID="$(page6_new_run_id)"
readonly SMOKE_ALLOWED="contract-review-code-dev-page6-capture-allowed-${RUN_ID}"
readonly SMOKE_DENIED="contract-review-code-dev-page6-capture-denied-${RUN_ID}"
readonly SMOKE_NETWORK="contract-review-code-dev-page6-capture-network-${RUN_ID}"
readonly SMOKE_IMAGE="caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
CAPTURE_MANAGER_PID=""
SMOKE_ALLOWED_ID=""
SMOKE_DENIED_ID=""

stop_capture_manager() {
    local pid="$CAPTURE_MANAGER_PID"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    page6_terminate_process_group "$pid" || return 1
    wait "$pid" 2>/dev/null || true
    CAPTURE_MANAGER_PID=""
}

cleanup() {
    local rc=0
    trap - EXIT INT TERM
    stop_capture_manager || rc=1
    page6_cleanup_registered || rc=1
    cleanup_probe_dir
    return "$rc"
}

create_probe_dir
readonly CAPTURE_SELECTOR_FILE="${PROBE_DIR}/capture-selectors"
readonly DUMMY_SELECTOR_ID="$(printf 'a%.0s' {1..64})"
readonly DUMMY_IMAGE_ID="sha256:$(printf 'b%.0s' {1..64})"
printf '%s\n' "${DUMMY_SELECTOR_ID}|contract-review-code-dev-page6-selector-test|contract-review-code-dev|selector-test|contract-review-code-dev|APPLICATION|${DUMMY_IMAGE_ID}|STRICT" > "$CAPTURE_SELECTOR_FILE"
chmod 600 -- "$CAPTURE_SELECTOR_FILE"
probe_install_cleanup_traps cleanup
page6_verify_docker_daemon || probe_fail "ISOLATION_DOCKER_TRUST_REJECTED"
for key in COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR COMPOSE_ENV_FILES; do
    [[ ! -v $key ]] || probe_fail "ISOLATION_COMPOSE_ENVIRONMENT_OVERRIDE_REJECTED"
done

if grep -ERq     '/var/run/docker\.sock|/var/lib/docker/containers|name:[[:space:]]*[^#]*formal'     "$OBS_COMPOSE"     "${STACK_DIR}/alloy" "${STACK_DIR}/loki" "${STACK_DIR}/tempo"     "${STACK_DIR}/otel-collector" "${STACK_DIR}/prometheus"     "${STACK_DIR}/alertmanager" "${STACK_DIR}/grafana"; then
    probe_fail "ISOLATION_FORBIDDEN_STATIC_RESOURCE"
fi

"$CAPTURE" --self-test >/dev/null

readonly WORKER_ERROR="${PROBE_DIR}/worker-error.txt"
set +e
"$CAPTURE" --follow-worker > /dev/null 2>"$WORKER_ERROR"
worker_status=$?
set -e
if [[ "$worker_status" -ne 64 ]] || [[ "$(cat -- "$WORKER_ERROR")" != "CAPTURE_WORKER_ARGUMENT_INVALID" ]]; then
    probe_fail "ISOLATION_CAPTURE_WORKER_ARGUMENT_CONTRACT_INVALID"
fi

if env -u CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT \
    CONTRACT_REVIEW_DEV_CAPTURE_MODE=STRICT_SELECTORS \
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    "$CAPTURE" --check >/dev/null 2>&1; then
    probe_fail "ISOLATION_CAPTURE_MISSING_ROOT_ACCEPTED"
fi
if CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT=/tmp \
    CONTRACT_REVIEW_DEV_CAPTURE_MODE=STRICT_SELECTORS \
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    "$CAPTURE" --check >/dev/null 2>&1; then
    probe_fail "ISOLATION_CAPTURE_OUTSIDE_ROOT_ACCEPTED"
fi

readonly CAPTURE_ROOT="${PROBE_DIR}/capture"
readonly CAPTURE_LINK="${PROBE_DIR}/capture-link"
mkdir -m 700 -- "$CAPTURE_ROOT"
ln -s -- "$CAPTURE_ROOT" "$CAPTURE_LINK"
if CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$CAPTURE_LINK" \
    CONTRACT_REVIEW_DEV_CAPTURE_MODE=STRICT_SELECTORS \
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    "$CAPTURE" --check >/dev/null 2>&1; then
    probe_fail "ISOLATION_CAPTURE_SYMLINK_ACCEPTED"
fi
CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$CAPTURE_ROOT" \
    CONTRACT_REVIEW_DEV_CAPTURE_MODE=STRICT_SELECTORS \
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    "$CAPTURE" --check >/dev/null

readonly FILTER_ERROR="${PROBE_DIR}/filter-error.txt"
printf '%s\n' "invalid-selector" > "$CAPTURE_SELECTOR_FILE"
set +e
CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$CAPTURE_ROOT" \
    CONTRACT_REVIEW_DEV_CAPTURE_MODE=STRICT_SELECTORS \
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    "$CAPTURE" --check >/dev/null 2>"$FILTER_ERROR"
filter_status=$?
set -e
if [[ "$filter_status" -ne 1 ]] ||
    [[ "$(tail -n 1 -- "$FILTER_ERROR")" != "CAPTURE_SELECTOR_MANIFEST_INVALID" ]]; then
    probe_fail "ISOLATION_CAPTURE_SELECTOR_MANIFEST_CONTRACT_INVALID"
fi
printf '%s\n' "${DUMMY_SELECTOR_ID}|contract-review-code-dev-page6-selector-test|contract-review-code-dev|selector-test|contract-review-code-dev|APPLICATION|${DUMMY_IMAGE_ID}|STRICT" > "$CAPTURE_SELECTOR_FILE"

page6_validate_local_image_reference "$SMOKE_IMAGE" "caddy:2.11.4-alpine" ||
    probe_fail "ISOLATION_CAPTURE_APPROVED_IMAGE_DIGEST_INVALID"
page6_create_network "$SMOKE_NETWORK" "$RUN_ID"
[[ "$PAGE6_LAST_NETWORK_ID" =~ ^[0-9a-f]{64}$ ]] ||
    probe_fail "ISOLATION_CAPTURE_SMOKE_NETWORK_ID_INVALID"
page6_validate_network "$PAGE6_LAST_NETWORK_ID"
page6_create_container "$SMOKE_ALLOWED" "$RUN_ID" --network "$SMOKE_NETWORK" \
    --label com.aituge.observability.capture=enabled \
    --label com.docker.compose.service=page6-capture-allowed \
    --entrypoint /bin/sh "$SMOKE_IMAGE" -c 'while true; do printf "%s\n" PAGE6_CAPTURE_ALLOWED; sleep 1; done'
SMOKE_ALLOWED_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start "$SMOKE_ALLOWED_ID" >/dev/null
page6_create_container "$SMOKE_DENIED" "$RUN_ID" --network "$SMOKE_NETWORK" \
    --label com.docker.compose.service=page6-capture-denied \
    --entrypoint /bin/sh "$SMOKE_IMAGE" -c 'while true; do printf "%s\n" PAGE6_CAPTURE_DENIED; sleep 1; done'
SMOKE_DENIED_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start "$SMOKE_DENIED_ID" >/dev/null
page6_validate_container "$SMOKE_ALLOWED_ID"
page6_validate_container "$SMOKE_DENIED_ID"
[[ "$SMOKE_ALLOWED_ID" =~ ^[0-9a-f]{64}$ ]] ||     probe_fail "ISOLATION_CAPTURE_SMOKE_ALLOWED_ID_INVALID"
[[ "$SMOKE_DENIED_ID" =~ ^[0-9a-f]{64}$ ]] ||     probe_fail "ISOLATION_CAPTURE_SMOKE_DENIED_ID_INVALID"
readonly SMOKE_ALLOWED_IMAGE_ID="$(page6_docker inspect --type container --format '{{.Image}}' "$SMOKE_ALLOWED_ID")"
[[ "$SMOKE_ALLOWED_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    probe_fail "ISOLATION_CAPTURE_SMOKE_IMAGE_ID_INVALID"
printf '%s\n' "${SMOKE_ALLOWED_ID}|${SMOKE_ALLOWED}|contract-review-code-dev|page6-capture-allowed|contract-review-code-dev|APPLICATION|${SMOKE_ALLOWED_IMAGE_ID}|STRICT" > "$CAPTURE_SELECTOR_FILE"
chmod 600 -- "$CAPTURE_SELECTOR_FILE"

CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$CAPTURE_ROOT" \
    CONTRACT_REVIEW_DEV_CAPTURE_MODE=STRICT_SELECTORS \
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    /usr/bin/setsid /usr/bin/bash "$CAPTURE" \
    >"${PROBE_DIR}/capture-manager.out" 2>"${PROBE_DIR}/capture-manager.err" &
CAPTURE_MANAGER_PID=$!
sleep 0.1
page6_capture_process_identity "$CAPTURE_MANAGER_PID" || probe_fail "ISOLATION_CAPTURE_MANAGER_IDENTITY_REJECTED"

readonly SMOKE_ALLOWED_LOG="${CAPTURE_ROOT}/${SMOKE_ALLOWED_ID}.jsonl"
for attempt in {1..15}; do
    [[ -s "$SMOKE_ALLOWED_LOG" ]] && break
    sleep 1
done
if [[ ! -s "$SMOKE_ALLOWED_LOG" ]]; then
    sed -n '1,120p' -- "${PROBE_DIR}/capture-manager.err" >&2 || true
    probe_fail "ISOLATION_CAPTURE_SMOKE_OUTPUT_MISSING"
fi
sleep 1
python3 - "$CAPTURE_ROOT" "$SMOKE_ALLOWED_ID" "$SMOKE_DENIED_ID" <<'PY'
import json
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
allowed_id = sys.argv[2]
denied_id = sys.argv[3]
files = sorted(path.name for path in root.glob("*.jsonl"))
if files != [f"{allowed_id}.jsonl"]:
    raise SystemExit("ISOLATION_CAPTURE_SMOKE_SELECTED_UNEXPECTED_CONTAINER")
output = root / files[0]
if stat.S_IMODE(output.stat().st_mode) != 0o600:
    raise SystemExit("ISOLATION_CAPTURE_SMOKE_MODE_INVALID")
records = [
    json.loads(line)
    for line in output.read_text(encoding="utf-8").splitlines()
]
if not any(
    record.get("service") == "page6-capture-allowed"
    and record.get("stream") == "APPLICATION"
    and record.get("event_code") == "LOG_RECORD_DROPPED_UNSTRUCTURED_TEXT"
    and record.get("message") == "source record removed by fail-safe redaction"
    for record in records
):
    raise SystemExit("ISOLATION_CAPTURE_SMOKE_RECORD_MISSING")
serialized = "\n".join(
    json.dumps(record, sort_keys=True, ensure_ascii=True) for record in records
)
if "PAGE6_CAPTURE_ALLOWED" in serialized or "PAGE6_CAPTURE_DENIED" in serialized:
    raise SystemExit("ISOLATION_CAPTURE_UNSTRUCTURED_SOURCE_LEAKED")
if (root / f"{denied_id}.jsonl").exists():
    raise SystemExit("ISOLATION_CAPTURE_SMOKE_MISSING_SCOPE_SELECTED")
PY
stop_capture_manager

readonly OBSERVABILITY_JSON="${PROBE_DIR}/observability.json"
page6_docker compose --env-file /dev/null -f "$OBS_COMPOSE" \
    config --no-interpolate --format json > "$OBSERVABILITY_JSON"

python3 - "$OBSERVABILITY_JSON" <<'PY'
import json
import pathlib
import sys

config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if config.get("name") != "contract-review-code-dev-observability":
    raise SystemExit("ISOLATION_OBSERVABILITY_PROJECT_INVALID")

expected_services = {
    "loki",
    "alloy",
    "tempo",
    "otel-collector",
    "prometheus",
    "alertmanager",
    "grafana-secret-init",
    "grafana",
}
services = config.get("services", {})
if set(services) != expected_services:
    raise SystemExit("ISOLATION_OBSERVABILITY_SERVICE_SET_INVALID")
if any(service.get("container_name") for service in services.values()):
    raise SystemExit("ISOLATION_OBSERVABILITY_CONTAINER_NAME_FORBIDDEN")

networks = config.get("networks", {})
expected_networks = {
    "observability-ingest": ("contract-review-code-dev-observability-ingest", True, "ingest"),
    "observability-backend": ("contract-review-code-dev-observability-backend", True, "backend"),
    "observability-loopback": ("contract-review-code-dev-observability-loopback", False, "loopback"),
}
if set(networks) != set(expected_networks):
    raise SystemExit("ISOLATION_OBSERVABILITY_NETWORK_SET_INVALID")
for key, (expected_name, expected_internal, expected_role) in expected_networks.items():
    network = networks[key]
    labels = network.get("labels", {})
    if (
        network.get("name") != expected_name
        or network.get("internal") is not expected_internal
        or network.get("external") is True
        or labels.get("com.aituge.environment") != "test"
        or labels.get("com.aituge.stack") != "contract-review-code-dev-observability"
        or labels.get("com.aituge.network.role") != expected_role
    ):
        raise SystemExit("ISOLATION_OBSERVABILITY_NETWORK_CONTRACT_INVALID")

expected_membership = {
    "otel-collector": {"observability-ingest", "observability-backend"},
    "loki": {"observability-backend"},
    "alloy": {"observability-backend"},
    "tempo": {"observability-backend"},
    "prometheus": {"observability-backend"},
    "alertmanager": {"observability-backend"},
    "grafana-secret-init": set(),
    "grafana": {"observability-backend", "observability-loopback"},
}
for name, expected in expected_membership.items():
    if set(services[name].get("networks", {})) != expected:
        raise SystemExit("ISOLATION_OBSERVABILITY_NETWORK_MEMBERSHIP_INVALID")
    labels = services[name].get("labels", {})
    if (
        labels.get("com.aituge.environment") != "test"
        or labels.get("com.aituge.observability.scope") != "contract-review-code-dev"
        or labels.get("com.aituge.observability.capture") != "enabled"
    ):
        raise SystemExit("ISOLATION_OBSERVABILITY_LABELS_INVALID")

serialized = json.dumps(config, sort_keys=True).lower()
for forbidden in (
    "agent-internal",
    "ai-framework-internal",
    "proofspace-network",
    "continew-agent_default",
    "formal",
    "production",
):
    if forbidden.lower() in serialized:
        raise SystemExit("ISOLATION_FORMAL_OR_BUSINESS_REFERENCE")
PY

probe_pass "ISOLATION_VALIDATION_OK"
