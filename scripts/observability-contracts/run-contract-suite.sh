#!/usr/bin/bash -p
set +x
set -euo pipefail
umask 077

unset BASH_ENV ENV CDPATH GLOBIGNORE POSIXLY_CORRECT 2>/dev/null || true
export -n BASHOPTS SHELLOPTS 2>/dev/null || true
readonly PATH="/usr/bin:/bin"
export PATH

for loader_override in LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT PYTHONPATH \
  PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE; do
  if [[ -v $loader_override ]]; then
    echo "OBS_CONTRACT_LOADER_OVERRIDE_DENIED name=$loader_override" >&2
    exit 64
  fi
done

readonly EXPECTED_DOCKER_CONTEXT="default"
readonly EXPECTED_DOCKER_ENDPOINT="unix:///var/run/docker.sock"
readonly EXPECTED_DOCKER_DAEMON_ID="3196b392-cce0-4178-a30a-2a9ff44d271c"
readonly EXPECTED_DOCKER_DAEMON_NAME="afs2600151"
readonly EXPECTED_DOCKER_ROOT_DIR="/var/lib/docker"
readonly RESOURCE_ENVIRONMENT="test"
readonly RESOURCE_SCOPE="obs70-contract-tests"
readonly JAVA_BASELINE="308d6bc343b0c64aed5fa49b3525b111d9d9b57b"
readonly PYTHON_BASELINE="7cc289e52850de78f0cd985f0cc4dfb2c74ff247"
readonly JAVA_ORIGIN="git@github.com:AI-tuge/Javabackend.git"
readonly PYTHON_ORIGIN="git@github.com:AI-tuge/Aituge-framework.git"
readonly PAGE1_APPROVED_COMMIT="ce65c64986d24d17e49f1381e38debcbf6ee4f74"
readonly PAGE2_APPROVED_COMMIT="0e6f00d1983130ced6f0158c50637b741bcdc678"
readonly PAGE3_APPROVED_COMMIT="6216b324bdd5d3da6a754ed466567853dbfa6fed"

readonly DOCKER_CLI="/usr/bin/docker"
readonly DOCKER_CLI_SHA256="d767d00af09e69cf053e9d923550fda999c2b5911c7a0a0a920b964e86b32d25"
readonly DOCKER_CLI_UID=0
readonly DOCKER_CLI_GID=0
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
"$repo_root/scripts/observability-contracts/verify-frozen-contract-source.sh"
work_dir=$(/usr/bin/mktemp -d /tmp/obs70-pytest-XXXXXX)
implementation_manifest_dir="${work_dir}-manifests"
implementation_snapshot_dir="$work_dir/implementation-snapshots"
mkdir -m 700 "$implementation_manifest_dir" "$implementation_snapshot_dir"
run_id=""
container_name=""
container_id=""
container_ids=()
declare -A container_names=()
pending_container_name=""
last_container_id=""
docker_cmd=()

verify_docker_cli_binding() {
  local actual_path actual_owner actual_group actual_mode actual_hash
  [[ -x "$DOCKER_CLI" && -f "$DOCKER_CLI" && ! -L "$DOCKER_CLI" ]] || {
    echo "OBS_CONTRACT_DOCKER_CLI_INVALID" >&2
    return 1
  }
  actual_path=$(/usr/bin/realpath -e -- "$DOCKER_CLI") || return 1
  IFS='|' read -r actual_owner actual_group actual_mode < <(
    /usr/bin/stat -Lc '%u|%g|%a' -- "$DOCKER_CLI"
  )
  actual_hash=$(/usr/bin/sha256sum "$DOCKER_CLI" | /usr/bin/awk '{print $1}') ||
    return 1
  [[ "$actual_path" == "$DOCKER_CLI" &&
    "$actual_owner" == "$DOCKER_CLI_UID" && "$actual_group" == "$DOCKER_CLI_GID" && "$actual_mode" == 755 &&
    "$actual_hash" == "$DOCKER_CLI_SHA256" ]] || {
    echo "OBS_CONTRACT_DOCKER_CLI_INVALID" >&2
    return 1
  }
}


