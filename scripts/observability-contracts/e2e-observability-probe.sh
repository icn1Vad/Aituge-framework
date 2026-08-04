#!/usr/bin/bash -p
set +x
set -euo pipefail
umask 077

readonly OBS_E2E_SAFE_PATH="/usr/bin:/bin"
PATH="$OBS_E2E_SAFE_PATH"
export PATH
unset BASH_ENV ENV CDPATH GLOBIGNORE POSIXLY_CORRECT 2>/dev/null || true
export -n BASHOPTS SHELLOPTS 2>/dev/null || true

for shell_loader_override in LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT; do
  if [[ -v $shell_loader_override ]]; then
    echo "OBS_E2E_PYTHON_LOADER_OVERRIDE_DENIED name=$shell_loader_override" >&2
    exit 80
  fi
done

readonly OBS_E2E_SAFE_STAGE_ARGUMENT="--obs70-probe-safe-bash-stage-4ea2916d"
if [[ "${1:-}" != "$OBS_E2E_SAFE_STAGE_ARGUMENT" ]]; then
  if [[ -L "${BASH_SOURCE[0]}" || ! -f "${BASH_SOURCE[0]}" ]]; then
    echo "OBS_E2E_PROBE_ENTRY_INVALID" >&2
    exit 80
  fi
  entry_script=$(/usr/bin/realpath -e -- "${BASH_SOURCE[0]}") || {
    echo "OBS_E2E_PROBE_ENTRY_INVALID" >&2
    exit 80
  }
  exec /usr/bin/env \
    -u BASH_ENV -u ENV -u SHELLOPTS -u BASHOPTS -u CDPATH \
    -u GLOBIGNORE -u POSIXLY_CORRECT \
    PATH="$OBS_E2E_SAFE_PATH" \
    /usr/bin/bash -p -- "$entry_script" "$OBS_E2E_SAFE_STAGE_ARGUMENT" "$@"
fi
shift

if [[ "${OBS_E2E_RUN:-}" != "I_UNDERSTAND_TEST_ONLY" ]]; then
  echo "OBS_E2E_DEFAULT_REFUSAL" >&2
  exit 80
fi
python_loader_overrides=(
  PYTHONPATH
  PYTHONHOME
  PYTHONSTARTUP
  PYTHONINSPECT
  PYTHONUSERBASE
  LD_PRELOAD
  LD_LIBRARY_PATH
  LD_AUDIT
)
for override_name in "${python_loader_overrides[@]}"; do
  if [[ -v $override_name ]]; then
    echo "OBS_E2E_PYTHON_LOADER_OVERRIDE_DENIED name=$override_name" >&2
    exit 80
  fi
done


synthetic_mode=false
case "${OBS_E2E_PROBE_TEST_MODE:-}" in
  "") ;;
  SYNTHETIC_NO_NETWORK) synthetic_mode=true ;;
  *) echo "OBS_E2E_PROBE_TEST_MODE_INVALID" >&2; exit 80 ;;
esac

