#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
readonly REPO_ROOT="$(repo_root_from_probe)"
readonly CADDYFILE="${REPO_ROOT}/infrastructure/observability-test/gateway/Caddyfile"
readonly IMAGE="caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
readonly RUN_ID="$(page6_new_run_id)"
readonly NETWORK="contract-review-code-dev-page6-gateway-network-${RUN_ID}"
readonly UPSTREAM="contract-review-code-dev-page6-upstream-${RUN_ID}"
readonly GATEWAY="contract-review-code-dev-page6-gateway-${RUN_ID}"
readonly VALIDATOR="contract-review-code-dev-page6-caddy-validate-${RUN_ID}"
NETWORK_ID=
UPSTREAM_ID=
GATEWAY_ID=
VALIDATOR_ID=

create_probe_dir

cleanup() {
    local rc=0
    page6_cleanup_registered || rc=1
    cleanup_probe_dir
    return "$rc"
}
probe_install_cleanup_traps cleanup

page6_verify_docker_daemon || probe_fail "GATEWAY_DOCKER_TRUST_REJECTED"
page6_validate_local_image_reference "$IMAGE" "caddy:2.11.4-alpine" ||
    probe_fail "GATEWAY_APPROVED_IMAGE_DIGEST_INVALID"
page6_create_network "$NETWORK" "$RUN_ID" --internal
NETWORK_ID="$PAGE6_LAST_NETWORK_ID"

page6_create_container "$VALIDATOR" "$RUN_ID" --network "$NETWORK_ID" \
    -v "${CADDYFILE}:/etc/caddy/Caddyfile:ro" --entrypoint caddy \
    "$IMAGE" validate --config /etc/caddy/Caddyfile
VALIDATOR_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start -a "$VALIDATOR_ID" >/dev/null || probe_fail "GATEWAY_CADDYFILE_VALIDATION_FAILED"

page6_create_container "$UPSTREAM" "$RUN_ID" --network "$NETWORK_ID" \
    --network-alias java --entrypoint caddy "$IMAGE" \
    respond --listen :18000 --status 204
UPSTREAM_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start "$UPSTREAM_ID" >/dev/null

page6_create_container "$GATEWAY" "$RUN_ID" --network "$NETWORK_ID" \
    -v "${CADDYFILE}:/etc/caddy/Caddyfile:ro" --entrypoint caddy \
    "$IMAGE" run --config /etc/caddy/Caddyfile
GATEWAY_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start "$GATEWAY_ID" >/dev/null

page6_validate_container "$GATEWAY_ID"
readonly GATEWAY_IP="$(page6_docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$GATEWAY_ID")"
[[ "$GATEWAY_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] ||     probe_fail "GATEWAY_CONTAINER_IP_INVALID"

readonly SENTINEL="$(python3 -c 'import secrets; print("PAGE6_" + secrets.token_hex(24))')"
readonly ENCODED_SENTINEL="$(
    python3 -c 'import sys; print("".join(f"%{byte:02X}" for byte in sys.argv[1].encode()))' "$SENTINEL"
)"
readonly MIXED_SENTINEL="$(
    python3 -c 'import sys; print(sys.argv[1].swapcase())' "$SENTINEL"
)"
readonly LONG_LOCATOR="$(
    python3 -c 'import sys; print(sys.argv[1] + ("L" * 4096))' "$MIXED_SENTINEL"
)"
readonly QUERY="CuRsOr=${SENTINEL}&cursor=${SENTINEL}&cursor=${SENTINEL}%2Fduplicate&%63%75%72%73%6F%72=${ENCODED_SENTINEL}&unknownField=${SENTINEL}&=${SENTINEL};semicolonKey=${SENTINEL}&emptyValue="
readonly CURL_HOME="${PROBE_DIR}/curl-home"
mkdir -m 700 -- "$CURL_HOME"
printf 'header = "X-Curlrc-Injected: %s"\n' "$SENTINEL" > "$CURL_HOME/.curlrc"
chmod 600 -- "$CURL_HOME/.curlrc"

if probe_http_request "file:///etc/passwd" >/dev/null 2>&1; then
    probe_fail "GATEWAY_NON_HTTP_URL_ACCEPTED"
fi
if probe_http_request "http://127.0.0.1/" --location >/dev/null 2>&1; then
    probe_fail "GATEWAY_REDIRECT_OVERRIDE_ACCEPTED"
fi

