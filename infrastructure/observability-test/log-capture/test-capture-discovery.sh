#!/usr/bin/env bash
# Static fake-Docker tests for bounded PROJECT_DISCOVERY capture.
set -Eeuo pipefail
umask 077
readonly HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly TEST_DIR="$(mktemp -d /tmp/capture-discovery.XXXXXX)"
chmod 700 -- "$TEST_DIR"
trap 'rm -rf -- "$TEST_DIR"' EXIT
export CONTRACT_REVIEW_DEV_CAPTURE_MODE=PROJECT_DISCOVERY
export CONTRACT_REVIEW_DEV_CAPTURE_TEST_NETWORKS=ollama-isolated-net
# shellcheck source=capture-test-container-logs.sh
source "${HERE}/capture-test-container-logs.sh"

readonly ID="$(printf 'a%.0s' {1..64})"
readonly IMAGE="sha256:$(printf 'b%.0s' {1..64})"
readonly LEGACY_ID="$(printf 'c%.0s' {1..64})"
readonly VALID="${ID}|/contract-review-new-feature-dev-1|contract-review-code-dev||||new-feature|${IMAGE}|true|contract-review-code-dev-app,ai-model-runtime-net,"
readonly FORMAL="${ID}|/contract-review-new-feature-dev-1|contract-review-code-dev||||new-feature|${IMAGE}|true|contract-review-code-dev-app,agent-internal,"

fail() { printf '%s\n' "$1" >&2; exit 1; }
expect_reject() { "$@" && fail "CAPTURE_DISCOVERY_TEST_UNEXPECTED_ACCEPT" || true; }

# Fake Docker: list output is bounded to a full ID, and no Docker binary is invoked.
page6_verify_docker_daemon() { :; }
FAKE_DOCKER_FAIL=0
page6_docker() {
    (( FAKE_DOCKER_FAIL == 0 )) || return 1
    case "$1" in
        ps) printf '%s\n' "$ID" ;;
        *) fail "CAPTURE_DISCOVERY_TEST_UNEXPECTED_DOCKER_COMMAND" ;;
    esac
}
[[ "$(discovery_container_ids | sort -u)" == "$ID" ]] || fail "CAPTURE_DISCOVERY_TEST_LIST_FAILED"
CAPTURE_PIDS["$ID"]=12345
FAKE_DOCKER_FAIL=1
if reconcile >/dev/null 2>&1; then fail "CAPTURE_DISCOVERY_TEST_LIST_FAILURE_ACCEPTED"; fi
[[ "${CAPTURE_PIDS[$ID]}" == 12345 ]] || fail "CAPTURE_DISCOVERY_TEST_LIST_FAILURE_STOPPED_WORKER"
unset 'CAPTURE_PIDS[$ID]'
FAKE_DOCKER_FAIL=0

# Docker's bare index emits the literal <no value> for a missing label. The
# inspect template must emit an empty field instead, because frontnew-test is
# deliberately pinned as a legacy source without Compose/observability labels.
readonly LEGACY_METADATA="${LEGACY_ID}|/frontnew-test||||||${IMAGE}|true|contract-review-code-dev-agent-internal,"
page6_docker() {
    case "$1" in
        inspect)
            local label
            for label in com.docker.compose.project com.aituge.environment \
                com.aituge.observability.scope com.aituge.observability.capture \
                com.docker.compose.service; do
                [[ "$*" == *"{{with index .Config.Labels \"${label}\"}}{{.}}{{end}}"* ]] || \
                    fail "CAPTURE_DISCOVERY_TEST_MISSING_LABEL_TEMPLATE_NOT_EMPTY"
            done
            printf '%s\n' "$LEGACY_METADATA"
            ;;
        *) fail "CAPTURE_DISCOVERY_TEST_UNEXPECTED_DOCKER_COMMAND" ;;
    esac
}
[[ "$(inspect_candidate "$LEGACY_ID")" == "$LEGACY_METADATA" ]] || \
    fail "CAPTURE_DISCOVERY_TEST_LEGACY_EMPTY_LABEL_METADATA_INVALID"

discovery_candidate_allowed "$ID" contract-review-new-feature-dev-1 contract-review-code-dev new-feature "$IMAGE" true 'contract-review-code-dev-app,ai-model-runtime-net,' || fail "CAPTURE_DISCOVERY_TEST_NEW_SERVICE_REJECTED"
[[ "$SELECTED_SERVICE" == new-feature && "$SELECTED_STREAM" == APPLICATION ]] || fail "CAPTURE_DISCOVERY_TEST_STREAM_INVALID"
expect_reject discovery_candidate_allowed "$ID" contract-review-new-feature-dev-1 contract-review-code-dev new-feature "$IMAGE" false 'contract-review-code-dev-app,'
expect_reject discovery_candidate_allowed "$ID" contract-review-new-feature-dev-1 contract-review-code-dev new-feature "$IMAGE" true 'ai-model-runtime-net,'
expect_reject discovery_candidate_allowed "$ID" contract-review-new-feature-dev-1 contract-review-code-dev new-feature "$IMAGE" true 'contract-review-code-dev-app,agent-internal,'
discovery_candidate_allowed "$ID" contract-review-resume-screening-dev-1 contract-review-code-dev resume-screening "$IMAGE" true 'ollama-isolated-net,' || fail "CAPTURE_DISCOVERY_TEST_REGISTERED_NETWORK_REJECTED"

