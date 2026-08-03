#!/usr/bin/bash -p
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
runner="$repo_root/scripts/observability-contracts/run-contract-suite.sh"
work_dir=$(mktemp -d /tmp/obs70-runner-safety-XXXXXX)
trusted_bin="$work_dir/trusted"
hostile_bin="$work_dir/hostile"
docker_log="$work_dir/docker.log"
path_docker_log="$work_dir/path-docker.log"
bash_log="$work_dir/bash.log"
bash_env_file="$work_dir/bash-env"
bash_env_marker="$work_dir/bash-env.executed"
docker_state="$work_dir/docker.state"
inspect_count="$work_dir/inspect.count"
python_log="$work_dir/python.log"
stdout_file="$work_dir/stdout"
stderr_file="$work_dir/stderr"
container_id=$(printf 'a%.0s' {1..64})
provenance_runner=""
instrumented_runner=""
mkdir -p "$trusted_bin" "$hostile_bin"

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$instrumented_runner" ]]; then
    case "$instrumented_runner" in
      "$repo_root"/scripts/observability-contracts/.obs70-contract-runner-safety-*)
  if [[ -n "$provenance_runner" ]]; then
    case "$provenance_runner" in
      "$repo_root"/scripts/observability-contracts/.obs70-contract-provenance-safety-*)
        rm -f -- "$provenance_runner" ;;
      *) echo "OBS_RUNNER_PROVENANCE_RUNNER_CLEANUP_REFUSED" >&2; status=1 ;;
    esac
  fi
        rm -f -- "$instrumented_runner" ;;
      *) echo "OBS_RUNNER_SAFETY_RUNNER_CLEANUP_REFUSED" >&2; status=1 ;;
    esac
  fi
  case "$work_dir" in
    /tmp/obs70-runner-safety-*) rm -rf -- "$work_dir" ;;
    *) echo "OBS_RUNNER_SAFETY_CLEANUP_REFUSED" >&2; status=1 ;;
  esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cat >"$hostile_bin/docker" <<'HOSTILE_DOCKER'
#!/bin/sh
set -eu
printf '%s\n' invoked >>"$PATH_DOCKER_LOG"
exit 99
HOSTILE_DOCKER
chmod 700 "$hostile_bin/docker"
cat >"$hostile_bin/bash" <<'HOSTILE_BASH'
#!/bin/sh
set -eu
printf '%s\n' invoked >>"$OBS70_FAKE_BASH_LOG"
exit 99
HOSTILE_BASH
chmod 700 "$hostile_bin/bash"
printf '%s\n' 'printf executed >"$OBS70_BASH_ENV_MARKER"' >"$bash_env_file"
chmod 600 "$bash_env_file"

cat >"$trusted_bin/docker" <<'FAKE_DOCKER'
#!/bin/sh
set -eu
printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
if [ "$1" = context ] && [ "$2" = show ]; then
  printf '%s\n' "$FAKE_DOCKER_CONTEXT"
  exit 0
fi
if [ "$1" = context ] && [ "$2" = inspect ]; then
  printf '%s\n' 'unix:///var/run/docker.sock'
  exit 0
fi
if [ "$1" = --context ]; then
  [ "$2" = default ] || exit 92
  shift 2