verify_docker_context_binding() {
  local actual_context
  local actual_endpoint
  local daemon_metadata
  local actual_daemon_id
  local actual_daemon_name
  local actual_root_dir
  if [[ -v DOCKER_HOST || -v DOCKER_CONTEXT || -v DOCKER_TLS_VERIFY ||
    -v DOCKER_CERT_PATH || -v DOCKER_CONFIG ]]; then
    echo "OBS_CONTRACT_DOCKER_OVERRIDE_DENIED" >&2
    return 1
  fi
  actual_context=$("$DOCKER_CLI" context show 2>/dev/null) || {
    echo "OBS_CONTRACT_DOCKER_CONTEXT_UNAVAILABLE" >&2
    return 1
  }
  actual_endpoint=$("$DOCKER_CLI" context inspect "$EXPECTED_DOCKER_CONTEXT" \
    --format '{{.Endpoints.docker.Host}}' 2>/dev/null) || {
    echo "OBS_CONTRACT_DOCKER_CONTEXT_UNAVAILABLE" >&2
    return 1
  }
  daemon_metadata=$("$DOCKER_CLI" --context "$EXPECTED_DOCKER_CONTEXT" info \
    --format '{{.ID}}|{{.Name}}|{{.DockerRootDir}}' 2>/dev/null) || {
    echo "OBS_CONTRACT_DOCKER_DAEMON_UNAVAILABLE" >&2
    return 1
  }
  IFS='|' read -r actual_daemon_id actual_daemon_name actual_root_dir \
    <<<"$daemon_metadata"
  if [[ "$actual_context" != "$EXPECTED_DOCKER_CONTEXT" ||
    "$actual_endpoint" != "$EXPECTED_DOCKER_ENDPOINT" ||
    "$actual_daemon_id" != "$EXPECTED_DOCKER_DAEMON_ID" ||
    "$actual_daemon_name" != "$EXPECTED_DOCKER_DAEMON_NAME" ||
    "$actual_root_dir" != "$EXPECTED_DOCKER_ROOT_DIR" ]]; then
    echo "OBS_CONTRACT_DOCKER_CONTEXT_DENIED" >&2
    return 1
  fi
}

trusted_docker_ready() {
  verify_docker_cli_binding && verify_docker_context_binding
}

verify_owned_container() {
  local expected_id="$1" expected_name="$2"
  local metadata actual_id actual_name actual_environment actual_scope actual_run_id
  metadata=$("${docker_cmd[@]}" inspect \
    --format '{{.Id}}|{{.Name}}|{{index .Config.Labels "com.aituge.environment"}}|{{index .Config.Labels "com.aituge.scope"}}|{{index .Config.Labels "com.aituge.run-id"}}' \
    "$expected_id" 2>/dev/null) || return 1
  IFS='|' read -r actual_id actual_name actual_environment actual_scope actual_run_id \
    <<<"$metadata"
  [[ "$actual_id" == "$expected_id" &&
    "$actual_name" == "/$expected_name" &&
    "$actual_environment" == "$RESOURCE_ENVIRONMENT" &&
    "$actual_scope" == "$RESOURCE_SCOPE" &&
    "$actual_run_id" == "$run_id" ]]
}

container_discovery_state="unresolved"
discovered_container_id=""

discover_container_by_name() {
  local expected_name="$1"
  local listing listed_id listed_name metadata
  local actual_id actual_name actual_environment actual_scope actual_run_id
  container_discovery_state="unresolved"
  discovered_container_id=""
  trusted_docker_ready || return 0
  listing=$("${docker_cmd[@]}" container ls --all --no-trunc \
    --filter "name=^/$expected_name$" --format '{{.ID}}|{{.Names}}' 2>/dev/null) ||
    return 0
  listing="${listing//$'\r'/}"
  if [[ -z "$listing" ]]; then
    container_discovery_state="absent"
    return 0
  fi
  [[ "$listing" != *$'\n'* ]] || return 0
  IFS='|' read -r listed_id listed_name <<<"$listing"
  if [[ ! "$listed_id" =~ ^[0-9a-f]{64}$ || "$listed_name" != "$expected_name" ]]; then
    return 0
  fi
  metadata=$("${docker_cmd[@]}" inspect \
    --format '{{.Id}}|{{.Name}}|{{index .Config.Labels "com.aituge.environment"}}|{{index .Config.Labels "com.aituge.scope"}}|{{index .Config.Labels "com.aituge.run-id"}}' \
    "$listed_id" 2>/dev/null) || return 0
  IFS='|' read -r actual_id actual_name actual_environment actual_scope actual_run_id \
    <<<"$metadata"
  if [[ "$actual_id" != "$listed_id" || "$actual_name" != "/$expected_name" ]]; then
    return 0
  fi
  discovered_container_id="$listed_id"
  if [[ "$actual_environment" == "$RESOURCE_ENVIRONMENT" &&
    "$actual_scope" == "$RESOURCE_SCOPE" && "$actual_run_id" == "$run_id" ]]; then
    container_discovery_state="owned"
  else
    container_discovery_state="foreign"
  fi
}

