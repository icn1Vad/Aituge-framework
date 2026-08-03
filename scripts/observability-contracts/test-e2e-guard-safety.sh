#!/usr/bin/bash -p
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
guard="$repo_root/scripts/observability-contracts/guard-test-target.sh"
work_dir=$(mktemp -d /tmp/obs70-e2e-guard-safety-XXXXXX)
fake_bin="$work_dir/bin"
policy_dir="$work_dir/policy"
policy_file="$policy_dir/formal-port-denylist"
stdout_file="$work_dir/stdout"
stderr_file="$work_dir/stderr"
docker_log="$work_dir/docker.log"
fake_bash_log="$work_dir/fake-bash.log"
bash_env_file="$work_dir/bash-env"
shell_marker="$work_dir/shell-startup.executed"
network_id=$(printf 'b%.0s' {1..64})
gateway_image_id="sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
gateway_id=$(printf 'a%.0s' {1..64})
trusted_daemon_id=3196b392-cce0-4178-a30a-2a9ff44d271c
mkdir -p "$fake_bin" "$policy_dir"
chmod 700 "$work_dir" "$policy_dir"
printf '%s\n' 44443 >"$policy_file"
chmod 600 "$policy_file"

cleanup() {
  case "$work_dir" in
    /tmp/obs70-e2e-guard-safety-*) rm -rf -- "$work_dir" ;;
    *) echo "OBS_E2E_GUARD_SAFETY_CLEANUP_REFUSED" >&2; exit 1 ;;
  esac
}
trap cleanup EXIT INT TERM

cat >"$fake_bin/docker" <<'FAKE_DOCKER'
#!/bin/sh
set -eu
printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
if [ "$1" = context ] && [ "$2" = show ]; then
  printf '%s\n' "$FAKE_DOCKER_CONTEXT"
  exit 0
fi
if [ "$1" = context ] && [ "$2" = inspect ]; then
  printf '%s\n' "$FAKE_DOCKER_ENDPOINT"
  exit 0
fi
if [ "$1" = --context ]; then
  [ "$2" = default ] || exit 90
  shift 2
fi
case "$1" in
  info)
    printf '%s|afs2600151|/var/lib/docker\n' "$FAKE_DOCKER_DAEMON_ID"
    ;;
  network)
    [ "$2" = inspect ] || exit 90
    printf '%s\n' "$FAKE_NETWORK_METADATA"
    ;;
  inspect)
    printf '%s\n' "$FAKE_DOCKER_METADATA"
    ;;
  *)
    exit 90
    ;;
esac
FAKE_DOCKER
chmod 700 "$fake_bin/docker"

cat >"$fake_bin/bash" <<'FAKE_BASH'
#!/bin/sh
set -eu
printf '%s\n' invoked >>"$OBS70_FAKE_BASH_LOG"
printf '%s\n' fake-bash-before-guard >>"$FAKE_DOCKER_LOG"
exit 99
FAKE_BASH
chmod 700 "$fake_bin/bash"
printf '%s\n' \
  'printf "%s\n" bash-env-before-guard >>"$FAKE_DOCKER_LOG"' \
  'printf executed >"$OBS70_SHELL_MARKER"' >"$bash_env_file"
chmod 600 "$bash_env_file"

grep -Fxq 'readonly PAGE6_TRUST_COMMIT="fbce7a11dec66cd7957b6831d0dc104ad4758985"' "$guard"
grep -Fxq 'readonly PAGE6_TRUST_SHA256="15f0d75567c3ff4da4725507ad5e689eeb766da008a6602766386b13006b531f"' "$guard"

common_args=(
  --environment test
  --compose-project contract-review-code-dev
  --confirm contract-review-code-dev
  --allowed-origins 'http://127.0.0.1:18080,http://localhost:18080'
  --gateway-container "$gateway_id"
)

run_guard() {
  local metadata="$1"
  local network_metadata="$2"
  local origin="$3"
  local context="${4:-default}"
  local daemon_id="${5:-$trusted_daemon_id}"
  : >"$docker_log"
  set +e
  env \
    -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY \
    -u DOCKER_CERT_PATH -u DOCKER_CONFIG \
    PATH="$fake_bin:/usr/bin:/bin" \
    BASH_ENV="$bash_env_file" ENV="$bash_env_file" SHELLOPTS=xtrace \
    OBS70_FAKE_BASH_LOG="$fake_bash_log" \
    OBS70_SHELL_MARKER="$shell_marker" \
    OBS_E2E_GUARD_TEST_MODE=SYNTHETIC_NO_NETWORK \
    OBS_E2E_GUARD_TEST_DOCKER_CLI="$fake_bin/docker" \
    OBS_E2E_GUARD_TEST_POLICY_FILE="$policy_file" \
    OBS_E2E_GUARD_TEST_TRUST_ROOT="$work_dir" \
    FAKE_DOCKER_LOG="$docker_log" \
    FAKE_DOCKER_CONTEXT="$context" \
    FAKE_DOCKER_ENDPOINT='unix:///var/run/docker.sock|{}' \
    FAKE_DOCKER_DAEMON_ID="$daemon_id" \
    FAKE_DOCKER_METADATA="$metadata" \
    FAKE_NETWORK_METADATA="$network_metadata" \
    "$guard" "${common_args[@]}" --origin "$origin" \
    >"$stdout_file" 2>"$stderr_file"
  guard_status=$?
  set -e
}