fi
case "$1" in
  info)
    printf '%s|afs2600151|/var/lib/docker\n' "$FAKE_DOCKER_DAEMON_ID"
    ;;
  create)
    name=''
    run_id=''
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --name)
          shift
          name=$1
          ;;
        --label)
          shift
          case "$1" in
            com.aituge.run-id=*) run_id=${1#*=} ;;
          esac
          ;;
      esac
      shift
    done
    case "$FAKE_DOCKER_MODE" in
      conflict-absent)
        exit 125
        ;;
      nonzero-owned)
        printf '%s|%s|owned\n' "$name" "$run_id" >"$FAKE_DOCKER_STATE"
        exit 125
        ;;
      conflict-foreign)
        printf '%s|ffffffffffffffff|foreign\n' "$name" >"$FAKE_DOCKER_STATE"
        exit 125
        ;;
      label-mismatch)
        printf '%s|ffffffffffffffff|foreign\n' "$name" >"$FAKE_DOCKER_STATE"
        printf '%s\n' "$FAKE_CONTAINER_ID"
        ;;
      name-mismatch)
        printf '%s|%s|wrong-name\n' "$name" "$run_id" >"$FAKE_DOCKER_STATE"
        printf '%s\n' "$FAKE_CONTAINER_ID"
        ;;
      id-malformed)
        printf '%s|%s|owned\n' "$name" "$run_id" >"$FAKE_DOCKER_STATE"
        printf '%s\n' malformed-id
        ;;
      *)
        printf '%s|%s|owned\n' "$name" "$run_id" >"$FAKE_DOCKER_STATE"
        printf '%s\n' "$FAKE_CONTAINER_ID"
        ;;
    esac
    ;;
  container)
    [ "$2" = ls ] || exit 94
    if [ -f "$FAKE_DOCKER_STATE" ]; then
      IFS='|' read -r name run_id ownership <"$FAKE_DOCKER_STATE"
      printf '%s|%s\n' "$FAKE_CONTAINER_ID" "$name"
    fi
    ;;
  inspect)
    count=0
    [ ! -f "$FAKE_INSPECT_COUNT" ] || count=$(cat "$FAKE_INSPECT_COUNT")
    count=$((count + 1))
    printf '%s' "$count" >"$FAKE_INSPECT_COUNT"
    if [ "$FAKE_DOCKER_MODE" = inspect-transient ] && [ "$count" -eq 1 ]; then
      exit 97
    fi
    [ -f "$FAKE_DOCKER_STATE" ] || exit 44
    IFS='|' read -r name run_id ownership <"$FAKE_DOCKER_STATE"
    actual_name=$name
    environment=test
    scope=obs70-contract-tests
    if [ "$ownership" = foreign ]; then
      scope=foreign-scope
    elif [ "$ownership" = wrong-name ]; then
      actual_name=foreign-container
    fi
    printf '%s|/%s|%s|%s|%s\n' \
      "$FAKE_CONTAINER_ID" "$actual_name" "$environment" "$scope" "$run_id"
    ;;
  start)
    exit 9
    ;;
  rm)
    [ "$2" = -f ] && [ "$3" = "$FAKE_CONTAINER_ID" ]
    rm -f -- "$FAKE_DOCKER_STATE"
    ;;
  *)
    exit 96
    ;;
esac
FAKE_DOCKER
chmod 755 "$trusted_bin/docker"

cat >"$work_dir/python" <<'FAKE_PYTHON'
#!/bin/sh
set -eu
if [ -n "${FAKE_EXPECTED_SNAPSHOT_SHA256:-}" ]; then
  snapshot_root="$TMPDIR/implementation-snapshots/page1-java"
  snapshot_file="$snapshot_root/continew-server/src/main/resources/db/changelog/mysql/business/observability_ledger_1_5.sql"
  manifest_file="${TMPDIR}-manifests/page1-java.tsv"
  [ -d "$snapshot_root" ] && [ ! -L "$snapshot_root" ]
  [ "$(/usr/bin/stat -Lc '%u:%g:%a:%F' -- "$snapshot_root")" = "$(/usr/bin/id -u):$(/usr/bin/id -g):700:directory" ]
  [ "$(/usr/bin/stat -Lc '%u:%g:%a:%h:%F' -- "$snapshot_file")" = "$(/usr/bin/id -u):$(/usr/bin/id -g):600:1:regular file" ]
  [ "$(/usr/bin/stat -Lc '%u:%g:%a:%h:%F' -- "$manifest_file")" = "$(/usr/bin/id -u):$(/usr/bin/id -g):600:1:regular file" ]
  actual_sha256=$(/usr/bin/sha256sum "$snapshot_file")
  actual_sha256=${actual_sha256%% *}
  [ "$actual_sha256" = "$FAKE_EXPECTED_SNAPSHOT_SHA256" ]
  snapshot_identity=$(/usr/bin/stat -Lc '%d:%i' -- "$snapshot_root")
  /usr/bin/grep -Fxq "$(printf 'root_identity\t%s' "$snapshot_identity")" "$manifest_file"
  /usr/bin/grep -Fxq "$(printf 'file\t%s\t%s' 'continew-server/src/main/resources/db/changelog/mysql/business/observability_ledger_1_5.sql' "$actual_sha256")" "$manifest_file"
  printf 'snapshot-ok root=%s sha256=%s root_mode=700 file_mode=600 manifest_mode=600\n' "$snapshot_root" "$actual_sha256" >>"$FAKE_PYTHON_LOG"