ready=0
for attempt in {1..30}; do
    code="$(
        HOME="$CURL_HOME" probe_http_request \
            "http://${GATEWAY_IP}:80/monitor/observability/tasks?${QUERY}" \
            --silent --output /dev/null --write-out '%{http_code}' \
            -H "Authorization: Bearer ${SENTINEL}" \
            -H "Cookie: session=${SENTINEL}" \
            -H "X-Observability-Access-Context: ${SENTINEL}" \
            -H "X-Observability-Access-Session: ${SENTINEL}" \
            -H "X-Observability-Access-Session-Id: ${SENTINEL}" \
            -H "X-Observability-AccessSessionId: ${SENTINEL}" \
            -H "X-Observability-Access-Reason: ${SENTINEL}" \
            -H "X-Observability-Access-Reason-Code: ${SENTINEL}" \
            -H "X-Observability-Scope: ${SENTINEL}" \
            -H "X-Observability-High-Watermark: ${SENTINEL}" \
            -H "Idempotency-Key: ${SENTINEL}" \
            -H "X-Internal-Service-Token: ${SENTINEL}" \
            -H "DownloadToken: ${SENTINEL}" \
            -H "X-Download-Token: ${SENTINEL}" \
            -H "Access-Token: ${SENTINEL}" \
            -H "Refresh-Token: ${SENTINEL}" \
            -H "X-Api-Key: ${SENTINEL}" \
            -H "Forwarded: for=${SENTINEL}" \
            -H "X-Forwarded-For: ${SENTINEL}" \
            -H "X-Real-IP: ${SENTINEL}" \
            -H "X-Debug-Token: ${SENTINEL}" \
            -H "X-Foo: ${SENTINEL}" \
            -H "User-Agent: ${SENTINEL}" \
            -H "hOsT: ${MIXED_SENTINEL}.example.invalid" \
            --data-binary "body=${SENTINEL}" || true
    )"
    if [[ "$code" == 204 ]]; then
        ready=1
        break
    fi
    sleep 0.2
done
[[ "$ready" -eq 1 ]] || probe_fail "GATEWAY_EXPECTED_UPSTREAM_RESPONSE_MISSING"
HOME="$CURL_HOME" probe_http_request \
    "http://${GATEWAY_IP}:80/monitor/observability/method-canary" \
    --silent --output /dev/null \
    --request "$SENTINEL" \
    -H "hOsT: ${MIXED_SENTINEL}.example.invalid" \
    -H "X-Debug-Token: ${SENTINEL}" \
    -H "User-Agent: ${SENTINEL}" ||
    probe_fail "GATEWAY_METHOD_CANARY_REQUEST_FAILED"

for locator_path in \
    "/MoNiToR/ObSeRvAbIlItY/RuNtImE-LoGs/${MIXED_SENTINEL}" \
    "/monitor/observability/runtime-logs/${ENCODED_SENTINEL}" \
    "/monitor/observability/runtime-logs/${LONG_LOCATOR}"
do
    HOME="$CURL_HOME" probe_http_request \
        "http://${GATEWAY_IP}:80${locator_path}" \
        --silent --output /dev/null \
        -H "Authorization: Bearer ${SENTINEL}" \
        -H "Content-Type: application/octet-stream" \
        --data-binary "body=${SENTINEL}" ||
        probe_fail "GATEWAY_LOCATOR_REQUEST_FAILED"
done
sleep 0.3

readonly ACCESS_LOG="${PROBE_DIR}/caddy.jsonl"
page6_container_logs "$GATEWAY_ID" > "$ACCESS_LOG" 2>&1
python3 - "$ACCESS_LOG" "$SENTINEL" "$ENCODED_SENTINEL" "$MIXED_SENTINEL" <<'PY'
import json
import pathlib
import sys

sentinel = sys.argv[2]
encoded_sentinel = sys.argv[3]
mixed_sentinel = sys.argv[4]
entries = []
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        continue
    if value.get("logger", "").startswith("http.log.access"):
        entries.append(value)
if len(entries) < 4:
    raise SystemExit("GATEWAY_LOCATOR_ACCESS_EVENTS_MISSING")

def leak_path(value, path="root"):
    if isinstance(value, dict):
        for key, child in value.items():
            found = leak_path(child, path + "." + str(key))
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = leak_path(child, path + "." + str(index))
            if found:
                return found
    elif any(
        marker in str(value)
        for marker in (sentinel, encoded_sentinel, mixed_sentinel)
    ):
        return path
    return None

for candidate in entries:
    if (found := leak_path(candidate)):
        raise SystemExit("GATEWAY_CAPABILITY_VALUE_LEAK_AT_" + found)

for entry in entries:
    request = entry.get("request", {})
    if "uri" in request:
        raise SystemExit("GATEWAY_REQUEST_URI_RETAINED")
    if "headers" in request:
        raise SystemExit("GATEWAY_REQUEST_HEADERS_RETAINED")
    if "host" in request:
        raise SystemExit("GATEWAY_REQUEST_HOST_RETAINED")
    if "body" in request:
        raise SystemExit("GATEWAY_REQUEST_BODY_LOGGED")
    if "method" in request:
        raise SystemExit("GATEWAY_REQUEST_METHOD_RETAINED")
    if not isinstance(entry.get("status"), int):
        raise SystemExit("GATEWAY_ACCESS_STATUS_MISSING")
    if not isinstance(entry.get("duration"), (int, float)):
        raise SystemExit("GATEWAY_ACCESS_DURATION_MISSING")
    for forbidden in ("remote_ip", "remote_port", "client_ip"):
        if forbidden in request:
            raise SystemExit("GATEWAY_CLIENT_ADDRESS_RETAINED")
PY

probe_pass "GATEWAY_VALIDATION_OK"