register_container_candidate() {
  local id="$1" name="$2" existing
  [[ "$id" =~ ^[0-9a-f]{64}$ ]] || return 1
  for existing in "${container_ids[@]}"; do
    if [[ "$existing" == "$id" ]]; then
      container_names[$id]="$name"
      [[ "$pending_container_name" != "$name" ]] || pending_container_name=""
      return 0
    fi
  done
  container_ids+=("$id")
  container_names[$id]="$name"
  [[ "$pending_container_name" != "$name" ]] || pending_container_name=""
}

reconcile_pending_container() {
  local name="$1"
  discover_container_by_name "$name"
  case "$container_discovery_state" in
    owned) register_container_candidate "$discovered_container_id" "$name" ;;
    absent)
      [[ "$pending_container_name" != "$name" ]] || pending_container_name=""
      ;;
    foreign | unresolved) ;;
  esac
}

create_owned_container() {
  local name="$1" created_id create_status
  shift
  pending_container_name="$name"
  trusted_docker_ready || {
    echo "OBS_CONTRACT_CONTAINER_CREATE_BOUNDARY_INVALID name=$name" >&2
    return 1
  }
  set +e
  created_id=$("${docker_cmd[@]}" create \
    --name "$name" \
    --label "com.aituge.environment=$RESOURCE_ENVIRONMENT" \
    --label "com.aituge.scope=$RESOURCE_SCOPE" \
    --label "com.aituge.run-id=$run_id" \
    "$@")
  create_status=$?
  set -e
  created_id="${created_id//$'\r'/}"
  created_id="${created_id//$'\n'/}"
  if [[ "$create_status" -ne 0 ]]; then
    reconcile_pending_container "$name"
    echo "OBS_CONTRACT_CONTAINER_CREATE_FAILED name=$name quarantine=$container_discovery_state" >&2
    return 1
  fi
  if [[ ! "$created_id" =~ ^[0-9a-f]{64}$ ]]; then
    reconcile_pending_container "$name"
    echo "OBS_CONTRACT_CONTAINER_ID_INVALID name=$name quarantine=$container_discovery_state" >&2
    return 1
  fi
  register_container_candidate "$created_id" "$name"
  if ! verify_owned_container "$created_id" "$name"; then
    echo "OBS_CONTRACT_CONTAINER_OWNERSHIP_INVALID quarantine=registered name=$name" >&2
    return 1
  fi
  last_container_id="$created_id"
}


cleanup() {
  local original_status="$1"
  local cleanup_status=0 index resource_id resource_name
  trap - EXIT INT TERM
  set +e
  if [[ -n "$pending_container_name" ]]; then
    resource_name="$pending_container_name"
    reconcile_pending_container "$resource_name"
    case "$container_discovery_state" in
      owned | absent) ;;
      foreign | unresolved)
        echo "OBS_CONTRACT_QUARANTINE_UNRESOLVED kind=container name=$resource_name state=$container_discovery_state manual=true" >&2
        cleanup_status=1
        ;;
    esac
  fi
  for ((index = ${#container_ids[@]} - 1; index >= 0; index--)); do
    resource_id="${container_ids[$index]}"
    resource_name="${container_names[$resource_id]-}"
    if trusted_docker_ready && [[ -n "$resource_name" ]] &&
      verify_owned_container "$resource_id" "$resource_name"; then
      "${docker_cmd[@]}" rm -f "$resource_id" >/dev/null 2>&1 ||
        cleanup_status=1
    else
      echo "OBS_CONTRACT_QUARANTINE_UNRESOLVED kind=container id=$resource_id name=$resource_name manual=true" >&2
      cleanup_status=1
    fi
  done
  case "$implementation_manifest_dir" in
    /tmp/obs70-pytest-*-manifests) rm -rf -- "$implementation_manifest_dir" || cleanup_status=1 ;;
    *) echo "OBS_CONTRACT_MANIFEST_CLEANUP_REFUSED" >&2; cleanup_status=1 ;;
  esac
  case "$work_dir" in
    /tmp/obs70-pytest-*) rm -rf -- "$work_dir" || cleanup_status=1 ;;
    *) echo "OBS_CONTRACT_CLEANUP_REFUSED" >&2; cleanup_status=1 ;;
  esac
  if ((cleanup_status != 0)); then
    echo "OBS_CONTRACT_CLEANUP_INCOMPLETE original_status=$original_status" >&2
    exit 74
  fi
  if ((original_status != 0)); then
    exit "$original_status"
  fi
  exit 0
}
trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