fi
printf '%s\n' "$*" >>"$FAKE_PYTHON_LOG"
exit 0
FAKE_PYTHON
chmod 700 "$work_dir/python"

grep -Fxq 'readonly DOCKER_CLI="/usr/bin/docker"' "$runner"
grep -Fxq 'readonly DOCKER_CLI_SHA256="d767d00af09e69cf053e9d923550fda999c2b5911c7a0a0a920b964e86b32d25"' "$runner"
grep -Fq 'cat-file blob "$approved_blob" >"$snapshot_file"' "$runner"
grep -Fq -- '--mount "type=bind,src=$validated_implementation_snapshot_root,dst=$destination,readonly"' "$runner"
fake_docker_sha=$(/usr/bin/sha256sum "$trusted_bin/docker" | /usr/bin/awk '{print $1}')
instrumented_runner="$repo_root/scripts/observability-contracts/.obs70-contract-runner-safety-${work_dir##*-}.sh"
sed \
  -e "s#^readonly DOCKER_CLI=\"/usr/bin/docker\"#readonly DOCKER_CLI=\"$trusted_bin/docker\"#" \
  -e "s#^readonly DOCKER_CLI_SHA256=\"[0-9a-f]*\"#readonly DOCKER_CLI_SHA256=\"$fake_docker_sha\"#" \
  -e "s#^readonly DOCKER_CLI_UID=0#readonly DOCKER_CLI_UID=$(id -u)#" \
  -e "s#^readonly DOCKER_CLI_GID=0#readonly DOCKER_CLI_GID=$(id -g)#" \
  "$runner" >"$instrumented_runner"
chmod 700 "$instrumented_runner"

run_case() {
  local mode=$1 context=$2 daemon_id=$3
  shift 3
  : >"$docker_log"
  : >"$path_docker_log"
  : >"$python_log"
  : >"$stdout_file"
  : >"$stderr_file"
  rm -f -- "$docker_state" "$inspect_count" "$bash_env_marker" "$bash_log"
  set +e
  env \
    PATH="$hostile_bin:/usr/bin:/bin" \
    BASH_ENV="$bash_env_file" ENV="$bash_env_file" SHELLOPTS=xtrace \
    OBS70_FAKE_BASH_LOG="$bash_log" \
    OBS70_BASH_ENV_MARKER="$bash_env_marker" \
    PATH_DOCKER_LOG="$path_docker_log" \
    FAKE_DOCKER_LOG="$docker_log" \
    FAKE_DOCKER_STATE="$docker_state" \
    FAKE_INSPECT_COUNT="$inspect_count" \
    FAKE_DOCKER_MODE="$mode" \
    FAKE_DOCKER_CONTEXT="$context" \
    FAKE_DOCKER_DAEMON_ID="$daemon_id" \
    FAKE_CONTAINER_ID="$container_id" \
    FAKE_PYTHON_LOG="$python_log" \
    "$@" "$instrumented_runner" >"$stdout_file" 2>"$stderr_file"
  case_status=$?
  set -e
  [[ ! -s "$path_docker_log" ]]
  [[ ! -s "$bash_log" ]]
  [[ ! -e "$bash_env_marker" ]]
}

trusted_daemon_id=3196b392-cce0-4178-a30a-2a9ff44d271c

run_case conflict-absent default "$trusted_daemon_id"
[[ "$case_status" -eq 68 ]]
grep -Fq 'OBS_CONTRACT_CONTAINER_CREATE_FAILED' "$stderr_file"
grep -Fq 'quarantine=absent' "$stderr_file"
! grep -Eq -- '(^| )rm -f( |$)' "$docker_log"
[[ ! -e "$docker_state" ]]

run_case nonzero-owned default "$trusted_daemon_id"
[[ "$case_status" -eq 68 ]]
grep -Fq 'quarantine=owned' "$stderr_file"
grep -Fq -- "rm -f $container_id" "$docker_log"
[[ ! -e "$docker_state" ]]

