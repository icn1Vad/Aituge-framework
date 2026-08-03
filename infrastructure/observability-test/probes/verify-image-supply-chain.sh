#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

readonly CADDY_REPOSITORY_TAG="caddy:2.11.4-alpine"
readonly CADDY_IMAGE="${CADDY_REPOSITORY_TAG}@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
readonly OTEL_REPOSITORY_TAG="otel/opentelemetry-collector-contrib:0.153.0"
readonly OTEL_IMAGE="${OTEL_REPOSITORY_TAG}@sha256:666fb40ee1391aa9f0eddb06ea143ffe215d731ff02cf13ce4c2f44f2a3bf89a"
readonly PROMETHEUS_REPOSITORY_TAG="prom/prometheus:v3.13.0"
readonly PROMETHEUS_IMAGE="${PROMETHEUS_REPOSITORY_TAG}@sha256:b96d6068885d3045ae74e149890aa574948f6fcaa6005b82171c6b2be7947998"
readonly LOKI_IMAGE="grafana/loki:3.7.2@sha256:191d4fdfb7264f16989f0a57f320872620a5a7c2ceeec6229212c4190ec49b86"
readonly ALLOY_IMAGE="grafana/alloy:v1.18.0@sha256:491b0578c04983fd54fe99b587b6fab4404dc46d0dc16677bd6b00cc1140b308"
readonly TEMPO_IMAGE="grafana/tempo:2.10.5@sha256:ee21727732c7a7199cb71c3eee9153bbf23f9b0b87619f0555a0cf21a67f1a33"
readonly ALERTMANAGER_IMAGE="prom/alertmanager:v0.32.1@sha256:51a825c2a40acc3e338fdd00d622e01ec090f72be2b3ea46be0839cd47a4d286"
readonly GRAFANA_IMAGE="grafana/grafana:13.1.0@sha256:121a7a9ece6dc10b969f1f96eed64b4f07dfac0d0b8abc070f7cb83bbde86f63"
STATUS=0

is_exact_approved_reference() {
    [[ "${1:-}" == "${2:-}" ]]
}

verify_fixed_image() {
    local component="$1"
    local image_reference="$2"
    local repository_tag="$3"
    if page6_validate_local_image_reference "$image_reference" "$repository_tag"; then
        probe_pass "${component}_APPROVED_IMAGE_DIGEST_OK"
    else
        probe_fail "${component}_APPROVED_IMAGE_DIGEST_INVALID" || true
        STATUS=1
    fi
}

verify_required_image() {
    local component="$1"
    local variable_name="$2"
    local approved_reference="$3"
    local image_reference="${!variable_name:-}"
    local repository_tag="${approved_reference%@*}"
    if [[ -z "$image_reference" ]]; then
        probe_fail "${component}_APPROVED_IMAGE_REFERENCE_MISSING" || true
        STATUS=1
        return
    fi
    if ! is_exact_approved_reference "$image_reference" "$approved_reference"; then
        probe_fail "${component}_APPROVED_IMAGE_DIGEST_MISMATCH" || true
        STATUS=1
        return
    fi
    if ! page6_validate_approved_image_reference "$approved_reference" "$repository_tag"; then
        probe_fail "${component}_APPROVED_IMAGE_REFERENCE_INVALID" || true
        STATUS=1
        return
    fi
    if ! page6_validate_local_image_reference "$image_reference" "$repository_tag"; then
        probe_fail "${component}_APPROVED_IMAGE_LOCAL_CONSISTENCY_INVALID" || true
        STATUS=1
        return
    fi
    probe_pass "${component}_APPROVED_IMAGE_DIGEST_OK"
}

page6_verify_docker_daemon || probe_fail "IMAGE_SUPPLY_CHAIN_DOCKER_TRUST_REJECTED"

if page6_validate_approved_image_reference "$CADDY_REPOSITORY_TAG" "$CADDY_REPOSITORY_TAG" >/dev/null 2>&1; then
    probe_fail "MUTABLE_IMAGE_TAG_ACCEPTED"
fi
if page6_validate_approved_image_reference "unapproved/caddy:2.11.4-alpine@${CADDY_IMAGE#*@}" "$CADDY_REPOSITORY_TAG" >/dev/null 2>&1; then
    probe_fail "UNAPPROVED_IMAGE_REPOSITORY_ACCEPTED"
fi
if page6_validate_approved_image_reference "${CADDY_IMAGE}@${CADDY_IMAGE#*@}" "$CADDY_REPOSITORY_TAG" >/dev/null 2>&1; then
    probe_fail "AMBIGUOUS_IMAGE_DIGEST_ACCEPTED"
fi
probe_pass "IMAGE_REFERENCE_POLICY_NEGATIVE_TEST_OK"

if is_exact_approved_reference "grafana/loki:3.7.2@sha256:$(printf '0%.0s' {1..64})" "$LOKI_IMAGE"; then
    probe_fail "APPROVED_IMAGE_WRONG_DIGEST_ACCEPTED"
fi
probe_pass "IMAGE_EXACT_DIGEST_POLICY_NEGATIVE_TEST_OK"

verify_fixed_image CADDY "$CADDY_IMAGE" "$CADDY_REPOSITORY_TAG"
verify_fixed_image OTEL "$OTEL_IMAGE" "$OTEL_REPOSITORY_TAG"
verify_fixed_image PROMETHEUS "$PROMETHEUS_IMAGE" "$PROMETHEUS_REPOSITORY_TAG"

verify_required_image LOKI CONTRACT_REVIEW_DEV_LOKI_IMAGE "$LOKI_IMAGE"
verify_required_image ALLOY CONTRACT_REVIEW_DEV_ALLOY_IMAGE "$ALLOY_IMAGE"
verify_required_image TEMPO CONTRACT_REVIEW_DEV_TEMPO_IMAGE "$TEMPO_IMAGE"
verify_required_image ALERTMANAGER CONTRACT_REVIEW_DEV_ALERTMANAGER_IMAGE "$ALERTMANAGER_IMAGE"
verify_required_image GRAFANA CONTRACT_REVIEW_DEV_GRAFANA_IMAGE "$GRAFANA_IMAGE"

if (( STATUS != 0 )); then
    exit 1
fi
probe_pass "IMAGE_SUPPLY_CHAIN_VALIDATION_OK"