export TMPDIR="$work_dir"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$work_dir/pycache"

for command_name in git realpath; do
  command -v "$command_name" >/dev/null || {
    echo "OBS_CONTRACT_TOOL_MISSING name=$command_name" >&2
    exit 64
  }
done

validated_implementation_head=""
validated_implementation_identity=""
validated_implementation_manifest_sha256=""
validated_implementation_commit=""
validated_implementation_snapshot_root=""

implementation_relative_files() {
  case "$1" in
    OBS_PAGE1_JAVA_WORKTREE)
      printf '%s\n' "continew-server/src/main/resources/db/changelog/mysql/business/observability_ledger_1_5.sql"
      ;;
    OBS_PAGE2_PYTHON_WORKTREE)
      printf '%s\n' \
        "backend/model_observability/migrations/001_model_invocation_ledger.up.sql" \
        "backend/model_observability/migrations/001_model_invocation_ledger.down.sql" \
        "backend/model_observability/migrations/002_model_observability_hardening.up.sql" \
        "backend/model_observability/migrations/002_model_observability_hardening.down.sql" \
        "backend/model_observability/migrate.py"
      ;;
    OBS_PAGE3_PYTHON_WORKTREE)
      printf '%s\n' \
        "backend/task_manager/observability_internal/migration.py" \
        "backend/task_manager/observability_internal/models.py"
      ;;
    *) return 1 ;;
  esac
}