run_case id-malformed default "$trusted_daemon_id"
[[ "$case_status" -eq 68 ]]
grep -Fq 'OBS_CONTRACT_CONTAINER_ID_INVALID' "$stderr_file"
grep -Fq 'quarantine=owned' "$stderr_file"
grep -Fq -- "rm -f $container_id" "$docker_log"
[[ ! -e "$docker_state" ]]

run_case inspect-transient default "$trusted_daemon_id"
[[ "$case_status" -eq 68 ]]
grep -Fq 'OBS_CONTRACT_CONTAINER_OWNERSHIP_INVALID quarantine=registered' "$stderr_file"
grep -Fq -- "rm -f $container_id" "$docker_log"
[[ ! -e "$docker_state" ]]

run_case label-mismatch default "$trusted_daemon_id"
[[ "$case_status" -eq 74 ]]
grep -Fq 'OBS_CONTRACT_CONTAINER_OWNERSHIP_INVALID quarantine=registered' "$stderr_file"
grep -Fq 'OBS_CONTRACT_QUARANTINE_UNRESOLVED kind=container' "$stderr_file"
grep -Fq 'OBS_CONTRACT_CLEANUP_INCOMPLETE' "$stderr_file"
! grep -Eq -- '(^| )rm -f( |$)' "$docker_log"
[[ -e "$docker_state" ]]

run_case name-mismatch default "$trusted_daemon_id"
[[ "$case_status" -eq 74 ]]
grep -Fq 'OBS_CONTRACT_CONTAINER_OWNERSHIP_INVALID quarantine=registered' "$stderr_file"
! grep -Eq -- '(^| )rm -f( |$)' "$docker_log"
[[ -e "$docker_state" ]]

run_case conflict-foreign default "$trusted_daemon_id"
[[ "$case_status" -eq 74 ]]
grep -Fq 'OBS_CONTRACT_CONTAINER_CREATE_FAILED' "$stderr_file"
grep -Fq 'quarantine=foreign' "$stderr_file"
grep -Fq 'manual=true' "$stderr_file"
! grep -Eq -- '(^| )rm -f( |$)' "$docker_log"
[[ -e "$docker_state" ]]

run_case conflict-absent formal-context "$trusted_daemon_id"
[[ "$case_status" -eq 66 ]]
grep -Fq 'OBS_CONTRACT_DOCKER_CONTEXT_DENIED' "$stderr_file"
! grep -Eq -- '(^| )create( |$)' "$docker_log"

run_case conflict-absent default 00000000-0000-0000-0000-000000000000
[[ "$case_status" -eq 66 ]]
grep -Fq 'OBS_CONTRACT_DOCKER_CONTEXT_DENIED' "$stderr_file"
! grep -Eq -- '(^| )create( |$)' "$docker_log"

run_case owned-start-fail default "$trusted_daemon_id"
[[ "$case_status" -eq 9 ]]
grep -Fq -- "rm -f $container_id" "$docker_log"
[[ ! -e "$docker_state" ]]

provenance_root="$work_dir/provenance-page1"
provenance_file="$provenance_root/continew-server/src/main/resources/db/changelog/mysql/business/observability_ledger_1_5.sql"
mkdir -p "${provenance_file%/*}"
printf '%s\n' 'CREATE TABLE manifest_bound (id bigint);' >"$provenance_file"
/usr/bin/git -C "$provenance_root" init -q -b obs/10-java-ledger
/usr/bin/git -C "$provenance_root" config user.name obs70-safety
/usr/bin/git -C "$provenance_root" config user.email obs70-safety@example.invalid
/usr/bin/git -C "$provenance_root" remote add origin \
  git@github.com:AI-tuge/Javabackend.git
/usr/bin/git -C "$provenance_root" add -- "$provenance_file"
/usr/bin/git -C "$provenance_root" commit -q -m 'synthetic provenance fixture'
provenance_head=$(/usr/bin/git -C "$provenance_root" rev-parse HEAD)
provenance_sha256=$(/usr/bin/sha256sum "$provenance_file")
provenance_sha256=${provenance_sha256%% *}
/usr/bin/git -C "$provenance_root" branch obs/00-baseline "$provenance_head"