# Strict selectors remain available for fixed legacy sources such as frontnew-test.
register_selector "$LEGACY_ID" frontnew-test STANDALONE frontnew-test "$OBSERVABILITY_SCOPE" APPLICATION "$IMAGE" LEGACY_TEST_PINNED
candidate_allowed "$LEGACY_ID" frontnew-test '' '' '' '' '' "$IMAGE" true 'contract-review-code-dev-agent-internal,' || fail "CAPTURE_DISCOVERY_TEST_STRICT_REGRESSION"
expect_reject candidate_allowed "$LEGACY_ID" frontnew-test '' '' '' '' '' "$IMAGE" true ''

# In PROJECT_DISCOVERY, a loaded selector wins over discovery for the same full ID.
SELECTORS_LOADED=1
[[ -n "${SELECTOR_NAME[$LEGACY_ID]}" ]] || fail "CAPTURE_DISCOVERY_TEST_UNION_SELECTOR_MISSING"
candidate_allowed "$LEGACY_ID" frontnew-test '' '' '' '' '' "$IMAGE" true 'contract-review-code-dev-agent-internal,' || fail "CAPTURE_DISCOVERY_TEST_UNION_SELECTOR_PRIORITY"

# The worker must reject a fact change between initial inspect and the last pre-log inspect.
readonly INSPECT_COUNTER="${TEST_DIR}/inspect-counter"
printf '0\n' > "$INSPECT_COUNTER"
inspect_candidate() {
    local count
    count="$(cat "$INSPECT_COUNTER")"
    count=$((count + 1))
    printf '%s\n' "$count" > "$INSPECT_COUNTER"
    if (( count == 1 )); then printf '%s\n' "$VALID"; else printf '%s\n' "$FORMAL"; fi
}
if capture_one "$ID" new-feature APPLICATION "$IMAGE" >/dev/null 2>"${TEST_DIR}/capture-discovery-test.err"; then
    fail "CAPTURE_DISCOVERY_TEST_REINSPECT_DRIFT_ACCEPTED"
fi
grep -Fxq CAPTURE_DISCOVERY_FACT_RECHECK_REJECTED "${TEST_DIR}/capture-discovery-test.err" || fail "CAPTURE_DISCOVERY_TEST_REINSPECT_CODE_INVALID"
rm -f -- "${TEST_DIR}/capture-discovery-test.err" "$INSPECT_COUNTER"
# A legacy degraded marker is sticky across reconciles and only emits once.
SELECTED_DEGRADED=1
update_legacy_degraded_status "$LEGACY_ID" 2>"${TEST_DIR}/legacy-first.err"
update_legacy_degraded_status "$LEGACY_ID" 2>"${TEST_DIR}/legacy-second.err"
[[ "${SELECTOR_DEGRADED_REPORTED[$LEGACY_ID]:-}" == 1 ]] || fail "CAPTURE_DISCOVERY_TEST_LEGACY_MARKER_CLEARED"
grep -Fxq CAPTURE_SOURCE_STATUS_INCOMPLETE_LEGACY_SELECTOR "${TEST_DIR}/legacy-first.err" || fail "CAPTURE_DISCOVERY_TEST_LEGACY_FIRST_NOTICE_MISSING"
[[ ! -s "${TEST_DIR}/legacy-second.err" ]] || fail "CAPTURE_DISCOVERY_TEST_LEGACY_NOTICE_REPEATED"
SELECTED_DEGRADED=0
update_legacy_degraded_status "$LEGACY_ID"
[[ -z "${SELECTOR_DEGRADED_REPORTED[$LEGACY_ID]:-}" ]] || fail "CAPTURE_DISCOVERY_TEST_LEGACY_MARKER_NOT_CLEARED"

# Deployment allowlist validation is fail-closed before any Docker command.
if CONTRACT_REVIEW_DEV_CAPTURE_TEST_NETWORKS=ai-model-runtime-net /usr/bin/bash -c 'source "$1"; validate_extra_test_networks' bash "${HERE}/capture-test-container-logs.sh" >/dev/null 2>&1; then
    fail "CAPTURE_DISCOVERY_TEST_SHARED_NETWORK_ALLOWLIST_ACCEPTED"
fi
if CONTRACT_REVIEW_DEV_CAPTURE_TEST_NETWORKS=ollama-isolated-net,ollama-isolated-net /usr/bin/bash -c 'source "$1"; validate_extra_test_networks' bash "${HERE}/capture-test-container-logs.sh" >/dev/null 2>&1; then
    fail "CAPTURE_DISCOVERY_TEST_DUPLICATE_NETWORK_ALLOWLIST_ACCEPTED"
fi
for malformed in ',ollama-isolated-net' 'ollama-isolated-net,' 'ollama-isolated-net,,contract-review-code-dev-app'; do
    if CONTRACT_REVIEW_DEV_CAPTURE_TEST_NETWORKS="$malformed" /usr/bin/bash -c 'source "$1"; validate_extra_test_networks' bash "${HERE}/capture-test-container-logs.sh" >/dev/null 2>&1; then
        fail "CAPTURE_DISCOVERY_TEST_MALFORMED_NETWORK_ALLOWLIST_ACCEPTED"
    fi
done
printf '%s\n' CAPTURE_DISCOVERY_STATIC_TEST_OK