validate_implementation_root() {
  local env_name="$1" source_root="$2" destination="$3" manifest_file="$4"
  local expected_path expected_branch expected_origin expected_baseline
  local approved_commit integration_root=0
  local actual_root actual_branch actual_origin actual_baseline actual_head
  local current_branch current_origin current_baseline current_head
  local relative_path absolute_file resolved_file file_metadata
  local approved_blob head_blob approved_sha256 working_sha256 manifest_sha256
  local source_root_identity current_source_identity snapshot_root_identity
  local current_snapshot_identity
  local snapshot_name snapshot_root snapshot_root_real snapshot_parent snapshot_file
  local snapshot_metadata snapshot_sha256 owner_uid owner_gid index
  local -a relative_paths=() approved_blobs=() approved_sha256s=() snapshot_files=()
  validated_implementation_head=""
  validated_implementation_identity=""
  validated_implementation_manifest_sha256=""
  validated_implementation_commit=""
  validated_implementation_snapshot_root=""

  case "$env_name:$source_root" in
    OBS_PAGE1_JAVA_WORKTREE:/home/aituge/worktrees/obs-java-ledger)
      expected_path="/home/aituge/worktrees/obs-java-ledger"
      expected_branch="obs/10-java-ledger"
      expected_origin="$JAVA_ORIGIN"
      expected_baseline="$JAVA_BASELINE"
      approved_commit="$PAGE1_APPROVED_COMMIT"
      ;;
    OBS_PAGE1_JAVA_WORKTREE:/home/aituge/worktrees/obs-integration/java)
      expected_path="/home/aituge/worktrees/obs-integration/java"
      expected_branch="obs/90-integration"
      expected_origin="$JAVA_ORIGIN"
      expected_baseline="$JAVA_BASELINE"
      approved_commit="$PAGE1_APPROVED_COMMIT"
      integration_root=1
      ;;
    OBS_PAGE2_PYTHON_WORKTREE:/home/aituge/worktrees/obs-python-model)
      expected_path="/home/aituge/worktrees/obs-python-model"
      expected_branch="obs/20-python-model"
      expected_origin="$PYTHON_ORIGIN"
      expected_baseline="$PYTHON_BASELINE"
      approved_commit="$PAGE2_APPROVED_COMMIT"
      ;;
    OBS_PAGE3_PYTHON_WORKTREE:/home/aituge/worktrees/obs-python-task-security)
      expected_path="/home/aituge/worktrees/obs-python-task-security"
      expected_branch="obs/30-python-task-security"
      expected_origin="$PYTHON_ORIGIN"
      expected_baseline="$PYTHON_BASELINE"
      approved_commit="$PAGE3_APPROVED_COMMIT"
      ;;
    OBS_PAGE2_PYTHON_WORKTREE:/home/aituge/worktrees/obs-integration/python)
      expected_path="/home/aituge/worktrees/obs-integration/python"
      expected_branch="obs/90-integration"
      expected_origin="$PYTHON_ORIGIN"
      expected_baseline="$PYTHON_BASELINE"
      approved_commit="$PAGE2_APPROVED_COMMIT"
      integration_root=1
      ;;
    OBS_PAGE3_PYTHON_WORKTREE:/home/aituge/worktrees/obs-integration/python)
      expected_path="/home/aituge/worktrees/obs-integration/python"
      expected_branch="obs/90-integration"
      expected_origin="$PYTHON_ORIGIN"
      expected_baseline="$PYTHON_BASELINE"
      approved_commit="$PAGE3_APPROVED_COMMIT"
      integration_root=1
      ;;
    *)
      echo "OBS_CONTRACT_IMPLEMENTATION_ROOT_NOT_ALLOWLISTED name=$env_name" >&2
      return 1
      ;;
  esac

  if [[ ! "$approved_commit" =~ ^[0-9a-f]{40}$ ]]; then
    echo "OBS_CONTRACT_IMPLEMENTATION_PIN_PENDING name=$env_name" >&2
    return 1
  fi
  if ((integration_root != 0)); then
    echo "OBS_CONTRACT_INTEGRATION_MANIFEST_PENDING name=$env_name" >&2
    return 1
  fi
  if [[ ! -d "$source_root" || -L "$source_root" ]]; then
    echo "OBS_CONTRACT_IMPLEMENTATION_ROOT_INVALID name=$env_name" >&2
    return 1
  fi
  actual_root=$(/usr/bin/realpath -e -- "$source_root" 2>/dev/null) || {
    echo "OBS_CONTRACT_IMPLEMENTATION_ROOT_INVALID name=$env_name" >&2
    return 1
  }
  if [[ "$actual_root" != "$expected_path" ||
    "$(/usr/bin/git -C "$actual_root" rev-parse --show-toplevel 2>/dev/null)" != "$expected_path" ||
    "$(/usr/bin/git -C "$actual_root" rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]] ||
    ! /usr/bin/git -C "$actual_root" worktree list --porcelain |
      /usr/bin/grep -Fxq "worktree $expected_path"; then
    echo "OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_INVALID name=$env_name" >&2
    return 1
  fi

  actual_branch=$(/usr/bin/git -C "$actual_root" branch --show-current)
  actual_origin=$(/usr/bin/git -C "$actual_root" config --get remote.origin.url)
  actual_baseline=$(/usr/bin/git -C "$actual_root" rev-parse \
    --verify "refs/heads/obs/00-baseline^{commit}" 2>/dev/null) || {
    echo "OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_INVALID name=$env_name" >&2
    return 1
  }
  actual_head=$(/usr/bin/git -C "$actual_root" rev-parse \
    --verify "HEAD^{commit}" 2>/dev/null) || {
    echo "OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_INVALID name=$env_name" >&2
    return 1
  }
  if [[ "$actual_branch" != "$expected_branch" ||
    "$actual_origin" != "$expected_origin" ||
    "$actual_baseline" != "$expected_baseline" ||
    "$actual_head" != "$approved_commit" ]] ||
    ! /usr/bin/git -C "$actual_root" merge-base --is-ancestor \
      "$expected_baseline" "$actual_head" ||
    [[ -n "$(/usr/bin/git -C "$actual_root" status --porcelain --untracked-files=all)" ]]; then
    echo "OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_INVALID name=$env_name" >&2
    return 1
  fi

  source_root_identity=$(/usr/bin/stat -Lc '%d:%i' -- "$actual_root") || {
    echo "OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_INVALID name=$env_name" >&2
    return 1
  }
  [[ "$source_root_identity" =~ ^[0-9]+:[0-9]+$ ]] || {
    echo "OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_INVALID name=$env_name" >&2
    return 1
  }
  owner_uid=$(/usr/bin/id -u)
  owner_gid=$(/usr/bin/id -g)
  mapfile -t relative_paths < <(implementation_relative_files "$env_name")
  (("${#relative_paths[@]}" > 0)) || return 1

  snapshot_name="${destination##*/}"
  case "$snapshot_name" in
    page1-java | page2-python | page3-python) ;;
    *) echo "OBS_CONTRACT_IMPLEMENTATION_SNAPSHOT_NAME_INVALID name=$env_name" >&2; return 1 ;;
  esac
  snapshot_root="$implementation_snapshot_dir/$snapshot_name"
  /usr/bin/mkdir -m 700 -- "$snapshot_root" || {
    echo "OBS_CONTRACT_IMPLEMENTATION_SNAPSHOT_CREATE_FAILED name=$env_name" >&2
    return 1
  }
  snapshot_root_real=$(/usr/bin/realpath -e -- "$snapshot_root") || return 1
  [[ "$snapshot_root_real" == "$snapshot_root" && ! -L "$snapshot_root" ]] || {
    echo "OBS_CONTRACT_IMPLEMENTATION_SNAPSHOT_INVALID name=$env_name" >&2
    return 1
  }
  snapshot_root_identity=$(/usr/bin/stat -Lc '%d:%i' -- "$snapshot_root") || return 1
  snapshot_metadata=$(/usr/bin/stat -Lc '%u:%g:%a:%F' -- "$snapshot_root") || return 1
  [[ "$snapshot_root_identity" =~ ^[0-9]+:[0-9]+$ &&
    "$snapshot_metadata" == "$owner_uid:$owner_gid:700:directory" ]] || return 1

  {
    printf '%s\n' "OBS70_IMPLEMENTATION_MANIFEST_V1"
    printf 'env\t%s\n' "$env_name"
    printf 'mount\t%s\n' "$destination"
    printf 'branch\t%s\n' "$actual_branch"
    printf 'commit\t%s\n' "$approved_commit"
    printf 'head\t%s\n' "$actual_head"
    printf 'root_identity\t%s\n' "$snapshot_root_identity"
    for relative_path in "${relative_paths[@]}"; do
      absolute_file="$actual_root/$relative_path"
      resolved_file=$(/usr/bin/realpath -e -- "$absolute_file" 2>/dev/null) || exit 81
      [[ "$resolved_file" == "$absolute_file" ]] || exit 81
      file_metadata=$(/usr/bin/stat -Lc '%u:%g:%a:%h:%F' -- "$absolute_file") || exit 81
      [[ "$file_metadata" =~ ^[0-9]+:[0-9]+:[0-7]+:1:regular\ file$ ]] || exit 81
      approved_blob=$(/usr/bin/git -C "$actual_root" rev-parse \
        --verify "$approved_commit:$relative_path" 2>/dev/null) || exit 81
      head_blob=$(/usr/bin/git -C "$actual_root" rev-parse \
        --verify "$actual_head:$relative_path" 2>/dev/null) || exit 81
      [[ "$approved_blob" =~ ^[0-9a-f]{40}$ &&
        "$head_blob" == "$approved_blob" &&
        "$(/usr/bin/git -C "$actual_root" cat-file -t "$approved_blob")" == blob ]] || exit 81
      approved_sha256=$(/usr/bin/git -C "$actual_root" cat-file blob "$approved_blob" |
        /usr/bin/sha256sum) || exit 81
      approved_sha256="${approved_sha256%% *}"
      working_sha256=$(/usr/bin/sha256sum "$absolute_file") || exit 81
      working_sha256="${working_sha256%% *}"
      [[ "$working_sha256" == "$approved_sha256" ]] || exit 81

      snapshot_file="$snapshot_root/$relative_path"
      snapshot_parent="${snapshot_file%/*}"
      /usr/bin/mkdir -p -- "$snapshot_parent" || exit 81
      /usr/bin/chmod 700 -- "$snapshot_parent" || exit 81
      [[ ! -e "$snapshot_file" && ! -L "$snapshot_file" ]] || exit 81
      /usr/bin/git -C "$actual_root" cat-file blob "$approved_blob" >"$snapshot_file" || exit 81
      /usr/bin/chmod 600 -- "$snapshot_file" || exit 81
      snapshot_metadata=$(/usr/bin/stat -Lc '%u:%g:%a:%h:%F' -- "$snapshot_file") || exit 81
      [[ "$snapshot_metadata" == "$owner_uid:$owner_gid:600:1:regular file" ]] || exit 81
      snapshot_sha256=$(/usr/bin/sha256sum "$snapshot_file") || exit 81
      snapshot_sha256="${snapshot_sha256%% *}"
      [[ "$snapshot_sha256" == "$approved_sha256" ]] || exit 81

      approved_blobs+=("$approved_blob")
      approved_sha256s+=("$approved_sha256")
      snapshot_files+=("$snapshot_file")
      printf 'file\t%s\t%s\n' "$relative_path" "$approved_sha256"
    done
  } >"$manifest_file" || {
    echo "OBS_CONTRACT_IMPLEMENTATION_BLOB_INVALID name=$env_name" >&2
    return 1
  }
  /usr/bin/chmod 600 -- "$manifest_file"
  manifest_sha256=$(/usr/bin/sha256sum "$manifest_file") || return 1
  manifest_sha256="${manifest_sha256%% *}"

  current_branch=$(/usr/bin/git -C "$actual_root" branch --show-current) || return 1
  current_origin=$(/usr/bin/git -C "$actual_root" config --get remote.origin.url) || return 1
  current_baseline=$(/usr/bin/git -C "$actual_root" rev-parse \
    --verify "refs/heads/obs/00-baseline^{commit}" 2>/dev/null) || return 1
  current_head=$(/usr/bin/git -C "$actual_root" rev-parse --verify "HEAD^{commit}") || return 1
  current_source_identity=$(/usr/bin/stat -Lc '%d:%i' -- "$actual_root") || return 1
  if [[ "$current_branch" != "$actual_branch" ||
    "$current_origin" != "$actual_origin" ||
    "$current_baseline" != "$actual_baseline" ||
    "$current_head" != "$actual_head" ||
    -n "$(/usr/bin/git -C "$actual_root" status --porcelain --untracked-files=all)" ||
    "$current_source_identity" != "$source_root_identity" ||
    ! "$manifest_sha256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_CHANGED name=$env_name" >&2
    return 1
  fi

  snapshot_root_real=$(/usr/bin/realpath -e -- "$snapshot_root") || return 1
  current_snapshot_identity=$(/usr/bin/stat -Lc '%d:%i' -- "$snapshot_root") || return 1
  snapshot_metadata=$(/usr/bin/stat -Lc '%u:%g:%a:%F' -- "$snapshot_root") || return 1
  if [[ "$snapshot_root_real" != "$snapshot_root" || -L "$snapshot_root" ||
    "$current_snapshot_identity" != "$snapshot_root_identity" ||
    "$snapshot_metadata" != "$owner_uid:$owner_gid:700:directory" ]]; then
    echo "OBS_CONTRACT_IMPLEMENTATION_SNAPSHOT_CHANGED name=$env_name" >&2
    return 1
  fi

  for index in "${!snapshot_files[@]}"; do
    snapshot_file="${snapshot_files[$index]}"
    snapshot_metadata=$(/usr/bin/stat -Lc '%u:%g:%a:%h:%F' -- "$snapshot_file") || return 1
    snapshot_sha256=$(/usr/bin/sha256sum "$snapshot_file") || return 1
    snapshot_sha256="${snapshot_sha256%% *}"
    if [[ "$snapshot_metadata" != "$owner_uid:$owner_gid:600:1:regular file" ||
      "$snapshot_sha256" != "${approved_sha256s[$index]}" ||
      "$(/usr/bin/git -C "$actual_root" cat-file -t "${approved_blobs[$index]}")" != blob ]]; then
      echo "OBS_CONTRACT_IMPLEMENTATION_SNAPSHOT_CHANGED name=$env_name" >&2
      return 1
    fi
  done

  validated_implementation_head="$actual_head"
  validated_implementation_identity="$snapshot_root_identity"
  validated_implementation_manifest_sha256="$manifest_sha256"
  validated_implementation_commit="$approved_commit"
  validated_implementation_snapshot_root="$snapshot_root"
  echo "OBS_CONTRACT_IMPLEMENTATION_PROVENANCE_OK name=$env_name branch=$actual_branch head=$actual_head baseline=$actual_baseline source_root_identity=$source_root_identity snapshot_root_identity=$snapshot_root_identity manifest_sha256=$manifest_sha256 files=${#relative_paths[@]} snapshot=git-blob-private"
}

mkdir -p "$work_dir/home" "$work_dir/pip-cache" "$work_dir/pytest"
docker_args=()
case "${OBS_REQUIRE_IMPLEMENTATION_DDL:-0}" in
  0) ;;
  1)
    implementation_names=(
      OBS_PAGE1_JAVA_WORKTREE
      OBS_PAGE2_PYTHON_WORKTREE
      OBS_PAGE3_PYTHON_WORKTREE
    )
    implementation_destinations=(
      /implementation/page1-java
      /implementation/page2-python
      /implementation/page3-python
    )
    implementation_manifest_destinations=(
      /implementation-manifests/page1-java.tsv
      /implementation-manifests/page2-python.tsv
      /implementation-manifests/page3-python.tsv
    )
    for index in "${!implementation_names[@]}"; do
      env_name="${implementation_names[$index]}"
      source_root="${!env_name:-}"
      destination="${implementation_destinations[$index]}"
      manifest_destination="${implementation_manifest_destinations[$index]}"
      manifest_file="$implementation_manifest_dir/${manifest_destination##*/}"
      if [[ -z "$source_root" ]] ||
        ! validate_implementation_root \
          "$env_name" "$source_root" "$destination" "$manifest_file"; then
        exit 65
      fi
      commit_env_name="${env_name}_COMMIT"
      identity_env_name="${env_name}_ROOT_IDENTITY"
      manifest_env_name="${env_name}_MANIFEST"
      manifest_sha_env_name="${env_name}_MANIFEST_SHA256"
      printf -v "$commit_env_name" '%s' "$validated_implementation_commit"
      printf -v "$identity_env_name" '%s' "$validated_implementation_identity"
      printf -v "$manifest_env_name" '%s' "$manifest_destination"
      printf -v "$manifest_sha_env_name" '%s' \
        "$validated_implementation_manifest_sha256"
      export "$commit_env_name" "$identity_env_name" \
        "$manifest_env_name" "$manifest_sha_env_name"
      docker_args+=(
        --mount "type=bind,src=$validated_implementation_snapshot_root,dst=$destination,readonly"
        --mount "type=bind,src=$manifest_file,dst=$manifest_destination,readonly"
        --env "$env_name=$destination"
        --env "$commit_env_name=$validated_implementation_commit"
        --env "$identity_env_name=$validated_implementation_identity"
        --env "$manifest_env_name=$manifest_destination"
        --env "$manifest_sha_env_name=$validated_implementation_manifest_sha256"
      )
    done
    docker_args+=(--env OBS_REQUIRE_IMPLEMENTATION_DDL=1)
    ;;
  *)
    echo "OBS_CONTRACT_IMPLEMENTATION_MODE_INVALID" >&2
    exit 65
    ;;