provenance_runner="$repo_root/scripts/observability-contracts/.obs70-contract-provenance-safety-${work_dir##*-}.sh"
sed \
  -e "s#^readonly PAGE1_APPROVED_COMMIT=.*#readonly PAGE1_APPROVED_COMMIT=\"$provenance_head\"#" \
  -e "s#^readonly JAVA_BASELINE=.*#readonly JAVA_BASELINE=\"$provenance_head\"#" \
  -e "s#/home/aituge/worktrees/obs-java-ledger#$provenance_root#g" \
  -e '/^      OBS_PAGE2_PYTHON_WORKTREE$/d' \
  -e '/^      OBS_PAGE3_PYTHON_WORKTREE$/d' \
  "$instrumented_runner" >"$provenance_runner"
chmod 700 "$provenance_runner"

resource_runner="$instrumented_runner"
instrumented_runner="$provenance_runner"
run_case conflict-absent default "$trusted_daemon_id" \
  FAKE_EXPECTED_SNAPSHOT_SHA256="$provenance_sha256" \
  OBS_CONTRACT_PYTHON_BIN="$work_dir/python" \
  OBS_REQUIRE_IMPLEMENTATION_DDL=1 \
  OBS_PAGE1_JAVA_WORKTREE="$provenance_root"
[[ "$case_status" -eq 0 ]]
grep -Fq 'OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_OK' "$stdout_file"
grep -Fq 'snapshot=git-blob-private' "$stdout_file"
grep -Fq 'snapshot-ok ' "$python_log"
grep -Fq "sha256=$provenance_sha256 root_mode=700 file_mode=600 manifest_mode=600" "$python_log"
snapshot_record=$(/usr/bin/grep -F 'snapshot-ok root=' "$python_log")
snapshot_root_record=${snapshot_record#*root=}
snapshot_root_record=${snapshot_root_record%% sha256=*}
case "$snapshot_root_record" in
  /tmp/obs70-pytest-*/implementation-snapshots/page1-java) ;;
  *) echo "OBS_RUNNER_SNAPSHOT_RECORD_INVALID" >&2; exit 1 ;;
esac
runner_temp_root=${snapshot_root_record%/implementation-snapshots/page1-java}
[[ ! -e "$runner_temp_root" && ! -e "${runner_temp_root}-manifests" ]]
[[ -s "$python_log" && ! -s "$docker_log" ]]

printf '%s\n' '-- dirty source must be refused' >>"$provenance_file"
run_case conflict-absent default "$trusted_daemon_id" \
  OBS_CONTRACT_PYTHON_BIN="$work_dir/python" \
  OBS_REQUIRE_IMPLEMENTATION_DDL=1 \
  OBS_PAGE1_JAVA_WORKTREE="$provenance_root"
[[ "$case_status" -eq 65 ]]
grep -Fq 'OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_INVALID' "$stderr_file"
[[ ! -s "$python_log" && ! -s "$docker_log" ]]
instrumented_runner="$resource_runner"

untrusted_root="$work_dir/untrusted"
mkdir "$untrusted_root"
run_case conflict-absent default "$trusted_daemon_id" \
  OBS_CONTRACT_PYTHON_BIN="$work_dir/python" \
  OBS_REQUIRE_IMPLEMENTATION_DDL=1 \
  OBS_PAGE1_JAVA_WORKTREE="$untrusted_root" \
  OBS_PAGE2_PYTHON_WORKTREE="$untrusted_root" \
  OBS_PAGE3_PYTHON_WORKTREE="$untrusted_root"
[[ "$case_status" -eq 65 ]]
grep -Fq 'OBS_CONTRACT_IMPLEMENTATION_ROOT_NOT_ALLOWLISTED' "$stderr_file"
[[ ! -s "$docker_log" && ! -s "$python_log" ]]

echo "OBS_RUNNER_RESOURCE_SAFETY_OK quarantine=recovered nonzero_create=reconciled foreign=refused name=exact docker_cli=absolute-sha256 path=hostile-denied provenance=git-blob-manifest dirty=refused cleanup=74 shell=privileged-bash-env-ignored"
