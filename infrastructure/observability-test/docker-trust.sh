#!/usr/bin/env bash
# Shared fail-closed Docker/process safety for Page 6 test-only scripts.
PAGE6_DOCKER_BIN=/usr/bin/docker
PAGE6_DOCKER_CONTEXT=default
PAGE6_DOCKER_ENDPOINT=unix:///var/run/docker.sock
PAGE6_DOCKER_SOCKET=/run/docker.sock
PAGE6_DOCKER_DAEMON='3196b392-cce0-4178-a30a-2a9ff44d271c|afs2600151|/var/lib/docker'
PAGE6_PROJECT=contract-review-code-dev
PAGE6_ENVIRONMENT=test
PAGE6_SCOPE=contract-review-code-dev
PAGE6_RUN_LABEL=com.aituge.page6.run-id
declare -gA PAGE6_CONTAINER_NAME=() PAGE6_CONTAINER_RUN=()
declare -gA PAGE6_NETWORK_NAME=() PAGE6_NETWORK_RUN=()
declare -gA PAGE6_CONTAINER_QUARANTINE_RUN=() PAGE6_CONTAINER_QUARANTINE_RAW_ID=()
declare -gA PAGE6_NETWORK_QUARANTINE_RUN=() PAGE6_NETWORK_QUARANTINE_RAW_ID=()
declare -gA PAGE6_PROCESS_UID=() PAGE6_PROCESS_START=() PAGE6_PROCESS_PGID=()
declare -gA PAGE6_PROCESS_SESSION=() PAGE6_PROCESS_COMMAND_HASH=()
PAGE6_LAST_CONTAINER_ID= PAGE6_LAST_NETWORK_ID=

page6_error() { printf 'page6-safety: %s\n' "$*" >&2; }
page6_forbidden_word() {
  local v=${1,,}
  [[ $v == *formal* || $v == *production* || $v == *prod* ]]
}
page6_assert_run_id() {
  [[ ${1:-} =~ ^[0-9a-f]{32}$ ]] ||
    { page6_error 'invalid Page 6 run id'; return 1; }
}
page6_assert_safe_test_name() {
  local n=${1:?resource name required}
  [[ $n == contract-review-code-dev-* || $n == contract-review-dev-* ]] ||
    { page6_error 'name outside approved test prefix'; return 1; }
  ! page6_forbidden_word "$n" || { page6_error 'forbidden environment token in name'; return 1; }
}
page6_assert_probe_name() {
  page6_assert_safe_test_name "$1" || return 1
  [[ $1 == contract-review-code-dev-page6-* ]] || { page6_error 'name outside Page 6 prefix'; return 1; }
}
page6_reject_docker_environment_overrides() {
  local k
  for k in DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG; do
    [[ ! -v $k ]] || { page6_error "$k must be unset"; return 1; }
  done
}
page6_docker() {
  page6_reject_docker_environment_overrides || return 1
  env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH -u DOCKER_CONFIG \
    "$PAGE6_DOCKER_BIN" --context "$PAGE6_DOCKER_CONTEXT" "$@"
}
page6_verify_docker_daemon() {
  local v target meta
  page6_reject_docker_environment_overrides || return 1
  [[ -x $PAGE6_DOCKER_BIN ]] || { page6_error 'pinned Docker binary unavailable'; return 1; }
  v=$(page6_docker context show) || return 1
  [[ $v == "$PAGE6_DOCKER_CONTEXT" ]] || { page6_error 'Docker context changed'; return 1; }
  v=$(page6_docker context inspect default --format '{{.Endpoints.docker.Host}}|{{json .TLSMaterial}}') || return 1
  [[ $v == "$PAGE6_DOCKER_ENDPOINT|{}" ]] || { page6_error 'Docker endpoint/TLS changed'; return 1; }
  target=$(readlink -f -- /var/run/docker.sock) || return 1
  [[ $target == "$PAGE6_DOCKER_SOCKET" && -S $target ]] || { page6_error 'Docker socket changed'; return 1; }
  meta=$(stat -Lc '%F|%a|%u|%g' -- "$target") || return 1
  [[ $meta == 'socket|660|0|984' ]] || { page6_error 'Docker socket metadata changed'; return 1; }
  getent group 984 | grep -q '^docker:' || { page6_error 'Docker socket group changed'; return 1; }
  v=$(page6_docker info --format '{{.ID}}|{{.Name}}|{{.DockerRootDir}}') || return 1
  [[ $v == "$PAGE6_DOCKER_DAEMON" ]] || { page6_error 'Docker daemon identity changed'; return 1; }
}
page6_new_run_id() {
  local v; v=$(tr -d '-' < /proc/sys/kernel/random/uuid)
  [[ $v =~ ^[0-9a-f]{32}$ ]] || return 1; printf '%s\n' "$v"
}

