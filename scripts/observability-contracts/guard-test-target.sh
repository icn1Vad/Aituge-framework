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
    echo "OBS_E2E_GUARD_SHELL_LOADER_OVERRIDE_DENIED name=$shell_loader_override" >&2
    exit 74
  fi
done

readonly OBS_E2E_SAFE_STAGE_ARGUMENT="--obs70-guard-safe-bash-stage-7f6c1e43"
if [[ "${1:-}" != "$OBS_E2E_SAFE_STAGE_ARGUMENT" ]]; then
  if [[ -L "${BASH_SOURCE[0]}" || ! -f "${BASH_SOURCE[0]}" ]]; then
    echo "OBS_E2E_GUARD_ENTRY_INVALID" >&2
    exit 74
  fi
  entry_script=$(/usr/bin/realpath -e -- "${BASH_SOURCE[0]}") || {
    echo "OBS_E2E_GUARD_ENTRY_INVALID" >&2
    exit 74
  }
  exec /usr/bin/env \
    -u BASH_ENV -u ENV -u SHELLOPTS -u BASHOPTS -u CDPATH \
    -u GLOBIGNORE -u POSIXLY_CORRECT \
    PATH="$OBS_E2E_SAFE_PATH" \
    /usr/bin/bash -p -- "$entry_script" "$OBS_E2E_SAFE_STAGE_ARGUMENT" "$@"
fi
shift

environment=""
compose_project=""
confirmation=""
origin=""
allowed_origins=""
forbidden_ports=""
gateway_container=""

usage() {
  echo "usage: guard-test-target.sh --environment test --compose-project contract-review-code-dev --confirm contract-review-code-dev --origin URL --allowed-origins CSV [--forbidden-ports EXTRA_CSV] --gateway-container FULL_64_HEX_ID" >&2
}

while (($#)); do
  case "$1" in
    --environment) environment="${2:-}"; shift 2 ;;
    --compose-project) compose_project="${2:-}"; shift 2 ;;
    --confirm) confirmation="${2:-}"; shift 2 ;;
    --origin) origin="${2:-}"; shift 2 ;;
    --allowed-origins) allowed_origins="${2:-}"; shift 2 ;;
    --forbidden-ports) forbidden_ports="${2:-}"; shift 2 ;;
    --gateway-container) gateway_container="${2:-}"; shift 2 ;;
    *) usage; echo "OBS_E2E_GUARD_UNKNOWN_ARGUMENT" >&2; exit 64 ;;
  esac
done

readonly EXPECTED_DOCKER_CONTEXT="default"
readonly EXPECTED_DOCKER_ENDPOINT="unix:///var/run/docker.sock|{}"
readonly EXPECTED_DOCKER_DAEMON="3196b392-cce0-4178-a30a-2a9ff44d271c|afs2600151|/var/lib/docker"
readonly EXPECTED_GATEWAY_NAME="contract-review-dev-caddy-1"
readonly EXPECTED_GATEWAY_SERVICE="test-gateway"
readonly EXPECTED_GATEWAY_IMAGE_ID="sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
readonly EXPECTED_GATEWAY_NETWORK="contract-review-code-dev-agent-internal"
readonly PAGE6_TRUST_COMMIT="fbce7a11dec66cd7957b6831d0dc104ad4758985"
readonly PAGE6_TRUST_SHA256="15f0d75567c3ff4da4725507ad5e689eeb766da008a6602766386b13006b531f"

for key in DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG; do
  if [[ -v $key ]]; then
    echo "OBS_E2E_GUARD_DOCKER_OVERRIDE_DENIED name=$key" >&2
    exit 74
  fi
done

