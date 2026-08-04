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
readonly NETWORK="contract-review-code-dev-page6-sse-network-${RUN_ID}"
readonly UPSTREAM="contract-review-code-dev-page6-sse-upstream-${RUN_ID}"
readonly GATEWAY="contract-review-code-dev-page6-sse-gateway-${RUN_ID}"
NETWORK_ID=
UPSTREAM_ID=
GATEWAY_ID=

create_probe_dir

cleanup() {
    local rc=0
    trap - EXIT INT TERM
    page6_cleanup_registered || rc=1
    cleanup_probe_dir
    return "$rc"
}
probe_install_cleanup_traps cleanup

page6_verify_docker_daemon || probe_fail "SSE_DOCKER_TRUST_REJECTED"
page6_validate_local_image_reference "$IMAGE" "caddy:2.11.4-alpine" ||
    probe_fail "SSE_APPROVED_IMAGE_DIGEST_INVALID"
page6_create_network "$NETWORK" "$RUN_ID" --internal
NETWORK_ID="$PAGE6_LAST_NETWORK_ID"

readonly UPSTREAM_COMMAND='while true; do { printf "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n"; printf "id: one\ndata: first\n\n"; sleep 1; printf "id: two\ndata: second\n\n"; sleep 1; } | nc -l -p 18000; done'
page6_create_container "$UPSTREAM" "$RUN_ID" --network "$NETWORK_ID" --network-alias java \
    --entrypoint /bin/sh "$IMAGE" -c "$UPSTREAM_COMMAND"
UPSTREAM_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start "$UPSTREAM_ID" >/dev/null

page6_create_container "$GATEWAY" "$RUN_ID" --network "$NETWORK_ID" \
    -v "${CADDYFILE}:/etc/caddy/Caddyfile:ro" --entrypoint caddy "$IMAGE" run --config /etc/caddy/Caddyfile
GATEWAY_ID="$PAGE6_LAST_CONTAINER_ID"
page6_docker start "$GATEWAY_ID" >/dev/null

page6_validate_container "$GATEWAY_ID"
readonly GATEWAY_IP="$(page6_docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$GATEWAY_ID")"
[[ "$GATEWAY_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] ||     probe_fail "SSE_CONTAINER_IP_INVALID"

python3 - "$GATEWAY_IP" <<'PY'
import sys
import time
import urllib.error
import urllib.request

host = sys.argv[1]
url = f"http://{host}:80/monitor/observability/runs/run-page6/events/stream"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
last_error = None
response = None
for _ in range(30):
    try:
        response = opener.open(url, timeout=5)
        break
    except (urllib.error.URLError, ConnectionError) as exc:
        last_error = exc
        time.sleep(0.2)
if response is None:
    raise SystemExit("SSE_GATEWAY_NOT_READY")

headers = {key.lower(): value for key, value in response.headers.items()}
cache_control = ",".join(response.headers.get_all("Cache-Control", [])).lower()
if "no-store" not in cache_control or "private" not in cache_control:
    raise SystemExit("SSE_CACHE_CONTROL_INVALID")
if headers.get("x-accel-buffering", "").lower() != "no":
    raise SystemExit("SSE_PROXY_BUFFERING_HEADER_MISSING")
if headers.get("referrer-policy", "").lower() != "no-referrer":
    raise SystemExit("SSE_REFERRER_POLICY_INVALID")
if "authorization" not in headers.get("vary", "").lower():
    raise SystemExit("SSE_VARY_AUTHORIZATION_MISSING")
if "content-encoding" in headers:
    raise SystemExit("SSE_RESPONSE_COMPRESSED")

started = time.monotonic()
first_at = None
second_at = None
deadline = started + 5
while time.monotonic() < deadline:
    line = response.readline()
    if not line:
        break
    if line == b"data: first\n":
        first_at = time.monotonic()
    elif line == b"data: second\n":
        second_at = time.monotonic()
        break
response.close()
if first_at is None or second_at is None:
    raise SystemExit("SSE_EVENT_MISSING")
if first_at - started > 1.5:
    raise SystemExit("SSE_FIRST_EVENT_BUFFERED")
if not 0.5 <= second_at - first_at <= 2.5:
    raise SystemExit("SSE_STREAM_TIMING_INVALID")
PY

grep -Fq 'flush_interval -1' "$CADDYFILE" || probe_fail "SSE_FLUSH_SETTING_MISSING"
grep -Fq 'stream_timeout 30m' "$CADDYFILE" || probe_fail "SSE_TIMEOUT_SETTING_MISSING"
probe_pass "SSE_VALIDATION_OK"