page6_register_container() {
  [[ $1 =~ ^[0-9a-f]{64}$ ]] || { page6_error 'invalid container id'; return 1; }
  page6_assert_probe_name "$2" || return 1
  page6_assert_run_id "$3" || return 1
  PAGE6_CONTAINER_NAME[$1]=$2; PAGE6_CONTAINER_RUN[$1]=$3
}
page6_register_network() {
  [[ $1 =~ ^[0-9a-f]{64}$ ]] || { page6_error 'invalid network id'; return 1; }
  page6_assert_probe_name "$2" || return 1
  page6_assert_run_id "$3" || return 1
  PAGE6_NETWORK_NAME[$1]=$2; PAGE6_NETWORK_RUN[$1]=$3
}
page6_validate_container() {
  local id=$1 n=${PAGE6_CONTAINER_NAME[$1]-} r=${PAGE6_CONTAINER_RUN[$1]-} v
  [[ -n $n && -n $r ]] || { page6_error 'container absent from run ledger'; return 1; }
  page6_verify_docker_daemon || return 1
  v=$(page6_docker inspect --type container --format \
    '{{.Id}}|{{.Name}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.aituge.environment"}}|{{index .Config.Labels "com.aituge.observability.scope"}}|{{index .Config.Labels "com.aituge.page6.run-id"}}' "$id") || return 1
  [[ $v == "$id|/$n|$PAGE6_PROJECT|$PAGE6_ENVIRONMENT|$PAGE6_SCOPE|$r" ]] || {
    page6_error 'container identity/labels changed; refused'; return 1;
  }
  page6_assert_probe_name "$n"
}
page6_validate_network() {
  local id=$1 n=${PAGE6_NETWORK_NAME[$1]-} r=${PAGE6_NETWORK_RUN[$1]-} v
  [[ -n $n && -n $r ]] || { page6_error 'network absent from run ledger'; return 1; }
  page6_verify_docker_daemon || return 1
  v=$(page6_docker network inspect --format \
    '{{.Id}}|{{.Name}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.aituge.environment"}}|{{index .Labels "com.aituge.observability.scope"}}|{{index .Labels "com.aituge.page6.run-id"}}' "$id") || return 1
  [[ $v == "$id|$n|$PAGE6_PROJECT|$PAGE6_ENVIRONMENT|$PAGE6_SCOPE|$r" ]] || {
    page6_error 'network identity/labels changed; refused'; return 1;
  }
  page6_assert_probe_name "$n"
}
page6_record_container_quarantine() {
  local n=$1 r=$2 raw=${3-}
  page6_assert_probe_name "$n" || return 1
  page6_assert_run_id "$r" || return 1
  PAGE6_CONTAINER_QUARANTINE_RUN[$n]=$r
  PAGE6_CONTAINER_QUARANTINE_RAW_ID[$n]=$raw
}
page6_record_network_quarantine() {
  local n=$1 r=$2 raw=${3-}
  page6_assert_probe_name "$n" || return 1
  page6_assert_run_id "$r" || return 1
  PAGE6_NETWORK_QUARANTINE_RUN[$n]=$r
  PAGE6_NETWORK_QUARANTINE_RAW_ID[$n]=$raw
}
page6_forget_container_quarantine() {
  local n=$1
  unset 'PAGE6_CONTAINER_QUARANTINE_RUN[$n]' \
    'PAGE6_CONTAINER_QUARANTINE_RAW_ID[$n]'
}
page6_forget_network_quarantine() {
  local n=$1
  unset 'PAGE6_NETWORK_QUARANTINE_RUN[$n]' \
    'PAGE6_NETWORK_QUARANTINE_RAW_ID[$n]'
}
page6_quarantine_manual_action() {
  local kind=$1 n=$2 r=$3 raw=$4 raw_hash
  raw_hash=$(printf '%s' "$raw" | sha256sum | awk '{print $1}') || raw_hash=unavailable
  page6_error "${kind}_QUARANTINE_MANUAL_ACTION_REQUIRED name=$n run-id=$r raw-id-sha256=$raw_hash"
  return 1
}
page6_find_quarantined_container_id() {
  local n=$1 raw=$2 output candidate name extra exact_id= raw_name= raw_seen=0
  output=$(page6_docker ps -a --no-trunc --format '{{.ID}}|{{.Names}}') || return 2
  while IFS='|' read -r candidate name extra; do
    [[ -n $candidate || -n $name ]] || continue
    [[ -z $extra && $candidate =~ ^[0-9a-f]{64}$ ]] || return 2
    if [[ $name == "$n" ]]; then
      [[ -z $exact_id ]] || return 2
      exact_id=$candidate
    fi
    if [[ $raw =~ ^[0-9a-f]{64}$ && $candidate == "$raw" ]]; then
      raw_seen=1
      raw_name=$name
    fi
  done <<< "$output"
  if (( raw_seen )) && [[ $raw_name != "$n" ]]; then
    return 2
  fi
  if [[ -n $exact_id ]]; then
    [[ ! $raw =~ ^[0-9a-f]{64}$ || $raw == "$exact_id" ]] || return 2
    printf '%s\n' "$exact_id"
    return 0
  fi
  return 1
}
page6_find_quarantined_network_id() {
  local n=$1 raw=$2 output candidate name extra exact_id= raw_name= raw_seen=0
  output=$(page6_docker network ls --no-trunc --format '{{.ID}}|{{.Name}}') || return 2
  while IFS='|' read -r candidate name extra; do
    [[ -n $candidate || -n $name ]] || continue
    [[ -z $extra && $candidate =~ ^[0-9a-f]{64}$ ]] || return 2
    if [[ $name == "$n" ]]; then
      [[ -z $exact_id ]] || return 2
      exact_id=$candidate
    fi
    if [[ $raw =~ ^[0-9a-f]{64}$ && $candidate == "$raw" ]]; then
      raw_seen=1
      raw_name=$name
    fi
  done <<< "$output"
  if (( raw_seen )) && [[ $raw_name != "$n" ]]; then
    return 2
  fi
  if [[ -n $exact_id ]]; then
    [[ ! $raw =~ ^[0-9a-f]{64}$ || $raw == "$exact_id" ]] || return 2
    printf '%s\n' "$exact_id"
    return 0
  fi
  return 1
}
page6_cleanup_quarantined_container() {
  local n=$1 r=${PAGE6_CONTAINER_QUARANTINE_RUN[$1]-}
  local raw=${PAGE6_CONTAINER_QUARANTINE_RAW_ID[$1]-} id= state inspect_value
  [[ -n $r ]] || { page6_error 'container absent from quarantine ledger'; return 1; }
  page6_assert_probe_name "$n" || return 1
  page6_assert_run_id "$r" || return 1
  page6_verify_docker_daemon || return 1
  if id=$(page6_find_quarantined_container_id "$n" "$raw"); then
    state=0
  else
    state=$?
  fi
  if (( state == 1 )); then
    [[ $raw =~ ^[0-9a-f]{64}$ ]] &&
      unset 'PAGE6_CONTAINER_NAME[$raw]' 'PAGE6_CONTAINER_RUN[$raw]'
    page6_forget_container_quarantine "$n"
    return 0
  fi
  if (( state != 0 )); then
    page6_quarantine_manual_action CONTAINER "$n" "$r" "$raw"
    return 1
  fi
  if ! inspect_value=$(page6_docker inspect --type container --format \
    '{{.Id}}|{{.Name}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.aituge.environment"}}|{{index .Config.Labels "com.aituge.observability.scope"}}|{{index .Config.Labels "com.aituge.page6.run-id"}}' "$id"); then
    page6_quarantine_manual_action CONTAINER "$n" "$r" "$raw"
    return 1
  fi
  if [[ $inspect_value != "$id|/$n|$PAGE6_PROJECT|$PAGE6_ENVIRONMENT|$PAGE6_SCOPE|$r" ]]; then
    page6_quarantine_manual_action CONTAINER "$n" "$r" "$raw"
    return 1
  fi
  if ! page6_docker rm -f "$id" >/dev/null; then
    page6_quarantine_manual_action CONTAINER "$n" "$r" "$raw"
    return 1
  fi
  unset 'PAGE6_CONTAINER_NAME[$id]' 'PAGE6_CONTAINER_RUN[$id]'
  if [[ $raw =~ ^[0-9a-f]{64}$ && $raw != "$id" ]]; then
    unset 'PAGE6_CONTAINER_NAME[$raw]' 'PAGE6_CONTAINER_RUN[$raw]'
  fi
  page6_forget_container_quarantine "$n"
  return 0
}
page6_cleanup_quarantined_network() {
  local n=$1 r=${PAGE6_NETWORK_QUARANTINE_RUN[$1]-}
  local raw=${PAGE6_NETWORK_QUARANTINE_RAW_ID[$1]-} id= state inspect_value
  [[ -n $r ]] || { page6_error 'network absent from quarantine ledger'; return 1; }
  page6_assert_probe_name "$n" || return 1
  page6_assert_run_id "$r" || return 1
  page6_verify_docker_daemon || return 1
  if id=$(page6_find_quarantined_network_id "$n" "$raw"); then
    state=0
  else
    state=$?
  fi
  if (( state == 1 )); then
    [[ $raw =~ ^[0-9a-f]{64}$ ]] &&
      unset 'PAGE6_NETWORK_NAME[$raw]' 'PAGE6_NETWORK_RUN[$raw]'
    page6_forget_network_quarantine "$n"
    return 0
  fi
  if (( state != 0 )); then
    page6_quarantine_manual_action NETWORK "$n" "$r" "$raw"
    return 1
  fi
  if ! inspect_value=$(page6_docker network inspect --format \
    '{{.Id}}|{{.Name}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.aituge.environment"}}|{{index .Labels "com.aituge.observability.scope"}}|{{index .Labels "com.aituge.page6.run-id"}}' "$id"); then
    page6_quarantine_manual_action NETWORK "$n" "$r" "$raw"
    return 1
  fi
  if [[ $inspect_value != "$id|$n|$PAGE6_PROJECT|$PAGE6_ENVIRONMENT|$PAGE6_SCOPE|$r" ]]; then
    page6_quarantine_manual_action NETWORK "$n" "$r" "$raw"
    return 1
  fi
  if ! page6_docker network rm "$id" >/dev/null; then
    page6_quarantine_manual_action NETWORK "$n" "$r" "$raw"
    return 1
  fi
  unset 'PAGE6_NETWORK_NAME[$id]' 'PAGE6_NETWORK_RUN[$id]'
  if [[ $raw =~ ^[0-9a-f]{64}$ && $raw != "$id" ]]; then
    unset 'PAGE6_NETWORK_NAME[$raw]' 'PAGE6_NETWORK_RUN[$raw]'
  fi
  page6_forget_network_quarantine "$n"
  return 0
}
page6_create_network() {
  local n=$1 r=$2 id= state; shift 2; PAGE6_LAST_NETWORK_ID=
  page6_assert_probe_name "$n" || return 1
  page6_assert_run_id "$r" || return 1
  page6_verify_docker_daemon || return 1
  page6_record_network_quarantine "$n" "$r" "" || return 1
  if id=$(page6_docker network create --label "com.docker.compose.project=$PAGE6_PROJECT" \
    --label "com.aituge.environment=$PAGE6_ENVIRONMENT" \
    --label "com.aituge.observability.scope=$PAGE6_SCOPE" --label "$PAGE6_RUN_LABEL=$r" \
    "$@" "$n"); then
    state=0
  else
    state=$?
  fi
  PAGE6_NETWORK_QUARANTINE_RAW_ID[$n]=$id
  if (( state != 0 )); then
    page6_error 'network create command failed; entering quarantine recovery'
    page6_cleanup_quarantined_network "$n" || return 1
    return 1
  fi
  if ! page6_register_network "$id" "$n" "$r"; then
    page6_error 'network post-create registration failed; entering quarantine recovery'
    page6_cleanup_quarantined_network "$n" || return 1
    return 1
  fi
  if ! page6_validate_network "$id"; then
    page6_error 'network post-create validation failed; entering quarantine recovery'
    page6_cleanup_quarantined_network "$n" || return 1
    return 1
  fi
  page6_forget_network_quarantine "$n"
  PAGE6_LAST_NETWORK_ID=$id
  return 0
}
page6_create_container() {
  local n=$1 r=$2 id= state; shift 2; PAGE6_LAST_CONTAINER_ID=
  page6_assert_probe_name "$n" || return 1
  page6_assert_run_id "$r" || return 1
  page6_verify_docker_daemon || return 1
  page6_record_container_quarantine "$n" "$r" "" || return 1
  if id=$(page6_docker create --pull never --name "$n" --label "com.docker.compose.project=$PAGE6_PROJECT" \
    --label "com.aituge.environment=$PAGE6_ENVIRONMENT" \
    --label "com.aituge.observability.scope=$PAGE6_SCOPE" --label "$PAGE6_RUN_LABEL=$r" "$@"); then
    state=0
  else
    state=$?
  fi
  PAGE6_CONTAINER_QUARANTINE_RAW_ID[$n]=$id
  if (( state != 0 )); then
    page6_error 'container create command failed; entering quarantine recovery'
    page6_cleanup_quarantined_container "$n" || return 1
    return 1
  fi
  if ! page6_register_container "$id" "$n" "$r"; then
    page6_error 'container post-create registration failed; entering quarantine recovery'
    page6_cleanup_quarantined_container "$n" || return 1
    return 1
  fi
  if ! page6_validate_container "$id"; then
    page6_error 'container post-create validation failed; entering quarantine recovery'
    page6_cleanup_quarantined_container "$n" || return 1
    return 1
  fi
  page6_forget_container_quarantine "$n"
  PAGE6_LAST_CONTAINER_ID=$id
  return 0
}
page6_container_logs() { local id=$1; shift; page6_validate_container "$id" && page6_docker logs "$@" "$id"; }
page6_cleanup_container() {
  local id=$1; page6_validate_container "$id" || return 1
  page6_docker rm -f "$id" >/dev/null || return 1
  unset 'PAGE6_CONTAINER_NAME[$id]' 'PAGE6_CONTAINER_RUN[$id]'
}
page6_cleanup_network() {
  local id=$1; page6_validate_network "$id" || return 1
  page6_docker network rm "$id" >/dev/null || return 1
  unset 'PAGE6_NETWORK_NAME[$id]' 'PAGE6_NETWORK_RUN[$id]'
}
page6_cleanup_registered() {
  local id name rc=0
  for name in "${!PAGE6_CONTAINER_QUARANTINE_RUN[@]}"; do
    page6_cleanup_quarantined_container "$name" || rc=1
  done
  for name in "${!PAGE6_NETWORK_QUARANTINE_RUN[@]}"; do
    page6_cleanup_quarantined_network "$name" || rc=1
  done
  for id in "${!PAGE6_CONTAINER_NAME[@]}"; do page6_cleanup_container "$id" || rc=1; done
  for id in "${!PAGE6_NETWORK_NAME[@]}"; do page6_cleanup_network "$id" || rc=1; done
  return "$rc"
}