if [[ "${OBS_E2E_GUARD_TEST_MODE:-}" == "SYNTHETIC_NO_NETWORK" ]]; then
  readonly SYNTHETIC_DOCKER_CLI="${OBS_E2E_GUARD_TEST_DOCKER_CLI:-}"
  synthetic_docker_owner=$(/usr/bin/id -u)
  if [[ "$SYNTHETIC_DOCKER_CLI" != /tmp/obs70-* ||
    ! -f "$SYNTHETIC_DOCKER_CLI" || ! -x "$SYNTHETIC_DOCKER_CLI" ||
    -L "$SYNTHETIC_DOCKER_CLI" ||
    "$(/usr/bin/realpath -e -- "$SYNTHETIC_DOCKER_CLI" 2>/dev/null)" != "$SYNTHETIC_DOCKER_CLI" ||
    "$(/usr/bin/stat -Lc '%u:%a:%h:%F' -- "$SYNTHETIC_DOCKER_CLI")" != "$synthetic_docker_owner:700:1:regular file" ]]; then
    echo "OBS_E2E_GUARD_TEST_DOCKER_INVALID" >&2
    exit 74
  fi
  guard_verify_docker_daemon() {
    local actual_context actual_endpoint actual_daemon
    actual_context=$("$SYNTHETIC_DOCKER_CLI" context show 2>/dev/null) || return 1
    actual_endpoint=$("$SYNTHETIC_DOCKER_CLI" context inspect "$EXPECTED_DOCKER_CONTEXT" \
      --format '{{.Endpoints.docker.Host}}|{{json .TLSMaterial}}' 2>/dev/null) ||
      return 1
    actual_daemon=$("$SYNTHETIC_DOCKER_CLI" --context "$EXPECTED_DOCKER_CONTEXT" info \
      --format '{{.ID}}|{{.Name}}|{{.DockerRootDir}}' 2>/dev/null) || return 1
    [[ "$actual_context" == "$EXPECTED_DOCKER_CONTEXT" &&
      "$actual_endpoint" == "$EXPECTED_DOCKER_ENDPOINT" &&
      "$actual_daemon" == "$EXPECTED_DOCKER_DAEMON" ]]
  }
  guard_docker() {
    guard_verify_docker_daemon || return 1
    "$SYNTHETIC_DOCKER_CLI" --context "$EXPECTED_DOCKER_CONTEXT" "$@"
  }