stable_network_metadata="$network_id|contract-review-code-dev-agent-internal|contract-review-code-dev|agent_internal"
stable_metadata="$gateway_id|/contract-review-dev-caddy-1|contract-review-code-dev|test|test-gateway|$gateway_image_id|true|contract-review-code-dev-agent-internal@$network_id,|8080/tcp@0.0.0.0@18080,"
run_guard "$stable_metadata" "$stable_network_metadata" http://127.0.0.1:18080
[[ "$guard_status" -eq 0 ]]
grep -Fq 'OBS_E2E_GUARD_OK' "$stdout_file"
grep -Fq -- "network inspect --format" "$docker_log"
[[ ! -s "$fake_bash_log" ]]
[[ ! -e "$shell_marker" ]]
! grep -Fq 'before-guard' "$docker_log"

run_guard "$stable_metadata" "$stable_network_metadata" http://localhost:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_ORIGIN_HOST_LITERAL_REQUIRED' "$stderr_file"

formal_metadata="$gateway_id|/contract-review-production-caddy-1|contract-review-code-dev|test|test-gateway|$gateway_image_id|true|contract-review-code-dev-agent-internal@$network_id,|8080/tcp@0.0.0.0@18080,"
run_guard "$formal_metadata" "$stable_network_metadata" http://127.0.0.1:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_FORMAL_TARGET_DENIED' "$stderr_file"

changed_id=$(printf 'd%.0s' {1..64})
changed_metadata="$changed_id|/contract-review-dev-caddy-1|contract-review-code-dev|test|test-gateway|$gateway_image_id|true|contract-review-code-dev-agent-internal@$network_id,|8080/tcp@0.0.0.0@18080,"
run_guard "$changed_metadata" "$stable_network_metadata" http://127.0.0.1:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_CONTAINER_ID_MISMATCH' "$stderr_file"

wrong_name_metadata="${stable_metadata/\/contract-review-dev-caddy-1/\/frontnew-test}"
run_guard "$wrong_name_metadata" "$stable_network_metadata" http://127.0.0.1:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_CONTAINER_NAME_MISMATCH' "$stderr_file"

wrong_service_metadata="${stable_metadata/|test-gateway|/|frontnew|}"
run_guard "$wrong_service_metadata" "$stable_network_metadata" http://127.0.0.1:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_CONTAINER_SERVICE_MISMATCH' "$stderr_file"

wrong_image_metadata="${stable_metadata/$gateway_image_id/sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff}"
run_guard "$wrong_image_metadata" "$stable_network_metadata" http://127.0.0.1:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_CONTAINER_IMAGE_MISMATCH' "$stderr_file"

dev_environment_metadata="${stable_metadata/|test|test-gateway|/|dev|test-gateway|}"
run_guard "$dev_environment_metadata" "$stable_network_metadata" http://127.0.0.1:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_CONTAINER_ENVIRONMENT_MISMATCH' "$stderr_file"

foreign_network_id=$(printf 'c%.0s' {1..64})
extra_network_metadata="${stable_metadata/agent-internal@$network_id,/agent-internal@$network_id,foreign-safe@$foreign_network_id,}"
run_guard "$extra_network_metadata" "$stable_network_metadata" http://127.0.0.1:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_NETWORK_SET_MISMATCH' "$stderr_file"

model_network_metadata="${stable_metadata/agent-internal@$network_id,/agent-internal@$network_id,ai-model-runtime-net@$foreign_network_id,}"
run_guard "$model_network_metadata" "$stable_network_metadata" http://127.0.0.1:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_NETWORK_SET_MISMATCH' "$stderr_file"

wrong_network_labels="$network_id|contract-review-code-dev-agent-internal|contract-review-code-dev|default"
run_guard "$stable_metadata" "$wrong_network_labels" http://127.0.0.1:18080
[[ "$guard_status" -ne 0 ]]
grep -Fq 'OBS_E2E_GUARD_NETWORK_IDENTITY_MISMATCH' "$stderr_file"

run_guard "$stable_metadata" "$stable_network_metadata" http://127.0.0.1:18080 changed-context
[[ "$guard_status" -eq 74 ]]
grep -Fq 'OBS_E2E_GUARD_DOCKER_TRUST_DENIED' "$stderr_file"
! grep -Fq -- '--context default inspect --format' "$docker_log"

run_guard "$stable_metadata" "$stable_network_metadata" http://127.0.0.1:18080 default 00000000-0000-0000-0000-000000000000
[[ "$guard_status" -eq 74 ]]
grep -Fq 'OBS_E2E_GUARD_DOCKER_TRUST_DENIED' "$stderr_file"
! grep -Fq -- '--context default inspect --format' "$docker_log"

set +e
DOCKER_HOST=tcp://127.0.0.1:2375 \
  PATH="$fake_bin:/usr/bin:/bin" \
  OBS_E2E_GUARD_TEST_MODE=SYNTHETIC_NO_NETWORK \
  OBS_E2E_GUARD_TEST_DOCKER_CLI="$fake_bin/docker" \
  "$guard" "${common_args[@]}" --origin http://127.0.0.1:18080 \
  >"$stdout_file" 2>"$stderr_file"
guard_status=$?
set -e
[[ "$guard_status" -eq 74 ]]
grep -Fq 'OBS_E2E_GUARD_DOCKER_OVERRIDE_DENIED name=DOCKER_HOST' "$stderr_file"

echo 'OBS_E2E_GUARD_SAFETY_OK context=bound daemon=bound overrides=denied gateway=name-service-image-exact network=single-id-labels-exact additional=denied shell=fixed-two-stage bash-env=ignored path-bash=denied page6=commit-sha256-pinned'