page6_capture_process_identity() {
  local pid=$1 raw rest uid hash; local -a f
  [[ $pid =~ ^[0-9]+$ && -r /proc/$pid/stat && -r /proc/$pid/cmdline ]] || { page6_error 'process vanished'; return 1; }
  uid=$(stat -Lc '%u' "/proc/$pid") || return 1
  [[ $uid == "$(id -u)" ]] || { page6_error 'process owner mismatch'; return 1; }
  raw=$(<"/proc/$pid/stat"); rest=${raw#*) }; read -r -a f <<< "$rest"
  [[ ${#f[@]} -ge 20 && ${f[2]} == "$pid" && ${f[3]} == "$pid" ]] || {
    page6_error 'process is not an independent group/session leader'; return 1;
  }
  hash=$(sha256sum "/proc/$pid/cmdline" | awk '{print $1}') || return 1
  PAGE6_PROCESS_UID[$pid]=$uid; PAGE6_PROCESS_PGID[$pid]=${f[2]}
  PAGE6_PROCESS_SESSION[$pid]=${f[3]}; PAGE6_PROCESS_START[$pid]=${f[19]}
  PAGE6_PROCESS_COMMAND_HASH[$pid]=$hash
}
page6_verify_process_identity() {
  local pid=$1 raw rest uid hash; local -a f
  [[ -n ${PAGE6_PROCESS_START[$pid]-} ]] || { page6_error 'process absent from run ledger'; return 2; }
  [[ -e /proc/$pid ]] || return 1
  [[ -r /proc/$pid/stat && -r /proc/$pid/cmdline ]] || return 2
  uid=$(stat -Lc '%u' "/proc/$pid") || return 2
  raw=$(<"/proc/$pid/stat"); rest=${raw#*) }; read -r -a f <<< "$rest"
  [[ ${#f[@]} -ge 20 ]] || return 2
  hash=$(sha256sum "/proc/$pid/cmdline" | awk '{print $1}') || return 2
  [[ $uid == "${PAGE6_PROCESS_UID[$pid]}" && ${f[2]} == "${PAGE6_PROCESS_PGID[$pid]}" && \
     ${f[3]} == "${PAGE6_PROCESS_SESSION[$pid]}" && ${f[19]} == "${PAGE6_PROCESS_START[$pid]}" && \
     $hash == "${PAGE6_PROCESS_COMMAND_HASH[$pid]}" && ${f[2]} == "$pid" && ${f[3]} == "$pid" ]] || {
    page6_error 'process identity changed; signal refused'; return 2;
  }
}
page6_forget_process() {
  local p=$1; unset 'PAGE6_PROCESS_UID[$p]' 'PAGE6_PROCESS_START[$p]' 'PAGE6_PROCESS_PGID[$p]' \
    'PAGE6_PROCESS_SESSION[$p]' 'PAGE6_PROCESS_COMMAND_HASH[$p]'
}
page6_terminate_process_group() {
  local p=$1 state i
  if page6_verify_process_identity "$p"; then state=0; else state=$?; fi
  (( state == 1 )) && { page6_forget_process "$p"; return 0; }
  (( state == 0 )) || return 1
  kill -TERM -- "-$p" 2>/dev/null || true
  for i in {1..50}; do [[ -e /proc/$p ]] || { page6_forget_process "$p"; return 0; }; sleep 0.1; done
  if page6_verify_process_identity "$p"; then state=0; else state=$?; fi
  (( state == 1 )) && { page6_forget_process "$p"; return 0; }
  (( state == 0 )) || return 1
  kill -KILL -- "-$p" 2>/dev/null || true
  for i in {1..20}; do [[ -e /proc/$p ]] || { page6_forget_process "$p"; return 0; }; sleep 0.1; done
  page6_error 'owned process group did not terminate'; return 1
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  page6_verify_docker_daemon
  printf 'page6 docker trust root: PASS (%s)\n' "$PAGE6_DOCKER_DAEMON"
fi