else
  platform_source="${CONTRACT_REVIEW_DEV_PLATFORM_SOURCE_DIR:-/home/aituge/worktrees/obs-platform-test/infrastructure/observability-test}"
  if [[ ! "$platform_source" = /* || -L "$platform_source" ||
    ! -d "$platform_source" ]]; then
    echo "OBS_E2E_GUARD_PAGE6_TRUST_INVALID" >&2
    exit 74
  fi
  platform_source_real=$(realpath -e -- "$platform_source" 2>/dev/null) || {
    echo "OBS_E2E_GUARD_PAGE6_TRUST_INVALID" >&2
    exit 74
  }
  case "$platform_source_real" in
    /home/aituge/worktrees/obs-platform-test/infrastructure/observability-test | \
      /home/aituge/worktrees/obs-integration/infrastructure/observability-test)
      ;;
    *)
      echo "OBS_E2E_GUARD_PAGE6_TRUST_INVALID" >&2
      exit 74
      ;;
  esac
  trust_script="$platform_source_real/docker-trust.sh"
  if [[ ! -f "$trust_script" || -L "$trust_script" ||
    "$(realpath -e -- "$trust_script")" != "$trust_script" ]]; then
    echo "OBS_E2E_GUARD_PAGE6_TRUST_INVALID" >&2
    exit 74
  fi
  trust_hash=$(sha256sum -- "$trust_script")
  if [[ "${trust_hash%% *}" != "$PAGE6_TRUST_SHA256" ]]; then
    echo "OBS_E2E_GUARD_PAGE6_TRUST_INVALID" >&2
    exit 74
  fi
  # shellcheck source=/dev/null
  source "$trust_script"
  guard_verify_docker_daemon() {
    page6_verify_docker_daemon
  }
  guard_docker() {
    page6_verify_docker_daemon || return 1
    page6_docker "$@"
  }
fi
if ! guard_verify_docker_daemon; then
  echo "OBS_E2E_GUARD_DOCKER_TRUST_DENIED" >&2
  exit 74
fi
if [[ "$environment" != "test" ]]; then
  echo "OBS_E2E_GUARD_ENVIRONMENT_DENIED" >&2
  exit 65
fi
if [[ "$compose_project" != "contract-review-code-dev" ]]; then
  echo "OBS_E2E_GUARD_PROJECT_DENIED" >&2
  exit 66
fi
if [[ "$confirmation" != "contract-review-code-dev" ]]; then
  echo "OBS_E2E_GUARD_CONFIRMATION_REQUIRED" >&2
  exit 67
fi
if [[ -z "$origin" || -z "$allowed_origins" || -z "$gateway_container" ]]; then
  echo "OBS_E2E_GUARD_ALLOWLIST_REQUIRED" >&2
  exit 68
fi
if [[ ! "$origin" =~ ^(https?)://(\[([0-9A-Fa-f:]+)\]|([A-Za-z0-9._-]+))(:([0-9]{1,5}))?$ ]]; then
  echo "OBS_E2E_GUARD_ORIGIN_INVALID" >&2
  exit 69
fi
scheme="${BASH_REMATCH[1]}"
host="${BASH_REMATCH[3]:-${BASH_REMATCH[4]}}"
port="${BASH_REMATCH[6]:-}"
if [[ -z "$port" ]]; then
  [[ "$scheme" == "https" ]] && port="443" || port="80"
fi
if [[ ! "$gateway_container" =~ ^[0-9a-f]{64}$ ]]; then
  echo "OBS_E2E_GUARD_CONTAINER_INVALID" >&2
  exit 69
fi

readonly SYSTEM_FORBIDDEN_PORTS_FILE="/etc/aituge/observability/formal-port-denylist"
policy_file="$SYSTEM_FORBIDDEN_PORTS_FILE"
policy_trust_root="/etc"
policy_owner="0"
synthetic_policy=false
if [[ "${OBS_E2E_GUARD_TEST_MODE:-}" == "SYNTHETIC_NO_NETWORK" ]]; then
  synthetic_policy=true
  policy_file="${OBS_E2E_GUARD_TEST_POLICY_FILE:-}"
  policy_trust_root="${OBS_E2E_GUARD_TEST_TRUST_ROOT:-}"
  policy_owner="$(id -u)"
fi
if [[ -z "$policy_file" || ! -f "$policy_file" || -L "$policy_file" ]]; then
  echo "OBS_E2E_GUARD_POLICY_REQUIRED" >&2
  exit 68
fi
if [[ -z "$policy_trust_root" || ! -d "$policy_trust_root" ||
  -L "$policy_trust_root" ]]; then
  echo "OBS_E2E_GUARD_POLICY_UNTRUSTED" >&2
  exit 68
fi
if ! policy_real=$(realpath -e -- "$policy_file" 2>/dev/null) ||
  ! policy_trust_root_real=$(realpath -e -- "$policy_trust_root" 2>/dev/null); then
  echo "OBS_E2E_GUARD_POLICY_UNTRUSTED" >&2
  exit 68
fi
if [[ "$policy_real" != "$policy_file" ||
  "$policy_trust_root_real" != "$policy_trust_root" ]]; then
  echo "OBS_E2E_GUARD_POLICY_UNTRUSTED" >&2
  exit 68
fi
if [[ "$synthetic_policy" != true &&
  "$policy_real" != "$SYSTEM_FORBIDDEN_PORTS_FILE" ]]; then
  echo "OBS_E2E_GUARD_POLICY_UNTRUSTED" >&2
  exit 68
fi
case "$policy_real" in
  "$policy_trust_root_real"/*) ;;
  *)
    echo "OBS_E2E_GUARD_POLICY_UNTRUSTED" >&2
    exit 68
    ;;
esac

policy_component="$policy_real"
while true; do
  if [[ -L "$policy_component" ]] ||
    [[ "$(stat -c '%u' -- "$policy_component")" != "$policy_owner" ]] ||
    ((8#$(stat -c '%a' -- "$policy_component") & 8#022)); then
    echo "OBS_E2E_GUARD_POLICY_UNTRUSTED" >&2
    exit 68
  fi
  [[ "$policy_component" == "$policy_trust_root_real" ]] && break
  policy_component=$(dirname -- "$policy_component")
  if [[ "$policy_component" == "/" ]]; then
    echo "OBS_E2E_GUARD_POLICY_UNTRUSTED" >&2
    exit 68
  fi
done

declare -A denied_ports=()
while IFS= read -r policy_line || [[ -n "$policy_line" ]]; do
  policy_line="${policy_line%%#*}"
  policy_line=$(printf '%s' "$policy_line" | tr -d '[:space:]')
  [[ -z "$policy_line" ]] && continue
  if [[ ! "$policy_line" =~ ^[0-9]{1,5}$ ]] ||
    ((10#$policy_line < 1 || 10#$policy_line > 65535)); then
    echo "OBS_E2E_GUARD_POLICY_INVALID" >&2
    exit 68
  fi
  denied_ports["$((10#$policy_line))"]=1
done <"$policy_file"
if (("${#denied_ports[@]}" == 0)); then
  echo "OBS_E2E_GUARD_POLICY_EMPTY" >&2
  exit 68
fi
if [[ -n "$forbidden_ports" ]]; then
  IFS=',' read -r -a additional_ports <<<"$forbidden_ports"
  for additional_port in "${additional_ports[@]}"; do
    if [[ ! "$additional_port" =~ ^[0-9]{1,5}$ ]] ||
      ((10#$additional_port < 1 || 10#$additional_port > 65535)); then
      echo "OBS_E2E_GUARD_ADDITIONAL_PORT_INVALID" >&2
      exit 68
    fi
    denied_ports["$((10#$additional_port))"]=1
  done
fi

lower_target=$(printf '%s' "$origin $compose_project $environment $gateway_container" | tr '[:upper:]' '[:lower:]')
if [[ "$lower_target" == *formal* || "$lower_target" == *prod* || "$lower_target" == *production* ]]; then
  echo "OBS_E2E_GUARD_FORMAL_TARGET_DENIED" >&2
  exit 70
fi

allowed=false
IFS=',' read -r -a origins <<<"$allowed_origins"
for candidate in "${origins[@]}"; do
  if [[ "$origin" == "$candidate" ]]; then
    allowed=true
    break
  fi
done
if [[ "$allowed" != true ]]; then
  echo "OBS_E2E_GUARD_ORIGIN_NOT_ALLOWLISTED" >&2
  exit 71
fi

inspect_format='{{.Id}}|{{.Name}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.aituge.environment"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{.Image}}|{{.State.Running}}|{{range $name, $value := .NetworkSettings.Networks}}{{$name}}@{{$value.NetworkID}},{{end}}|{{range $port, $bindings := .NetworkSettings.Ports}}{{range $bindings}}{{$port}}@{{.HostIp}}@{{.HostPort}},{{end}}{{end}}'
if ! guard_verify_docker_daemon ||
  ! metadata=$(guard_docker inspect --format "$inspect_format" "$gateway_container" 2>/dev/null); then
  echo "OBS_E2E_GUARD_CONTAINER_NOT_FOUND" >&2
  exit 74
fi
IFS='|' read -r actual_id actual_name actual_project actual_environment \
  actual_service actual_image_id running network_csv port_binding_csv <<<"$metadata"
actual_name="${actual_name#/}"
lower_actual_name=$(printf '%s' "$actual_name" | tr '[:upper:]' '[:lower:]')
if [[ "$lower_actual_name" == *formal* || "$lower_actual_name" == *prod* ||
  "$lower_actual_name" == *production* ]]; then
  echo "OBS_E2E_GUARD_FORMAL_TARGET_DENIED" >&2
  exit 70
fi
if [[ "$gateway_container" != "$actual_id" ]]; then
  echo "OBS_E2E_GUARD_CONTAINER_ID_MISMATCH" >&2
  exit 75
fi
if [[ "$actual_name" != "$EXPECTED_GATEWAY_NAME" ]]; then
  echo "OBS_E2E_GUARD_CONTAINER_NAME_MISMATCH" >&2
  exit 75
fi
if [[ "$actual_project" != "contract-review-code-dev" ||
  "$actual_project" != "$compose_project" ]]; then
  echo "OBS_E2E_GUARD_CONTAINER_PROJECT_MISMATCH" >&2
  exit 76
fi
if [[ "$actual_environment" != "$environment" ]]; then
  echo "OBS_E2E_GUARD_CONTAINER_ENVIRONMENT_MISMATCH" >&2
  exit 77
fi
if [[ "$actual_service" != "$EXPECTED_GATEWAY_SERVICE" ]]; then
  echo "OBS_E2E_GUARD_CONTAINER_SERVICE_MISMATCH" >&2
  exit 77
fi
if [[ "$actual_image_id" != "$EXPECTED_GATEWAY_IMAGE_ID" ]]; then
  echo "OBS_E2E_GUARD_CONTAINER_IMAGE_MISMATCH" >&2
  exit 77
fi
if [[ "$running" != "true" ]]; then
  echo "OBS_E2E_GUARD_CONTAINER_NOT_RUNNING" >&2
  exit 78
fi

IFS=',' read -r -a networks <<<"$network_csv"
network_entries=()
for network_entry in "${networks[@]}"; do
  [[ -z "$network_entry" ]] || network_entries+=("$network_entry")
done
if (("${#network_entries[@]}" != 1)); then
  echo "OBS_E2E_GUARD_NETWORK_SET_MISMATCH" >&2
  exit 79
fi
IFS='@' read -r network_name network_id network_extra <<<"${network_entries[0]}"
if [[ -n "${network_extra:-}" ||
  "$network_name" != "$EXPECTED_GATEWAY_NETWORK" ||
  ! "$network_id" =~ ^[0-9a-f]{64}$ ]]; then
  echo "OBS_E2E_GUARD_NETWORK_SET_MISMATCH" >&2
  exit 79
fi
network_format='{{.Id}}|{{.Name}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.network"}}'
if ! network_metadata=$(guard_docker network inspect \
  --format "$network_format" "$network_id" 2>/dev/null); then
  echo "OBS_E2E_GUARD_NETWORK_IDENTITY_MISMATCH" >&2
  exit 79
fi
IFS='|' read -r inspected_network_id inspected_network_name \
  network_project network_label <<<"$network_metadata"
if [[ "$inspected_network_id" != "$network_id" ||
  "$inspected_network_name" != "$EXPECTED_GATEWAY_NETWORK" ||
  "$network_project" != "$compose_project" ||
  "$network_label" != "agent_internal" ]]; then
  echo "OBS_E2E_GUARD_NETWORK_IDENTITY_MISMATCH" >&2
  exit 79
fi

if ((10#$port < 1 || 10#$port > 65535)); then
  echo "OBS_E2E_GUARD_PORT_INVALID" >&2
  exit 72
fi
port="$((10#$port))"
if [[ -n "${denied_ports[$port]:-}" ]]; then
  echo "OBS_E2E_GUARD_FORMAL_PORT_DENIED" >&2
  exit 73
fi

is_ipv4() {
  local value="$1"
  local octet
  [[ "$value" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  IFS='.' read -r -a octets <<<"$value"
  for octet in "${octets[@]}"; do
    ((10#$octet <= 255)) || return 1
  done
}

is_ipv6() {
  [[ "$1" == *:* && "$1" =~ ^[0-9A-Fa-f:]+$ ]]
}

is_local_ip() {
  local value="$1"
  local local_ip
  if [[ "$value" == 127.* || "$value" == "::1" ]]; then
    return 0
  fi
  for local_ip in $(hostname -I 2>/dev/null); do
    [[ "$value" == "$local_ip" ]] && return 0
  done
  return 1
}

if [[ "$host" == "0.0.0.0" || "$host" == "::" ]]; then
  echo "OBS_E2E_GUARD_ORIGIN_HOST_INVALID" >&2
  exit 69
fi
origin_ips=()
if is_ipv4 "$host" || is_ipv6 "$host"; then
  origin_ips+=("$host")
else
  echo "OBS_E2E_GUARD_ORIGIN_HOST_LITERAL_REQUIRED" >&2
  exit 69
fi
for origin_ip in "${origin_ips[@]}"; do
  if ! is_local_ip "$origin_ip"; then
    echo "OBS_E2E_GUARD_ORIGIN_NOT_LOCAL" >&2
    exit 79
  fi
done

mapping_proved=false
IFS=',' read -r -a port_bindings <<<"$port_binding_csv"
for binding in "${port_bindings[@]}"; do
  [[ -z "$binding" ]] && continue
  IFS='@' read -r container_port host_ip host_port extra <<<"$binding"
  if [[ -n "${extra:-}" || ! "$container_port" =~ ^([0-9]{1,5})/tcp$ ]]; then
    continue
  fi
  container_port_number="${BASH_REMATCH[1]}"
  if [[ ! "$host_port" =~ ^[0-9]{1,5}$ ]] ||
    ((10#$container_port_number < 1 || 10#$container_port_number > 65535)) ||
    ((10#$host_port < 1 || 10#$host_port > 65535)) ||
    ((10#$host_port != port)); then
    continue
  fi
  for origin_ip in "${origin_ips[@]}"; do
    if [[ "$host_ip" == "0.0.0.0" ]] && is_ipv4 "$origin_ip"; then
      mapping_proved=true
    elif [[ "$host_ip" == "::" ]] && is_ipv6 "$origin_ip"; then
      mapping_proved=true
    elif [[ "$host_ip" == "$origin_ip" ]]; then
      mapping_proved=true
    fi
  done
done
if [[ "$mapping_proved" != true ]]; then
  echo "OBS_E2E_GUARD_ORIGIN_CONTAINER_MISMATCH" >&2
  exit 79
fi

echo "OBS_E2E_GUARD_OK environment=$environment project=$compose_project origin=$origin gateway=$actual_name"