esac

if [[ -n "${OBS_CONTRACT_PYTHON_BIN:-}" ]]; then
  cd "$repo_root"
  "$OBS_CONTRACT_PYTHON_BIN" -m pytest \
    --confcutdir=tests/observability_contracts \
    -p no:cacheprovider \
    --strict-markers \
    -q tests/observability_contracts "$@"
  exit
fi

verify_docker_cli_binding || exit 66
verify_docker_context_binding || exit 66
docker_cmd=("$DOCKER_CLI" --context "$EXPECTED_DOCKER_CONTEXT")
run_id=$(/usr/bin/od -An -N8 -tx1 /dev/urandom | /usr/bin/tr -d ' \n')
if [[ ! "$run_id" =~ ^[0-9a-f]{16}$ ]]; then
  echo "OBS_CONTRACT_RUN_ID_INVALID" >&2
  exit 67
fi
container_name="contract-review-code-dev-obs70-pytest-$run_id"

if ! create_owned_container "$container_name" \
  --user "$(/usr/bin/id -u):$(/usr/bin/id -g)" \
  --mount "type=bind,src=$repo_root,dst=/workspace,readonly" \
  --mount "type=bind,src=$work_dir,dst=/work" \
  --workdir /workspace \
  --env HOME=/work/home \
  --env TMPDIR=/work/pytest \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --env PYTHONPYCACHEPREFIX=/work/pycache \
  --env PIP_CACHE_DIR=/work/pip-cache \
  --env PIP_DISABLE_PIP_VERSION_CHECK=1 \
  "${docker_args[@]}" \
  python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 \
  sh -ec 'python -m pip install --no-input --no-deps --target /work/site "pytest==8.4.1" "PyYAML==6.0.2" "iniconfig==2.1.0" "packaging==25.0" "pluggy==1.6.0" "Pygments==2.19.2" "ruff==0.12.7" >/dev/null && PYTHONPATH=/work/site python -m ruff check --no-cache tests/observability_contracts && PYTHONPATH=/work/site python -m ruff format --check --no-cache tests/observability_contracts && PYTHONPATH=/work/site python -m pytest --confcutdir=tests/observability_contracts -p no:cacheprovider --strict-markers -q tests/observability_contracts "$@"' \
  obs70-contract-tests "$@"; then
  exit 68
fi
container_id="$last_container_id"
if ! trusted_docker_ready ||
  ! verify_owned_container "$container_id" "$container_name"; then
  echo "OBS_CONTRACT_CONTAINER_START_BOUNDARY_INVALID" >&2
  exit 68
fi

set +e
"${docker_cmd[@]}" start --attach "$container_id"
test_status=$?
set -e
if ((test_status != 0)); then
  exit "$test_status"
fi

for safety_script in \
  test-frozen-contract-source-safety.sh \
  test-runner-resource-safety.sh \
  test-e2e-guard-safety.sh \
  test-e2e-probe-safety.sh \
  test-e2e-secret-safety.sh \
  test-capability-leak-scanner.sh; do
  "$repo_root/scripts/observability-contracts/$safety_script"
done
echo "OBS_CONTRACT_HOST_SAFETY_MATRIX_OK cases=6 real_e2e=not-run"
echo "OBS_CONTRACT_SUITE_OK capability_manifest=validated host_safety=validated"