required=(
  OBS_E2E_ENVIRONMENT
  OBS_E2E_COMPOSE_PROJECT
  OBS_E2E_CONFIRM
  OBS_E2E_ORIGIN
  OBS_E2E_ALLOWED_ORIGINS
  OBS_E2E_BEARER_FILE
  OBS_E2E_SECRETS_ROOT
  OBS_E2E_GATEWAY_CONTAINER
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "OBS_E2E_REQUIRED_SETTING_MISSING name=$name" >&2
    exit 81
  fi
done
readonly REAL_SECRETS_ROOT="/home/aituge/contract-review-code-dev-test-private/secrets"
secret_policy=synthetic
expected_real_secrets_root=""
if [[ "$synthetic_mode" != true ]]; then
  secret_policy=real
  if [[ -z "${CONTRACT_REVIEW_DEV_TEST_SECRETS_DIR:-}" ]]; then
    echo "OBS_E2E_REQUIRED_SETTING_MISSING name=CONTRACT_REVIEW_DEV_TEST_SECRETS_DIR" >&2
    exit 81
  fi
  if [[ "$CONTRACT_REVIEW_DEV_TEST_SECRETS_DIR" != "$REAL_SECRETS_ROOT" ||
    "$OBS_E2E_SECRETS_ROOT" != "$REAL_SECRETS_ROOT" ]]; then
    echo "OBS_E2E_SECRETS_ROOT_POLICY_INVALID" >&2
    exit 81
  fi
  expected_real_secrets_root="$REAL_SECRETS_ROOT"
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
guard="$repo_root/scripts/observability-contracts/guard-test-target.sh"
secret_reader="$repo_root/scripts/observability-contracts/read-private-e2e-secret.py"
readonly CURL_CLI="/usr/bin/curl"
readonly CURL_CLI_SHA256="459c937b69bd76620d6d01100a0793294349c89871f4474d398efdae49fcee09"
readonly PYTHON_CLI="/usr/bin/python3.12"
readonly PYTHON_CLI_SHA256="1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"

verify_fixed_binary() {
  local path="$1" expected_hash="$2" label="$3"
  local owner group mode actual_hash
  if [[ ! -x "$path" || ! -f "$path" || -L "$path" ]]; then
    echo "OBS_E2E_BINARY_INVALID label=$label" >&2
    return 1
  fi
  IFS='|' read -r owner group mode < <(/usr/bin/stat -Lc '%u|%g|%a' -- "$path")
  actual_hash=$(/usr/bin/sha256sum "$path" | /usr/bin/awk '{print $1}')
  if [[ "$owner" != 0 || "$group" != 0 || "$mode" != 755 ||
    "$actual_hash" != "$expected_hash" ]]; then
    echo "OBS_E2E_BINARY_INVALID label=$label" >&2
    return 1
  fi
}

verify_fixed_binary "$CURL_CLI" "$CURL_CLI_SHA256" curl || exit 80
verify_fixed_binary "$PYTHON_CLI" "$PYTHON_CLI_SHA256" python || exit 80
if [[ ! -f "$secret_reader" || -L "$secret_reader" ]]; then
  echo "OBS_E2E_SECRET_READER_INVALID" >&2
  exit 80
fi

if [[ "$synthetic_mode" != true ]]; then
  unset OBS_E2E_GUARD_TEST_MODE \
    OBS_E2E_GUARD_TEST_POLICY_FILE \
    OBS_E2E_GUARD_TEST_TRUST_ROOT
elif [[ "${OBS_E2E_GUARD_TEST_MODE:-}" != "SYNTHETIC_NO_NETWORK" ]]; then
  echo "OBS_E2E_SYNTHETIC_GUARD_REQUIRED" >&2
  exit 80
fi

guard_args=(
  --environment "$OBS_E2E_ENVIRONMENT"
  --compose-project "$OBS_E2E_COMPOSE_PROJECT"
  --confirm "$OBS_E2E_CONFIRM"
  --origin "$OBS_E2E_ORIGIN"
  --allowed-origins "$OBS_E2E_ALLOWED_ORIGINS"
  --gateway-container "$OBS_E2E_GATEWAY_CONTAINER"
)
if [[ -n "${OBS_E2E_ADDITIONAL_FORBIDDEN_PORTS:-}" ]]; then
  guard_args+=(--forbidden-ports "$OBS_E2E_ADDITIONAL_FORBIDDEN_PORTS")
fi
verify_target() {
  "$guard" "${guard_args[@]}" >/dev/null
}

api_prefix="${OBS_E2E_API_PREFIX:-/monitor/observability}"
if [[ "$api_prefix" != /* || "$api_prefix" == *..* ]]; then
  echo "OBS_E2E_API_PREFIX_INVALID" >&2
  exit 82
fi

work_dir=$(mktemp -d /tmp/obs70-e2e-XXXXXX)
cleanup() {
  local status=$?
  bearer=""
  limited_bearer=""
  runtime_locator=""
  trap - EXIT INT TERM
  sse_last_event_id=""
  sse_replay_gap_event_id=""
  sse_reset_event_id=""
  export_idempotency_key=""
  case "$work_dir" in
    /tmp/obs70-e2e-*) rm -rf -- "$work_dir" || status=1 ;;
    *) echo "OBS_E2E_CLEANUP_REFUSED" >&2; status=1 ;;
  esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

read_private_line() {
  local file="$1"
  local label="$2"
  "$PYTHON_CLI" -I -B "$secret_reader" \
    --root "$OBS_E2E_SECRETS_ROOT" --file "$file" --label "$label" \
    --policy "$secret_policy" \
    --expected-real-root "$expected_real_secrets_root"
}

bearer=$(read_private_line "$OBS_E2E_BEARER_FILE" bearer) || exit 83
if [[ ! "$bearer" =~ ^[A-Za-z0-9._~-]{20,8192}$ ]]; then
  echo "OBS_E2E_BEARER_FORMAT_INVALID" >&2
  exit 84
fi

curl_config="$work_dir/curl.conf"
public_config="$work_dir/public-curl.conf"
printf 'silent\nshow-error\nmax-time = 20\nconnect-timeout = 5\nheader = "Authorization: Bearer %s"\nheader = "Accept: application/json"\n' "$bearer" >"$curl_config"
printf 'silent\nshow-error\nmax-time = 20\nconnect-timeout = 5\nheader = "Accept: application/json"\n' >"$public_config"
bearer=""

curl_config_value_ok() {
  local value="$1"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* &&
    "$value" != *\"* && "$value" != *\\* ]]
}

request() {
  local method="$1"
  local path="$2"
  local expected="$3"
  local body_file="$4"
  local slug="$5"
  local config="$6"
  shift 6
  if [[ ! "$method" =~ ^(GET|POST)$ || ! "$slug" =~ ^[a-z0-9-]+$ ||
    ! "$expected" =~ ^[0-9]{3}(,[0-9]{3})*$ ]] ||
    ! curl_config_value_ok "$OBS_E2E_ORIGIN$api_prefix$path"; then
    echo "OBS_E2E_REQUEST_ARGUMENT_INVALID probe=$slug" >&2
    return 1
  fi
  if [[ -n "$body_file" &&
    ( "$body_file" != "$work_dir/"* || ! -f "$body_file" || -L "$body_file" ) ]]; then
    echo "OBS_E2E_REQUEST_BODY_INVALID probe=$slug" >&2
    return 1
  fi
  if [[ "$config" != "$work_dir/"* || ! -f "$config" || -L "$config" ]]; then
    echo "OBS_E2E_CURL_CONFIG_INVALID probe=$slug" >&2
    return 1
  fi
  verify_target
  local headers="$work_dir/$slug.headers"
  local body="$work_dir/$slug.body"
  local request_config="$work_dir/$slug.request.conf"
  local curl_error="$work_dir/$slug.curl-error"
  {
    printf 'noproxy = "*"\n'
    printf 'proto = "=http,https"\nproto-redir = "=http,https"\nmax-redirs = 0\n'
    printf 'request = "%s"\ndump-header = "%s"\noutput = "%s"\n' \
      "$method" "$headers" "$body"
    printf 'write-out = "%%{http_code}"\nurl = "%s"\n' \
      "$OBS_E2E_ORIGIN$api_prefix$path"
    if [[ -n "$body_file" ]]; then
      printf 'header = "Content-Type: application/json"\ndata-binary = "@%s"\n' "$body_file"
    fi
    local header
    for header in "$@"; do
      if ! curl_config_value_ok "$header"; then
        echo "OBS_E2E_REQUEST_HEADER_INVALID probe=$slug" >&2
        return 1
      fi
      printf 'header = "%s"\n' "$header"
    done
  } >"$request_config"
  local status curl_status
  set +e
  status=$("$CURL_CLI" -q --config "$config" --config "$request_config" 2>"$curl_error")
  curl_status=$?
  set -e
  rm -f -- "$request_config" "$curl_error"
  if [[ "$curl_status" -ne 0 ]]; then
    echo "OBS_E2E_CURL_FAILED probe=$slug status=$curl_status" >&2
    return 1
  fi
  if [[ ",$expected," != *",$status,"* ]]; then
    echo "OBS_E2E_STATUS_MISMATCH probe=$slug expected=$expected actual=$status" >&2
    return 1
  fi
  printf '%s\n' "$headers"
}

assert_header() {
  local headers="$1" name="$2" expected_fragment="$3"
  if ! tr -d '\r' <"$headers" | grep -Eiq "^$name:.*$expected_fragment"; then
    echo "OBS_E2E_HEADER_MISSING name=$name" >&2
    return 1
  fi
}

json_check() {
  local file="$1" mode="$2" path="$3" expected="${4:-}"
  "$PYTHON_CLI" -I -B - "$file" "$mode" "$path" "$expected" <<'PY'
import json
import sys

file_name, mode, path, expected = sys.argv[1:]
with open(file_name, encoding="utf-8") as stream:
    value = json.load(stream)

def at_path(item, dotted):
    for token in dotted.split("."):
        if not isinstance(item, dict) or token not in item:
            raise KeyError(token)
        item = item[token]
    return item

def values_for_key(item, key):
    if isinstance(item, dict):
        for name, child in item.items():
            if name == key:
                yield child
            yield from values_for_key(child, key)
    elif isinstance(item, list):
        for child in item:
            yield from values_for_key(child, key)

ok = False
try:
    if mode == "equal":
        ok = str(at_path(value, path)) == expected
    elif mode == "nonempty":
        ok = at_path(value, path) not in (None, "", [], {})
    elif mode == "contains-key-value":
        ok = any(str(item) == expected for item in values_for_key(value, path))
    elif mode == "contains-key-fragment":
        ok = any(expected in str(item) for item in values_for_key(value, path))
except (KeyError, TypeError):
    ok = False
if not ok:
    raise SystemExit("OBS_E2E_JSON_ASSERTION_FAILED")
PY
}
json_contains_reference() {
  "$PYTHON_CLI" -I -B - "$1" "$2" "$3" "$4" <<'PY'
import json
import sys

response_path, key, reference_path, dotted = sys.argv[1:]
with open(response_path, encoding="utf-8") as stream:
    response = json.load(stream)
with open(reference_path, encoding="utf-8") as stream:
    expected = json.load(stream)
for token in dotted.split("."):
    expected = expected[token]


def values_for_key(item, name):
    if isinstance(item, dict):
        for current, child in item.items():
            if current == name:
                yield child
            yield from values_for_key(child, name)
    elif isinstance(item, list):
        for child in item:
            yield from values_for_key(child, name)


if not any(value == expected for value in values_for_key(response, key)):
    raise SystemExit("OBS_E2E_JSON_REFERENCE_ASSERTION_FAILED")
PY
}



json_value() {
  "$PYTHON_CLI" -I -B - "$1" "$2" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
for token in sys.argv[2].split("."):
    value = value[token]
if not isinstance(value, str) or not value:
    raise SystemExit("OBS_E2E_JSON_VALUE_INVALID")
print(value)
PY
}

json_same() {
  "$PYTHON_CLI" -I -B - "$1" "$2" "$3" <<'PY'
import json
import sys
def read(path):
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    for token in sys.argv[3].split("."):
        value = value[token]
    return value
if read(sys.argv[1]) != read(sys.argv[2]):
    raise SystemExit("OBS_E2E_JSON_IDENTITY_MISMATCH")
PY
}

overview_headers=$(request GET "/overview" 200 "" overview "$curl_config")
assert_header "$overview_headers" Cache-Control 'no-store|private'
assert_header "$overview_headers" Referrer-Policy no-referrer
assert_header "$overview_headers" Vary Authorization
assert_header "$overview_headers" X-Request-ID '.+'
request GET "/overview" 401 "" unauthenticated "$public_config" >/dev/null
request GET "/timeline" 400 "" timeline-missing-correlation "$curl_config" >/dev/null
request GET "/events?cursor=obs70syntheticcursor0001&retryToken=obs70syntheticretry0001" 400 "" cursor-retry-mutual-exclusion "$curl_config" >/dev/null
runtime_missing="$work_dir/runtime-missing.json"
printf '%s' '{"locator":"loc_obs70_synthetic_missing_000001"}' >"$runtime_missing"
request POST "/runtime-logs/detail" 404 "$runtime_missing" runtime-missing "$curl_config" >/dev/null
security_expected="${OBS_E2E_SECURITY_EXPECTED_STATUS:-404}"
if [[ "$security_expected" != 404 && "$security_expected" != 503 ]]; then
  echo "OBS_E2E_SECURITY_EXPECTATION_INVALID" >&2
  exit 85
fi
request GET "/security-events/evt_obs70_synthetic_missing" "$security_expected" "" security-missing "$curl_config" >/dev/null

if [[ "$synthetic_mode" == true ]]; then
  synthetic_required=(
    OBS_E2E_ARGV_SAFETY_CURSOR_FILE
    OBS_E2E_ARGV_SAFETY_IDEMPOTENCY_KEY_FILE
    OBS_E2E_ARGV_SAFETY_LOCATOR_FILE
  )
  for name in "${synthetic_required[@]}"; do
    if [[ -z "${!name:-}" ]]; then
      echo "OBS_E2E_REQUIRED_SETTING_MISSING name=$name" >&2
      exit 81
    fi
  done
  argv_safety_cursor=$(read_private_line "$OBS_E2E_ARGV_SAFETY_CURSOR_FILE" argv-safety-cursor) || exit 83
  argv_safety_idempotency_key=$(read_private_line "$OBS_E2E_ARGV_SAFETY_IDEMPOTENCY_KEY_FILE" argv-safety-idempotency-key) || exit 83
  argv_safety_locator=$(read_private_line "$OBS_E2E_ARGV_SAFETY_LOCATOR_FILE" argv-safety-locator) || exit 83
  if [[ ! "$argv_safety_cursor" =~ ^obs70-e2e-cursor-[0-9a-f]{32}$ ||
    ! "$argv_safety_idempotency_key" =~ ^obs70-e2e-[0-9a-f]{32}$ ||
    ! "$argv_safety_locator" =~ ^obs70-e2e-locator-[0-9a-f]{32}$ ]]; then
    echo "OBS_E2E_ARGV_SAFETY_VALUE_INVALID" >&2
    exit 85
  fi
  argv_safety_body="$work_dir/argv-safety.json"
  printf '{"locator":"%s"}' "$argv_safety_locator" >"$argv_safety_body"
  request POST "/runtime-logs/detail?cursor=$argv_safety_cursor" 404 \
    "$argv_safety_body" argv-safety "$curl_config" \
    "Idempotency-Key: $argv_safety_idempotency_key" >/dev/null
  argv_safety_cursor=""
  argv_safety_idempotency_key=""
  argv_safety_locator=""
  echo "OBS_E2E_PROBE_SAFETY_PATH_OK probes=7"
  exit 0
fi

fixture_required=(
  OBS_E2E_FIXTURE_TASK_ID
  OBS_E2E_FIXTURE_RUN_ID
  OBS_E2E_FIXTURE_INVOCATION_ID
  OBS_E2E_FIXTURE_SECURITY_EVENT_ID
  OBS_E2E_FIXTURE_TENANT_ID
  OBS_E2E_FIXTURE_FEATURE_CODE
  OBS_E2E_FIXTURE_FROM
  OBS_E2E_FIXTURE_TO
  OBS_E2E_FIXTURE_CANARY
  OBS_E2E_RUNTIME_LOCATOR_FILE
  OBS_E2E_LIMITED_BEARER_FILE
  OBS_E2E_SSE_LAST_EVENT_ID_FILE
  OBS_E2E_SSE_REPLAY_GAP_EVENT_ID_FILE
  OBS_E2E_SSE_RESET_EVENT_ID_FILE
  OBS_E2E_EXPORT_IDEMPOTENCY_KEY_FILE
)
for name in "${fixture_required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "OBS_E2E_FIXTURE_SETTING_MISSING name=$name" >&2
    exit 86
  fi
done
sse_last_event_id=$(read_private_line "$OBS_E2E_SSE_LAST_EVENT_ID_FILE" sse-last-event-id) || exit 83
sse_replay_gap_event_id=$(read_private_line "$OBS_E2E_SSE_REPLAY_GAP_EVENT_ID_FILE" sse-replay-gap-event-id) || exit 83
sse_reset_event_id=$(read_private_line "$OBS_E2E_SSE_RESET_EVENT_ID_FILE" sse-reset-event-id) || exit 83
export_idempotency_key=$(read_private_line "$OBS_E2E_EXPORT_IDEMPOTENCY_KEY_FILE" export-idempotency-key) || exit 83

for value in   "$OBS_E2E_FIXTURE_TASK_ID" "$OBS_E2E_FIXTURE_RUN_ID"   "$OBS_E2E_FIXTURE_INVOCATION_ID" "$OBS_E2E_FIXTURE_SECURITY_EVENT_ID"   "$OBS_E2E_FIXTURE_TENANT_ID" "$OBS_E2E_FIXTURE_FEATURE_CODE"   "$sse_last_event_id" "$sse_replay_gap_event_id" "$sse_reset_event_id"; do
  if [[ ! "$value" =~ ^[A-Za-z0-9._:-]{1,200}$ ]]; then
    echo "OBS_E2E_FIXTURE_IDENTIFIER_INVALID" >&2
    exit 86
  fi
done
if [[ ! "$OBS_E2E_FIXTURE_FROM" =~ ^[0-9TZ:._+-]{20,40}$ ||
  ! "$OBS_E2E_FIXTURE_TO" =~ ^[0-9TZ:._+-]{20,40}$ ||
  ! "$OBS_E2E_FIXTURE_CANARY" =~ ^obs70-e2e-canary-[0-9a-f]{32}$ ||
  ! "$export_idempotency_key" =~ ^obs70-e2e-[0-9a-f]{32}$ ]]; then
  echo "OBS_E2E_FIXTURE_VALUE_INVALID" >&2
  exit 86
fi

limited_bearer=$(read_private_line "$OBS_E2E_LIMITED_BEARER_FILE" limited-bearer) || exit 83
runtime_locator=$(read_private_line "$OBS_E2E_RUNTIME_LOCATOR_FILE" runtime-locator) || exit 83
if [[ ! "$limited_bearer" =~ ^[A-Za-z0-9._~-]{20,8192}$ ||
  ! "$runtime_locator" =~ ^[A-Za-z0-9._~:-]{20,4096}$ ]]; then
  echo "OBS_E2E_FIXTURE_SECRET_FORMAT_INVALID" >&2
  exit 86
fi
limited_config="$work_dir/limited-curl.conf"
printf 'silent\nshow-error\nmax-time = 20\nconnect-timeout = 5\nheader = "Authorization: Bearer %s"\nheader = "Accept: application/json"\n' "$limited_bearer" >"$limited_config"
limited_bearer=""

request GET "/tasks/$OBS_E2E_FIXTURE_TASK_ID" 200 "" task-detail "$curl_config" >/dev/null
json_check "$work_dir/task-detail.body" equal taskId "$OBS_E2E_FIXTURE_TASK_ID"
json_check "$work_dir/task-detail.body" equal tenantId "$OBS_E2E_FIXTURE_TENANT_ID"

request GET "/runs/$OBS_E2E_FIXTURE_RUN_ID/stages" 200 "" run-stages "$curl_config" >/dev/null
json_check "$work_dir/run-stages.body" nonempty data
request GET "/runs/$OBS_E2E_FIXTURE_RUN_ID/events?limit=100" 200 "" run-events "$curl_config" >/dev/null
json_check "$work_dir/run-events.body" contains-key-value runId "$OBS_E2E_FIXTURE_RUN_ID"

request GET "/model-invocations/$OBS_E2E_FIXTURE_INVOCATION_ID" 200 "" model-detail "$curl_config" >/dev/null
json_check "$work_dir/model-detail.body" equal invocation.invocationId "$OBS_E2E_FIXTURE_INVOCATION_ID"
json_check "$work_dir/model-detail.body" equal invocation.tenantId "$OBS_E2E_FIXTURE_TENANT_ID"

request GET "/security-events/$OBS_E2E_FIXTURE_SECURITY_EVENT_ID" 200 "" security-detail "$curl_config" >/dev/null
json_check "$work_dir/security-detail.body" equal eventId "$OBS_E2E_FIXTURE_SECURITY_EVENT_ID"
json_check "$work_dir/security-detail.body" equal tenantId "$OBS_E2E_FIXTURE_TENANT_ID"

runtime_search="$work_dir/runtime-search.json"
printf '{"from":"%s","to":"%s","keyword":"%s","runId":"%s","limit":20}'   "$OBS_E2E_FIXTURE_FROM" "$OBS_E2E_FIXTURE_TO"   "$OBS_E2E_FIXTURE_CANARY" "$OBS_E2E_FIXTURE_RUN_ID" >"$runtime_search"
runtime_detail="$work_dir/runtime-detail.json"
printf '{"locator":"%s"}' "$runtime_locator" >"$runtime_detail"
runtime_locator=""
request POST "/runtime-logs/search" 200 "$runtime_search" runtime-search "$curl_config" >/dev/null
json_contains_reference "$work_dir/runtime-search.body" locator "$runtime_detail" locator
json_check "$work_dir/runtime-search.body" contains-key-fragment displayMessage "$OBS_E2E_FIXTURE_CANARY"
request POST "/runtime-logs/detail" 200 "$runtime_detail" runtime-detail "$curl_config" >/dev/null
json_check "$work_dir/runtime-detail.body" contains-key-value runId "$OBS_E2E_FIXTURE_RUN_ID"

request GET "/tasks?limit=1" 200 "" task-snapshot-first "$curl_config" >/dev/null
json_check "$work_dir/task-snapshot-first.body" nonempty query.querySnapshotId
json_check "$work_dir/task-snapshot-first.body" equal query.snapshotMode MATERIALIZED_RESULT_SET
next_cursor=$("$PYTHON_CLI" -I -B - "$work_dir/task-snapshot-first.body" <<'PY'
import json
import sys
import urllib.parse
with open(sys.argv[1], encoding="utf-8") as stream:
    cursor = json.load(stream).get("page", {}).get("nextCursor")
print(urllib.parse.quote(cursor, safe="") if cursor else "")
PY
)
if [[ -z "$next_cursor" ]]; then
  echo "OBS_E2E_SNAPSHOT_CURSOR_REQUIRED" >&2
  exit 87
fi
request GET "/tasks?limit=1&cursor=$next_cursor" 200 "" task-snapshot-next "$curl_config" >/dev/null
json_same "$work_dir/task-snapshot-first.body" "$work_dir/task-snapshot-next.body" query.querySnapshotId
next_cursor=""

stream_headers="$work_dir/sse-live.headers"
stream_body="$work_dir/sse-live.body"
stream_config="$work_dir/sse-live.request.conf"
stream_error="$work_dir/sse-live.curl-error"
if ! curl_config_value_ok "$OBS_E2E_ORIGIN$api_prefix/runs/$OBS_E2E_FIXTURE_RUN_ID/events/stream" ||
  ! curl_config_value_ok "Last-Event-ID: $sse_last_event_id"; then
  echo "OBS_E2E_SSE_ARGUMENT_INVALID" >&2
  exit 87
fi
{
  printf 'noproxy = "*"\n'
  printf 'proto = "=http,https"\nproto-redir = "=http,https"\nmax-redirs = 0\n'
  printf 'max-time = 8\nno-buffer\nheader = "Accept: text/event-stream"\n'
  printf 'header = "Last-Event-ID: %s"\n' "$sse_last_event_id"
  printf 'dump-header = "%s"\noutput = "%s"\nwrite-out = "%%{http_code}"\n' \
    "$stream_headers" "$stream_body"
  printf 'url = "%s"\n' \
    "$OBS_E2E_ORIGIN$api_prefix/runs/$OBS_E2E_FIXTURE_RUN_ID/events/stream"
} >"$stream_config"
verify_target
set +e
stream_status=$("$CURL_CLI" -q --config "$curl_config" --config "$stream_config" 2>"$stream_error")
stream_curl_status=$?
set -e
rm -f -- "$stream_config" "$stream_error"
if [[ "$stream_status" != 200 ||
  ( "$stream_curl_status" -ne 0 && "$stream_curl_status" -ne 28 ) ]]; then
  echo "OBS_E2E_SSE_LIVE_FAILED status=$stream_curl_status" >&2
  exit 87
fi
assert_header "$stream_headers" Cache-Control 'no-cache.*no-store.*private'
assert_header "$stream_headers" X-Accel-Buffering no
assert_header "$stream_headers" Content-Type text/event-stream
"$PYTHON_CLI" -I -B - "$stream_body" <<'PY'
import json
import sys
text = open(sys.argv[1], encoding="utf-8").read()
valid = False
for block in text.replace("\r\n", "\n").split("\n\n"):
    fields = {}
    for line in block.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
    if {"id", "event", "data"} <= fields:
        data = json.loads(fields["data"])
        event = data.get("event", {})
        valid = (
            isinstance(data.get("sequence"), int)
            and bool(data.get("occurredAt"))
            and event.get("eventId") == fields["id"]
        )
        if valid:
            break
if not valid:
    raise SystemExit("OBS_E2E_SSE_EVENT_IDENTITY_FAILED")
PY

request GET "/runs/$OBS_E2E_FIXTURE_RUN_ID/events/stream" 409 "" sse-replay-gap "$curl_config"   "Last-Event-ID: $sse_replay_gap_event_id" >/dev/null
json_check "$work_dir/sse-replay-gap.body" equal code EVENT_STREAM_REPLAY_GAP
request GET "/runs/$OBS_E2E_FIXTURE_RUN_ID/events/stream" 410 "" sse-reset-required "$curl_config"   "Last-Event-ID: $sse_reset_event_id" >/dev/null
json_check "$work_dir/sse-reset-required.body" equal code EVENT_STREAM_RESET_REQUIRED

request GET "/security-events?limit=1" 403 "" permission-security "$limited_config" >/dev/null

if [[ "${OBS_E2E_EXPORT_RUN:-}" != "I_UNDERSTAND_EXPORT_CREATES_TEST_DATA" ||
  "${OBS_E2E_EXPORT_CONFIRM:-}" != "contract-review-code-dev" ]]; then
  echo "OBS_E2E_EXPORT_WRITE_CONFIRMATIONS_REQUIRED" >&2
  exit 88
fi
export_request="$work_dir/export-request.json"
printf '{"resource":"TASKS","format":"JSONL","from":"%s","to":"%s","tenantScope":"CURRENT","filter":{"filterType":"TASKS","featureCode":"%s"}}'   "$OBS_E2E_FIXTURE_FROM" "$OBS_E2E_FIXTURE_TO"   "$OBS_E2E_FIXTURE_FEATURE_CODE" >"$export_request"
request POST "/exports" 202 "$export_request" export-create "$curl_config"   "Idempotency-Key: $export_idempotency_key" >/dev/null
json_check "$work_dir/export-create.body" nonempty exportId
json_check "$work_dir/export-create.body" nonempty querySnapshotId
request POST "/exports" 202 "$export_request" export-repeat "$curl_config"   "Idempotency-Key: $export_idempotency_key" >/dev/null
json_same "$work_dir/export-create.body" "$work_dir/export-repeat.body" exportId
export_id=$(json_value "$work_dir/export-create.body" exportId)
if [[ ! "$export_id" =~ ^[A-Za-z0-9._:-]{1,200}$ ]]; then
  echo "OBS_E2E_EXPORT_ID_INVALID" >&2
  exit 88
fi
request GET "/exports/$export_id" 200 "" export-status "$curl_config" >/dev/null
json_check "$work_dir/export-status.body" equal exportId "$export_id"
export_id=""

bad_export="$work_dir/export-bad-discriminator.json"
printf '{"resource":"TASKS","format":"JSONL","from":"%s","to":"%s","filter":{"filterType":"EVENTS"}}'   "$OBS_E2E_FIXTURE_FROM" "$OBS_E2E_FIXTURE_TO" >"$bad_export"
request POST "/exports" 400 "$bad_export" export-discriminator-negative "$curl_config"   "Idempotency-Key: $export_idempotency_key-negative" >/dev/null

echo "OBS_E2E_PROBE_OK target=contract-review-code-dev probes=task,run,model,security,runtime,sse,snapshot,permission,export"
