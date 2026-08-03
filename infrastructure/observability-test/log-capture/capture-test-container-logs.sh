#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=../docker-trust.sh
source "${SCRIPT_DIR}/../docker-trust.sh"
readonly PRIMARY_PROJECT="contract-review-code-dev"
readonly PLATFORM_PROJECT="contract-review-code-dev-observability"
readonly OBSERVABILITY_SCOPE="contract-review-code-dev"
readonly ENVIRONMENT="test"
readonly CAPTURE_LABEL="enabled"
readonly CAPTURE_MODE="${CONTRACT_REVIEW_DEV_CAPTURE_MODE:-PROJECT_DISCOVERY}"
# Additional test-only networks must be an exact comma-separated allowlist. Shared model
# networks never satisfy the test-network requirement by themselves.
readonly EXTRA_TEST_NETWORKS="${CONTRACT_REVIEW_DEV_CAPTURE_TEST_NETWORKS:-ollama-isolated-net}"
readonly SELECTOR_MANIFEST="${CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE:-}"
readonly PRIVATE_ROOT="/home/aituge/contract-review-code-dev-test-private"
readonly DEPLOYED_SELECTOR_MANIFEST="${PRIVATE_ROOT}/observability-capture.selectors"
readonly ALLOWED_LOG_PARENT="/home/aituge/contract-review-code-dev-test-private/observability-logs"
readonly EXACT_LOG_ROOT="${ALLOWED_LOG_PARENT}/redacted"
readonly PYTHON_BIN="/usr/bin/python3"
readonly SETSID_BIN="/usr/bin/setsid"
readonly FLOCK_BIN="/usr/bin/flock"
readonly RECONCILE_SECONDS=5
readonly LOOKBACK="10s"
readonly SCRIPT_PATH="${SCRIPT_DIR}/$(basename -- "${BASH_SOURCE[0]}")"
readonly REDACTOR="${SCRIPT_DIR}/redact_stream.py"
readonly SECURE_LAUNCHER="${SCRIPT_DIR}/secure_capture_launcher.py"

declare -A CAPTURE_PIDS=()
declare -A CAPTURE_SEEN=()
declare -A CAPTURE_SERVICES=()
declare -A CAPTURE_STREAMS=()
declare -A SELECTOR_NAME=()
declare -A SELECTOR_PROJECT=()
declare -A SELECTOR_SERVICE=()
declare -A SELECTOR_SCOPE=()
declare -A SELECTOR_STREAM=()
declare -A SELECTOR_IMAGE_ID=()
declare -A SELECTOR_POLICY=()
declare -a SELECTOR_CONTAINER_IDS=()
declare -A SELECTOR_MISSING_REPORTED=()
declare -A SELECTOR_DEGRADED_REPORTED=()
SELECTED_SERVICE=""
SELECTED_STREAM=""
SELECTED_DEGRADED=0
SELECTORS_LOADED=0
LAST_RETENTION_CLEANUP=0
MANAGER_LOCK_FD=""
CAPTURE_ROOT_FD="${PAGE6_CAPTURE_ROOT_FD:-}"
readonly CAPTURE_ROOT_DEVICE="${PAGE6_CAPTURE_ROOT_DEVICE:-}"
readonly CAPTURE_ROOT_INODE="${PAGE6_CAPTURE_ROOT_INODE:-}"
SHUTDOWN_DONE=0

stable_error() {
    printf '%s\n' "$1" >&2
}

is_hex_id() {
    [[ "$1" =~ ^[0-9a-f]{12,64}$ ]]
}

is_full_hex_id() {
    [[ "$1" =~ ^[0-9a-f]{64}$ ]]
}

is_image_id() {
    [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]
}

is_service_label() {
    [[ "$1" =~ ^[A-Za-z0-9_.-]{1,128}$ ]]
}

is_approved_capture_name() {
    [[ "$1" == "frontnew-test" ]] || page6_assert_safe_test_name "$1"
}

validate_capture_mode() {
    [[ "$CAPTURE_MODE" == "PROJECT_DISCOVERY" || "$CAPTURE_MODE" == "STRICT_SELECTORS" ]] || {
        stable_error "CAPTURE_MODE_INVALID"
        return 1
    }
}

validate_extra_test_networks() {
    local network seen="|" count=0
    local -a extra_networks=()
    [[ -n "$EXTRA_TEST_NETWORKS" && "$EXTRA_TEST_NETWORKS" != ,* && "$EXTRA_TEST_NETWORKS" != *, && "$EXTRA_TEST_NETWORKS" != *,,* ]] || { stable_error "CAPTURE_EXTRA_TEST_NETWORKS_INVALID"; return 1; }
    IFS=',' read -r -a extra_networks <<< "$EXTRA_TEST_NETWORKS"
    for network in "${extra_networks[@]}"; do
        [[ "$network" =~ ^[A-Za-z0-9_.-]{1,128}$ ]] || { stable_error "CAPTURE_EXTRA_TEST_NETWORKS_INVALID"; return 1; }
        [[ "$network" != "ai-model-runtime-net" ]] || { stable_error "CAPTURE_EXTRA_TEST_NETWORKS_SHARED_REJECTED"; return 1; }
        ! is_formal_network_name "$network" || { stable_error "CAPTURE_EXTRA_TEST_NETWORKS_FORMAL_REJECTED"; return 1; }
        [[ "$seen" != *"|$network|"* ]] || { stable_error "CAPTURE_EXTRA_TEST_NETWORKS_DUPLICATE"; return 1; }
        seen+="$network|"; count=$((count + 1)); (( count <= 32 )) || { stable_error "CAPTURE_EXTRA_TEST_NETWORKS_INVALID"; return 1; }
    done
}

metadata_field_safe() {
    [[ "$1" != *'|'* && "$1" != *$'\r'* && "$1" != *$'\n'* ]]
}

metadata_fields_safe() {
    local value
    for value in "$@"; do metadata_field_safe "$value" || return 1; done
}

is_running_state() {
    [[ "$1" == "true" ]]
}

is_test_network_name() {
    local network="$1" approved
    local -a approved_networks=()
    [[ "$network" =~ ^contract-review-code-dev-[A-Za-z0-9_.-]{1,128}$ ]] && return 0
    IFS=',' read -r -a approved_networks <<< "$EXTRA_TEST_NETWORKS"
    for approved in "${approved_networks[@]}"; do
        [[ -n "$approved" && "$approved" =~ ^[A-Za-z0-9_.-]{1,128}$ ]] || return 1
        [[ "$network" == "$approved" ]] && return 0
    done
    return 1
}

is_discovery_name() {
    local name="$1"
    [[ "$name" =~ ^contract-review-[A-Za-z0-9][A-Za-z0-9_.-]{0,128}$ ]] && ! page6_forbidden_word "$name"
}

is_formal_network_name() {
    local network="${1,,}"
    case "$network" in
        agent-internal|ai-framework-internal|proofspace-network|continew-agent_default) return 0 ;;
    esac
    page6_forbidden_word "$network"
}

networks_allowed() {
    local encoded="$1" network
    local found_test=0
    local -a networks=()
    [[ -n "$encoded" ]] || return 1
    IFS=',' read -r -a networks <<< "$encoded"
    for network in "${networks[@]}"; do
        [[ -n "$network" ]] || return 1
        is_formal_network_name "$network" && return 1
        is_test_network_name "$network" && found_test=1
    done
    (( found_test ))
}

validate_selector_manifest_path() {
    local path="$SELECTOR_MANIFEST"
    local component
    [[ -n "$path" && -f "$path" && ! -L "$path" ]] || {
        stable_error "CAPTURE_SELECTOR_MANIFEST_REQUIRED"
        return 1
    }
    case "$path" in
        "$DEPLOYED_SELECTOR_MANIFEST") ;;
        "$ALLOWED_LOG_PARENT"/page6-probe.??????/capture-selectors) ;;
        *) stable_error "CAPTURE_SELECTOR_MANIFEST_OUTSIDE_TEST"; return 1 ;;
    esac
    for component in /home /home/aituge "$PRIVATE_ROOT"; do
        [[ -d "$component" && ! -L "$component" ]] || {
            stable_error "CAPTURE_SELECTOR_PARENT_UNSAFE"; return 1;
        }
    done
    [[ "$(stat -Lc '%F|%a|%u|%g' -- /home)" == "directory|755|0|0" ]] &&
        [[ "$(stat -Lc '%F|%a|%u|%g' -- /home/aituge)" == "directory|750|$(id -u)|$(id -g)" ]] &&
        [[ "$(stat -Lc '%F|%a|%u|%g' -- "$PRIVATE_ROOT")" == "directory|700|$(id -u)|$(id -g)" ]] || {
        stable_error "CAPTURE_SELECTOR_PARENT_PERMISSIONS"; return 1;
    }
    if [[ "$path" != "$DEPLOYED_SELECTOR_MANIFEST" ]]; then
        for component in "$ALLOWED_LOG_PARENT" "$(dirname -- "$path")"; do
            [[ -d "$component" && ! -L "$component" ]] &&
                [[ "$(stat -Lc '%F|%a|%u|%g' -- "$component")" == "directory|700|$(id -u)|$(id -g)" ]] || {
                stable_error "CAPTURE_SELECTOR_PARENT_UNSAFE"; return 1;
            }
        done
    fi
    [[ "$(stat -Lc '%F|%a|%u|%g|%h' -- "$path")" == \
        "regular file|600|$(id -u)|$(id -g)|1" ]] || {
        stable_error "CAPTURE_SELECTOR_MANIFEST_PERMISSIONS"
        return 1
    }
}


register_selector() {
    local container_id="$1"
    local container_name="$2"
    local project="$3"
    local service="$4"
    local scope="$5"
    local stream="$6"
    local image_id="$7"
    local policy="$8"
    is_full_hex_id "$container_id" &&
        is_approved_capture_name "$container_name" &&
        is_service_label "$service" &&
        [[ "$scope" == "$OBSERVABILITY_SCOPE" ]] &&
        [[ "$stream" == "APPLICATION" || "$stream" == "PLATFORM" ]] &&
        is_image_id "$image_id" &&
        [[ "$policy" == "STRICT" || "$policy" == "LEGACY_TEST_PINNED" ]] || {
            stable_error "CAPTURE_SELECTOR_MANIFEST_INVALID"
            return 1
        }
    [[ -z "${SELECTOR_NAME[$container_id]:-}" ]] || {
        stable_error "CAPTURE_SELECTOR_MANIFEST_DUPLICATE"
        return 1
    }
    case "$project|$stream|$policy" in
        "$PRIMARY_PROJECT|APPLICATION|STRICT")
            [[ "$container_name" == contract-review-code-dev-* ||
                "$container_name" == contract-review-dev-* ]] || return 1
            ;;
        "$PLATFORM_PROJECT|PLATFORM|STRICT")
            [[ "$container_name" == contract-review-code-dev-observability-* ]] || return 1
            ;;
        "STANDALONE|APPLICATION|LEGACY_TEST_PINNED")
            [[ "$container_name" == "frontnew-test" && "$service" == "frontnew-test" ]] || return 1
            ;;
        "$PRIMARY_PROJECT|APPLICATION|LEGACY_TEST_PINNED")
            [[ "$container_name" == "contract-review-dev-model-gateway-1" &&
                "$service" == "model-gateway" ]] || return 1
            ;;
        *) stable_error "CAPTURE_SELECTOR_PROJECT_REJECTED"; return 1 ;;
    esac
    SELECTOR_CONTAINER_IDS+=("$container_id")
    SELECTOR_NAME["$container_id"]="$container_name"
    SELECTOR_PROJECT["$container_id"]="$project"
    SELECTOR_SERVICE["$container_id"]="$service"
    SELECTOR_SCOPE["$container_id"]="$scope"
    SELECTOR_STREAM["$container_id"]="$stream"
    SELECTOR_IMAGE_ID["$container_id"]="$image_id"
    SELECTOR_POLICY["$container_id"]="$policy"
}

load_selectors() {
    local container_id container_name project service scope stream image_id policy extra
    local count=0
    validate_selector_manifest_path || return 1
    SELECTOR_CONTAINER_IDS=()
    SELECTOR_NAME=(); SELECTOR_PROJECT=(); SELECTOR_SERVICE=(); SELECTOR_SCOPE=()
    SELECTOR_STREAM=(); SELECTOR_IMAGE_ID=(); SELECTOR_POLICY=()
    while IFS='|' read -r container_id container_name project service scope stream image_id policy extra; do
        [[ -z "$container_id" || "$container_id" == \#* ]] && continue
        [[ -z "$extra" ]] || { stable_error "CAPTURE_SELECTOR_MANIFEST_INVALID"; return 1; }
        count=$((count + 1))
        (( count <= 64 )) || { stable_error "CAPTURE_SELECTOR_MANIFEST_INVALID"; return 1; }
        register_selector "$container_id" "$container_name" "$project" "$service" \
            "$scope" "$stream" "$image_id" "$policy" || return 1
    done < "$SELECTOR_MANIFEST"
    (( count > 0 )) || { stable_error "CAPTURE_SELECTOR_MANIFEST_EMPTY"; return 1; }
    SELECTORS_LOADED=1
}

candidate_allowed() {
    local container_id="$1" container_name="$2" project_label="$3" environment_label="$4"
    local scope_label="$5" capture_label="$6" service_label="$7" image_id="$8"
    local running_state="${9:-}" networks="${10:-}"
    local expected_project="${SELECTOR_PROJECT[$container_id]:-}"
    local expected_policy="${SELECTOR_POLICY[$container_id]:-}"
    SELECTED_SERVICE=""; SELECTED_STREAM=""; SELECTED_DEGRADED=0
    is_full_hex_id "$container_id" && is_running_state "$running_state" &&
        networks_allowed "$networks" &&
        [[ "$container_name" == "${SELECTOR_NAME[$container_id]:-}" ]] &&
        is_approved_capture_name "$container_name" &&
        [[ "$image_id" == "${SELECTOR_IMAGE_ID[$container_id]:-}" ]] || return 1
    if [[ "$expected_policy" == "STRICT" ]]; then
        [[ "$project_label" == "$expected_project" &&
            "$environment_label" == "$ENVIRONMENT" &&
            "$scope_label" == "${SELECTOR_SCOPE[$container_id]}" &&
            "$capture_label" == "$CAPTURE_LABEL" &&
            "$service_label" == "${SELECTOR_SERVICE[$container_id]}" ]] || return 1
    elif [[ "$expected_policy" == "LEGACY_TEST_PINNED" ]]; then
        [[ -z "$environment_label" && -z "$scope_label" && -z "$capture_label" ]] || return 1
        if [[ "$expected_project" == "STANDALONE" ]]; then
            [[ -z "$project_label" && -z "$service_label" ]] || return 1
        else
            [[ "$project_label" == "$expected_project" && "$service_label" == "${SELECTOR_SERVICE[$container_id]}" ]] || return 1
        fi
        SELECTED_DEGRADED=1
    else
        return 1
    fi
    SELECTED_SERVICE="${SELECTOR_SERVICE[$container_id]}"; SELECTED_STREAM="${SELECTOR_STREAM[$container_id]}"
}

discovery_candidate_allowed() {
    local container_id="$1" container_name="$2" project_label="$3" service_label="$4"
    local image_id="$5" running_state="$6" networks="$7"
    SELECTED_SERVICE=""; SELECTED_STREAM=""; SELECTED_DEGRADED=0
    is_full_hex_id "$container_id" && is_image_id "$image_id" && is_running_state "$running_state" &&
        is_discovery_name "$container_name" && is_service_label "$service_label" &&
        networks_allowed "$networks" || return 1
    case "$project_label" in
        "$PRIMARY_PROJECT")
            SELECTED_STREAM=APPLICATION ;;
        "$PLATFORM_PROJECT")
            [[ "$container_name" == contract-review-code-dev-observability-* ]] || return 1
            SELECTED_STREAM=PLATFORM ;;
        *) return 1 ;;
    esac
    SELECTED_SERVICE="$service_label"
}

secure_reexec_if_needed() {
    [[ "${1:-}" == "--self-test" ]] && return 0
    if [[ "${PAGE6_CAPTURE_SECURE_LAUNCHED:-}" != "1" ]]; then
        [[ -n "${CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT:-}" ]] || {
            stable_error "CAPTURE_LOG_ROOT_REQUIRED"
            return 1
        }
        [[ -f "$SECURE_LAUNCHER" && ! -L "$SECURE_LAUNCHER" ]] || {
            stable_error "CAPTURE_SECURE_LAUNCHER_INVALID"
            return 1
        }
        exec "$PYTHON_BIN" -E -s -B "$SECURE_LAUNCHER" \
            --output-dir "$CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT" \
            -- "$@"
        stable_error "CAPTURE_SECURE_REEXEC_FAILED"
        return 1
    fi
}

validate_log_root() {
    local raw_root="${CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT:-}"
    local opened_root
    [[ -n "$raw_root" ]] || {
        stable_error "CAPTURE_LOG_ROOT_REQUIRED"
        return 1
    }
    [[ "${PAGE6_CAPTURE_SECURE_LAUNCHED:-}" == "1" &&
        "$CAPTURE_ROOT_FD" =~ ^[0-9]+$ &&
        "$CAPTURE_ROOT_DEVICE" =~ ^[0-9]+$ &&
        "$CAPTURE_ROOT_INODE" =~ ^[0-9]+$ &&
        -e "/proc/self/fd/$CAPTURE_ROOT_FD" ]] || {
        stable_error "CAPTURE_SECURE_ROOT_FD_REQUIRED"
        return 1
    }
    opened_root="$(readlink -- "/proc/self/fd/$CAPTURE_ROOT_FD")" || {
        stable_error "CAPTURE_LOG_ROOT_INVALID"
        return 1
    }
    [[ "$opened_root" == "$raw_root" ]] || {
        stable_error "CAPTURE_LOG_ROOT_CHANGED"
        return 1
    }
    [[ "$(stat -Lc '%F|%a|%u|%g|%d|%i' -- "/proc/self/fd/$CAPTURE_ROOT_FD")" == \
        "directory|700|$(id -u)|$(id -g)|$CAPTURE_ROOT_DEVICE|$CAPTURE_ROOT_INODE" ]] || {
        stable_error "CAPTURE_LOG_ROOT_PERMISSIONS"
        return 1
    }
    case "$raw_root" in
        "$EXACT_LOG_ROOT") ;;
        "$ALLOWED_LOG_PARENT"/page6-probe.??????/*) ;;
        *)
            stable_error "CAPTURE_LOG_ROOT_OUTSIDE_TEST"
            return 1
            ;;
    esac
    [[ -f "$REDACTOR" && ! -L "$REDACTOR" ]] || {
        stable_error "CAPTURE_REDACTOR_INVALID"
        return 1
    }
    [[ -f "$SECURE_LAUNCHER" && ! -L "$SECURE_LAUNCHER" ]] || {
        stable_error "CAPTURE_SECURE_LAUNCHER_INVALID"
        return 1
    }
    [[ -x "$PAGE6_DOCKER_BIN" && -x "$PYTHON_BIN" && -x "$SETSID_BIN" && -x "$FLOCK_BIN" ]] || {
        stable_error "CAPTURE_DEPENDENCY_MISSING"
        return 1
    }
    validate_capture_mode || return 1
    validate_extra_test_networks || return 1
    SELECTORS_LOADED=0
    if [[ "$CAPTURE_MODE" == "STRICT_SELECTORS" || -n "$SELECTOR_MANIFEST" ]]; then
        load_selectors || return 1
    fi
    page6_verify_docker_daemon ||
        { stable_error "CAPTURE_DOCKER_TRUST_REJECTED"; return 1; }
    return 0
}

self_test() {
    local valid_id platform_id frontend_id gateway_id image_id
    valid_id="$(printf 'a%.0s' {1..64})"
    platform_id="$(printf 'b%.0s' {1..64})"
    frontend_id="$(printf 'c%.0s' {1..64})"
    gateway_id="$(printf 'd%.0s' {1..64})"
    image_id="sha256:$(printf 'e%.0s' {1..64})"
    SELECTOR_CONTAINER_IDS=()
    SELECTOR_NAME=(); SELECTOR_PROJECT=(); SELECTOR_SERVICE=(); SELECTOR_SCOPE=()
    SELECTOR_STREAM=(); SELECTOR_IMAGE_ID=(); SELECTOR_POLICY=()
    register_selector "$valid_id" "contract-review-code-dev-java-1" \
        "$PRIMARY_PROJECT" "java" "$OBSERVABILITY_SCOPE" APPLICATION "$image_id" STRICT
    register_selector "$platform_id" "contract-review-code-dev-observability-loki-1" \
        "$PLATFORM_PROJECT" "loki" "$OBSERVABILITY_SCOPE" PLATFORM "$image_id" STRICT
    register_selector "$frontend_id" "frontnew-test" STANDALONE "frontnew-test" \
        "$OBSERVABILITY_SCOPE" APPLICATION "$image_id" LEGACY_TEST_PINNED
    register_selector "$gateway_id" "contract-review-dev-model-gateway-1" \
        "$PRIMARY_PROJECT" "model-gateway" "$OBSERVABILITY_SCOPE" APPLICATION \
        "$image_id" LEGACY_TEST_PINNED

    candidate_allowed "$valid_id" "contract-review-code-dev-java-1" "$PRIMARY_PROJECT" \
        "$ENVIRONMENT" "$OBSERVABILITY_SCOPE" "$CAPTURE_LABEL" java "$image_id" true "contract-review-code-dev-app," || {
        stable_error "CAPTURE_SELF_TEST_APPLICATION_SELECTOR_REJECTED"; return 1;
    }
    [[ "$SELECTED_STREAM" == APPLICATION && "$SELECTED_SERVICE" == java &&
        "$SELECTED_DEGRADED" == 0 ]] || return 1
    if candidate_allowed "$valid_id" "contract-review-code-dev-java-1" "$PRIMARY_PROJECT" "$ENVIRONMENT" "$OBSERVABILITY_SCOPE" "$CAPTURE_LABEL" java "$image_id" false "contract-review-code-dev-app,"; then
        stable_error "CAPTURE_SELF_TEST_STRICT_STOPPED_SELECTED"; return 1
    fi
    if candidate_allowed "$valid_id" "contract-review-code-dev-java-1" "$PRIMARY_PROJECT" "$ENVIRONMENT" "$OBSERVABILITY_SCOPE" "$CAPTURE_LABEL" java "$image_id" true "contract-review-code-dev-app,agent-internal,"; then
        stable_error "CAPTURE_SELF_TEST_STRICT_FORMAL_NETWORK_SELECTED"; return 1
    fi
    candidate_allowed "$platform_id" "contract-review-code-dev-observability-loki-1" \
        "$PLATFORM_PROJECT" "$ENVIRONMENT" "$OBSERVABILITY_SCOPE" \
        "$CAPTURE_LABEL" loki "$image_id" true "contract-review-code-dev-observability-backend," || {
        stable_error "CAPTURE_SELF_TEST_PLATFORM_SELECTOR_REJECTED"; return 1;
    }
    [[ "$SELECTED_STREAM" == PLATFORM && "$SELECTED_SERVICE" == loki &&
        "$SELECTED_DEGRADED" == 0 ]] || return 1
    candidate_allowed "$frontend_id" frontnew-test "" "" "" "" "" "$image_id" true "contract-review-code-dev-agent-internal," || {
        stable_error "CAPTURE_SELF_TEST_FRONTEND_SELECTOR_REJECTED"; return 1;
    }
    [[ "$SELECTED_STREAM" == APPLICATION && "$SELECTED_DEGRADED" == 1 ]] || return 1
    candidate_allowed "$gateway_id" contract-review-dev-model-gateway-1 \
        "$PRIMARY_PROJECT" "" "" "" model-gateway "$image_id" true "contract-review-code-dev-app," || {
        stable_error "CAPTURE_SELF_TEST_GATEWAY_SELECTOR_REJECTED"; return 1;
    }
    [[ "$SELECTED_STREAM" == APPLICATION && "$SELECTED_DEGRADED" == 1 ]] || return 1
    if candidate_allowed "$valid_id" "contract-review-formal" "$PRIMARY_PROJECT" \
        "$ENVIRONMENT" "$OBSERVABILITY_SCOPE" "$CAPTURE_LABEL" java "$image_id" true "contract-review-code-dev-app,"; then
        stable_error "CAPTURE_SELF_TEST_FORMAL_NAME_SELECTED"; return 1
    fi
    if candidate_allowed "$valid_id" "contract-review-code-dev-java-1" "$PRIMARY_PROJECT" \
        "$ENVIRONMENT" "$OBSERVABILITY_SCOPE" "$CAPTURE_LABEL" java \
        "sha256:$(printf 'f%.0s' {1..64})" true "contract-review-code-dev-app,"; then
        stable_error "CAPTURE_SELF_TEST_IMAGE_DRIFT_SELECTED"; return 1
    fi
    discovery_candidate_allowed "$valid_id" "contract-review-code-dev-new-service-1" "$PRIMARY_PROJECT" "new-service" "$image_id" true "contract-review-code-dev-app,ai-model-runtime-net," || {
        stable_error "CAPTURE_SELF_TEST_DISCOVERY_NEW_SERVICE_REJECTED"; return 1;
    }
    [[ "$SELECTED_STREAM" == APPLICATION && "$SELECTED_SERVICE" == new-service ]] || return 1
    if discovery_candidate_allowed "$valid_id" "contract-review-code-dev-new-service-1" "$PRIMARY_PROJECT" "new-service" "$image_id" false "contract-review-code-dev-app,"; then
        stable_error "CAPTURE_SELF_TEST_DISCOVERY_STOPPED_SELECTED"; return 1
    fi
    if discovery_candidate_allowed "$valid_id" "contract-review-code-dev-new-service-1" "$PRIMARY_PROJECT" "new-service" "$image_id" true "ai-model-runtime-net,"; then
        stable_error "CAPTURE_SELF_TEST_DISCOVERY_NO_TEST_NETWORK_SELECTED"; return 1
    fi
    if discovery_candidate_allowed "$valid_id" "contract-review-code-dev-new-service-1" "$PRIMARY_PROJECT" "new-service" "$image_id" true "contract-review-code-dev-app,agent-internal,"; then
        stable_error "CAPTURE_SELF_TEST_DISCOVERY_FORMAL_NETWORK_SELECTED"; return 1
    fi
    discovery_candidate_allowed "$valid_id" "contract-review-resume-screening-dev-1" "$PRIMARY_PROJECT" "resume-screening" "$image_id" true "ollama-isolated-net," || {
        stable_error "CAPTURE_SELF_TEST_DISCOVERY_REGISTERED_NETWORK_REJECTED"; return 1;
    }
    if discovery_candidate_allowed "$valid_id" "contract-review-formal-1" "$PRIMARY_PROJECT" "new-service" "$image_id" true "contract-review-code-dev-app,"; then
        stable_error "CAPTURE_SELF_TEST_DISCOVERY_NAME_DRIFT_SELECTED"; return 1
    fi
    local process_group_pid
    "$SETSID_BIN" /usr/bin/bash -c 'sleep 30 & wait' &
    process_group_pid=$!
    sleep 0.1
    page6_capture_process_identity "$process_group_pid" || {
        stable_error "CAPTURE_SELF_TEST_PROCESS_IDENTITY_FAILED"
        return 1
    }
    terminate_process_group "$process_group_pid" || {
        stable_error "CAPTURE_SELF_TEST_PROCESS_GROUP_STOP_FAILED"
        return 1
    }
    if kill -0 "$process_group_pid" 2>/dev/null; then
        stable_error "CAPTURE_SELF_TEST_ORPHAN_PROCESS"
        return 1
    fi

    "$SETSID_BIN" /usr/bin/bash -c 'sleep 30 & wait' &
    process_group_pid=$!
    sleep 0.1
    page6_capture_process_identity "$process_group_pid"
    PAGE6_PROCESS_START[$process_group_pid]=$((PAGE6_PROCESS_START[$process_group_pid] + 1))
    if terminate_process_group "$process_group_pid"; then
        stable_error "CAPTURE_SELF_TEST_PID_REUSE_NOT_REJECTED"; return 1
    fi
    kill -0 "$process_group_pid" 2>/dev/null || {
        stable_error "CAPTURE_SELF_TEST_PID_REUSE_SIGNALLED"; return 1;
    }
    PAGE6_PROCESS_START[$process_group_pid]=$((PAGE6_PROCESS_START[$process_group_pid] - 1))
    terminate_process_group "$process_group_pid"

    "$SETSID_BIN" /usr/bin/bash -c 'sleep 30 & wait' &
    process_group_pid=$!
    sleep 0.1
    terminate_unregistered_child "$process_group_pid" || {
        stable_error "CAPTURE_SELF_TEST_UNREGISTERED_CHILD_STOP_FAILED"
        return 1
    }
    if kill -0 "$process_group_pid" 2>/dev/null; then
        stable_error "CAPTURE_SELF_TEST_UNREGISTERED_CHILD_ORPHANED"
        return 1
    fi

    SHUTDOWN_DONE=0
    shutdown || {
        stable_error "CAPTURE_SELF_TEST_SHUTDOWN_FAILED"
        return 1
    }
    shutdown || {
        stable_error "CAPTURE_SELF_TEST_SHUTDOWN_NOT_IDEMPOTENT"
        return 1
    }
    printf '%s\n' "CAPTURE_SELF_TEST_OK"
}

inspect_candidate() {
    local container_id="$1"
    local metadata
    is_full_hex_id "$container_id" || return 1
    page6_verify_docker_daemon || return 1
    metadata="$(
        page6_docker inspect --type container \
            --format '{{.Id}}|{{.Name}}|{{with index .Config.Labels "com.docker.compose.project"}}{{.}}{{end}}|{{with index .Config.Labels "com.aituge.environment"}}{{.}}{{end}}|{{with index .Config.Labels "com.aituge.observability.scope"}}{{.}}{{end}}|{{with index .Config.Labels "com.aituge.observability.capture"}}{{.}}{{end}}|{{with index .Config.Labels "com.docker.compose.service"}}{{.}}{{end}}|{{.Image}}|{{.State.Running}}|{{range $name, $_ := .NetworkSettings.Networks}}{{$name}},{{end}}' \
            "$container_id" 2>/dev/null
    )" || return 1
    printf '%s\n' "$metadata"
}

capture_one() {
    local container_id="$1" expected_service="$2" expected_stream="$3" expected_image="$4"
    local metadata inspected_id inspected_name project_label environment_label scope_label capture_label
    local inspected_service image_id running_state networks
    metadata="$(inspect_candidate "$container_id")" || { stable_error "CAPTURE_CANDIDATE_REJECTED"; return 1; }
    IFS='|' read -r inspected_id inspected_name project_label environment_label scope_label capture_label inspected_service image_id running_state networks <<< "$metadata"
    inspected_name="${inspected_name#/}"
    metadata_fields_safe "$inspected_id" "$inspected_name" "$project_label" "$environment_label" "$scope_label" "$capture_label" "$inspected_service" "$image_id" "$running_state" "$networks" || { stable_error "CAPTURE_DISCOVERY_METADATA_INVALID"; return 1; }
    [[ "$inspected_id" == "$container_id" && "$image_id" == "$expected_image" ]] || { stable_error "CAPTURE_CANDIDATE_REJECTED"; return 1; }
    if [[ -n "${SELECTOR_NAME[$inspected_id]:-}" ]]; then
        candidate_allowed "$inspected_id" "$inspected_name" "$project_label" "$environment_label" "$scope_label" "$capture_label" "$inspected_service" "$image_id" "$running_state" "$networks"
    else
        discovery_candidate_allowed "$inspected_id" "$inspected_name" "$project_label" "$inspected_service" "$image_id" "$running_state" "$networks"
    fi || { stable_error "CAPTURE_CANDIDATE_REJECTED"; return 1; }
    [[ "$SELECTED_SERVICE" == "$expected_service" && "$SELECTED_STREAM" == "$expected_stream" ]] || { stable_error "CAPTURE_SELECTOR_DRIFT_REJECTED"; return 1; }
    metadata="$(inspect_candidate "$container_id")" || { stable_error "CAPTURE_LABEL_RECHECK_FAILED"; return 1; }
    IFS='|' read -r inspected_id inspected_name project_label environment_label scope_label capture_label inspected_service image_id running_state networks <<< "$metadata"
    inspected_name="${inspected_name#/}"
    metadata_fields_safe "$inspected_id" "$inspected_name" "$project_label" "$environment_label" "$scope_label" "$capture_label" "$inspected_service" "$image_id" "$running_state" "$networks" || { stable_error "CAPTURE_DISCOVERY_METADATA_INVALID"; return 1; }
    [[ "$inspected_id" == "$container_id" && "$image_id" == "$expected_image" ]] || { stable_error "CAPTURE_DISCOVERY_FACT_RECHECK_REJECTED"; return 1; }
    if [[ -n "${SELECTOR_NAME[$inspected_id]:-}" ]]; then
        candidate_allowed "$inspected_id" "$inspected_name" "$project_label" "$environment_label" "$scope_label" "$capture_label" "$inspected_service" "$image_id" "$running_state" "$networks" || { stable_error "CAPTURE_DISCOVERY_FACT_RECHECK_REJECTED"; return 1; }
    else
        discovery_candidate_allowed "$inspected_id" "$inspected_name" "$project_label" "$inspected_service" "$image_id" "$running_state" "$networks" || { stable_error "CAPTURE_DISCOVERY_FACT_RECHECK_REJECTED"; return 1; }
    fi
    [[ "$SELECTED_SERVICE" == "$expected_service" && "$SELECTED_STREAM" == "$expected_stream" ]] || { stable_error "CAPTURE_DISCOVERY_FACT_RECHECK_REJECTED"; return 1; }
    page6_verify_docker_daemon || { stable_error "CAPTURE_DOCKER_TRUST_REJECTED"; return 1; }
    page6_docker logs --follow --timestamps --since "$LOOKBACK" "$container_id" 2>&1 | "$PYTHON_BIN" -E -s -B "$REDACTOR" --output-dir "$CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT" --root-fd "$CAPTURE_ROOT_FD" --root-device "$CAPTURE_ROOT_DEVICE" --root-inode "$CAPTURE_ROOT_INODE" --container-id "$container_id" --service "$expected_service" --stream "$expected_stream" || { stable_error "CAPTURE_FOLLOW_ENDED"; return 1; }
}


terminate_process_group() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    page6_terminate_process_group "$pid" || return 1
    wait "$pid" 2>/dev/null || true
}

terminate_unregistered_child() {
    local pid="$1"
    local parent=""
    local pgid=""
    local session=""
    local attempt
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    kill -0 "$pid" 2>/dev/null || {
        wait "$pid" 2>/dev/null || true
        return 0
    }
    parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')" || true
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]')" || true
    session="$(ps -o sid= -p "$pid" 2>/dev/null | tr -d '[:space:]')" || true
    if [[ "$parent" == "$$" && "$pgid" == "$pid" && "$session" == "$pid" ]]; then
        kill -TERM -- "-$pid" 2>/dev/null || true
    elif [[ "$parent" == "$$" ]]; then
        kill -TERM -- "$pid" 2>/dev/null || true
    else
        stable_error "CAPTURE_UNREGISTERED_CHILD_IDENTITY_REJECTED"
        return 1
    fi
    for attempt in {1..20}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.05
    done
    if kill -0 "$pid" 2>/dev/null; then
        if [[ "$pgid" == "$pid" && "$session" == "$pid" ]]; then
            kill -KILL -- "-$pid" 2>/dev/null || true
        else
            kill -KILL -- "$pid" 2>/dev/null || true
        fi
    fi
    wait "$pid" 2>/dev/null || true
    ! kill -0 "$pid" 2>/dev/null
}

start_capture() {
    local container_id="$1"
    local service="$2"
    local stream="$3"
    local image_id="$4"
    local pid
    local existing="${CAPTURE_PIDS[$container_id]:-}"
    if [[ "$existing" =~ ^[0-9]+$ ]]; then
        if page6_verify_process_identity "$existing"; then
            stable_error "CAPTURE_DUPLICATE_WORKER_REJECTED"
            return 1
        fi
        page6_forget_process "$existing"
        unset 'CAPTURE_PIDS[$container_id]'
    fi
    (
        if [[ "$MANAGER_LOCK_FD" =~ ^[0-9]+$ ]]; then
            eval "exec ${MANAGER_LOCK_FD}>&-"
            unset PAGE6_CAPTURE_LOCK_FD
        fi
        exec "$SETSID_BIN" /usr/bin/bash "$SCRIPT_PATH" --follow-worker \
            "$container_id" "$service" "$stream" "$image_id"
    ) &
    pid=$!
    sleep 0.05
    if ! page6_capture_process_identity "$pid"; then
        terminate_unregistered_child "$pid" ||
            stable_error "CAPTURE_UNREGISTERED_CHILD_CLEANUP_FAILED"
        stable_error "CAPTURE_WORKER_IDENTITY_REJECTED"
        return 1
    fi
    CAPTURE_PIDS["$container_id"]=$pid
    CAPTURE_SERVICES["$container_id"]="$service"
    CAPTURE_STREAMS["$container_id"]="$stream"
}
stop_capture() {
    local container_id="$1"
    local pid="${CAPTURE_PIDS[$container_id]:-}"
    terminate_process_group "$pid" || return 1
    unset 'CAPTURE_PIDS[$container_id]'
    unset 'CAPTURE_SERVICES[$container_id]'
    unset 'CAPTURE_STREAMS[$container_id]'
}


release_manager_lock() {
    [[ "$MANAGER_LOCK_FD" =~ ^[0-9]+$ ]] || return 0
    "$FLOCK_BIN" --unlock "$MANAGER_LOCK_FD" 2>/dev/null || true
    exec {MANAGER_LOCK_FD}>&-
    MANAGER_LOCK_FD=""
}

release_capture_root() {
    [[ "$CAPTURE_ROOT_FD" =~ ^[0-9]+$ ]] || return 0
    eval "exec ${CAPTURE_ROOT_FD}>&-"
    CAPTURE_ROOT_FD=""
}

acquire_manager_lock() {
    local inherited="${PAGE6_CAPTURE_LOCK_FD:-}"
    local opened_lock
    [[ "$inherited" =~ ^[0-9]+$ && -e "/proc/self/fd/$inherited" ]] || {
        stable_error "CAPTURE_MANAGER_LOCK_FD_REQUIRED"
        return 1
    }
    opened_lock="$(readlink -- "/proc/self/fd/$inherited")" || {
        stable_error "CAPTURE_MANAGER_LOCK_INVALID"
        return 1
    }
    [[ "$opened_lock" == \
        "$CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT/.capture-manager.lock" ]] || {
        stable_error "CAPTURE_MANAGER_LOCK_CHANGED"
        return 1
    }
    [[ -f "/proc/self/fd/$inherited" ]] || {
        stable_error "CAPTURE_MANAGER_LOCK_UNSAFE"
        return 1
    }
    [[ "$(stat -Lc '%a|%u|%g|%h' -- "/proc/self/fd/$inherited")" == \
        "600|$(id -u)|$(id -g)|1" ]] || {
        stable_error "CAPTURE_MANAGER_LOCK_UNSAFE"
        return 1
    }
    MANAGER_LOCK_FD="$inherited"
    if ! "$FLOCK_BIN" --nonblock "$MANAGER_LOCK_FD"; then
        stable_error "CAPTURE_MANAGER_ALREADY_RUNNING"
        return 73
    fi
}

shutdown() {
    local container_id
    local rc=0
    if (( SHUTDOWN_DONE )); then
        return 0
    fi
    SHUTDOWN_DONE=1
    for container_id in "${!CAPTURE_PIDS[@]}"; do
        stop_capture "$container_id" || rc=1
    done
    release_manager_lock
    release_capture_root
    return "$rc"
}

on_exit() {
    local status=$?
    trap - EXIT
    shutdown || true
    return "$status"
}

on_interrupt() {
    trap - INT
    shutdown || true
    exit 130
}

on_terminate() {
    trap - TERM
    shutdown || true
    exit 143
}
cleanup_redacted_files() {
    local now
    printf -v now '%(%s)T' -1
    if (( now - LAST_RETENTION_CLEANUP < 300 )); then
        return 0
    fi
    "$PYTHON_BIN" -E -s -B "$REDACTOR" \
        --output-dir "$CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT" \
        --root-fd "$CAPTURE_ROOT_FD" \
        --root-device "$CAPTURE_ROOT_DEVICE" \
        --root-inode "$CAPTURE_ROOT_INODE" \
        --cleanup-only || {
            stable_error "CAPTURE_RETENTION_CLEANUP_FAILED"
            return 1
        }
    LAST_RETENTION_CLEANUP="$now"
}

update_legacy_degraded_status() {
    local container_id="$1"
    if (( SELECTED_DEGRADED )); then
        if [[ -z "${SELECTOR_DEGRADED_REPORTED[$container_id]:-}" ]]; then
            stable_error "CAPTURE_SOURCE_STATUS_INCOMPLETE_LEGACY_SELECTOR"
            SELECTOR_DEGRADED_REPORTED["$container_id"]=1
        fi
    else
        unset 'SELECTOR_DEGRADED_REPORTED[$container_id]'
    fi
}

discovery_container_ids() {
    local project output container_id
    page6_verify_docker_daemon || return 1
    for project in "$PRIMARY_PROJECT" "$PLATFORM_PROJECT"; do
        output="$(page6_docker ps --no-trunc --filter status=running --filter "label=com.docker.compose.project=${project}" --format '{{.ID}}')" || return 1
        while IFS= read -r container_id; do
            [[ -n "$container_id" ]] || continue
            is_full_hex_id "$container_id" || { stable_error "CAPTURE_DISCOVERY_ID_INVALID"; return 1; }
            printf '%s\n' "$container_id"
        done <<< "$output"
    done
}

reconcile() {
    local requested_id metadata container_id container_name project_label environment_label scope_label capture_label service_label image_id running_state networks selected_service selected_stream pid process_state discovered=""
    local -a requested_ids=()
    local -A requested_seen=()
    CAPTURE_SEEN=()
    if [[ "$CAPTURE_MODE" == "STRICT_SELECTORS" ]]; then
        requested_ids=("${SELECTOR_CONTAINER_IDS[@]}")
    else
        discovered="$(discovery_container_ids)" || { stable_error "CAPTURE_DISCOVERY_LIST_FAILED"; return 1; }
        if [[ -n "$discovered" ]]; then mapfile -t requested_ids <<< "$discovered"; fi
        if (( SELECTORS_LOADED )); then requested_ids+=("${SELECTOR_CONTAINER_IDS[@]}"); fi
    fi
    for requested_id in "${requested_ids[@]}"; do
        [[ -n "$requested_id" && -z "${requested_seen[$requested_id]:-}" ]] || continue
        requested_seen["$requested_id"]=1
        metadata="$(inspect_candidate "$requested_id")" || {
            [[ -n "${SELECTOR_NAME[$requested_id]:-}" && -z "${SELECTOR_MISSING_REPORTED[$requested_id]:-}" ]] && { stable_error "CAPTURE_SOURCE_STATUS_INCOMPLETE_SELECTOR_UNAVAILABLE"; SELECTOR_MISSING_REPORTED["$requested_id"]=1; }
            continue
        }
        unset 'SELECTOR_MISSING_REPORTED[$requested_id]'
        IFS='|' read -r container_id container_name project_label environment_label scope_label capture_label service_label image_id running_state networks <<< "$metadata"
        container_name="${container_name#/}"
        metadata_fields_safe "$container_id" "$container_name" "$project_label" "$environment_label" "$scope_label" "$capture_label" "$service_label" "$image_id" "$running_state" "$networks" || { stable_error "CAPTURE_DISCOVERY_METADATA_INVALID"; continue; }
        [[ "$container_id" == "$requested_id" ]] || { stable_error "CAPTURE_DISCOVERY_SCOPE_REJECTED"; continue; }
        if [[ -n "${SELECTOR_NAME[$container_id]:-}" ]]; then
            candidate_allowed "$container_id" "$container_name" "$project_label" "$environment_label" "$scope_label" "$capture_label" "$service_label" "$image_id" "$running_state" "$networks"
        else
            discovery_candidate_allowed "$container_id" "$container_name" "$project_label" "$service_label" "$image_id" "$running_state" "$networks"
        fi || { stable_error "CAPTURE_DISCOVERY_SCOPE_REJECTED"; continue; }
        selected_service="$SELECTED_SERVICE"; selected_stream="$SELECTED_STREAM"
        update_legacy_degraded_status "$container_id"
        CAPTURE_SEEN["$container_id"]=1
        pid="${CAPTURE_PIDS[$container_id]:-}"
        if [[ "$pid" =~ ^[0-9]+$ ]]; then
            if page6_verify_process_identity "$pid" && [[ "${CAPTURE_SERVICES[$container_id]:-}" == "$selected_service" && "${CAPTURE_STREAMS[$container_id]:-}" == "$selected_stream" ]]; then continue; else process_state=$?; fi
            if (( process_state == 2 )); then stable_error "CAPTURE_WORKER_IDENTITY_CHANGED"; continue; fi
            stop_capture "$container_id" || { stable_error "CAPTURE_WORKER_STOP_FAILED"; continue; }
        fi
        start_capture "$container_id" "$selected_service" "$selected_stream" "$image_id" || stable_error "CAPTURE_WORKER_START_FAILED"
    done
    for container_id in "${!CAPTURE_PIDS[@]}"; do [[ -n "${CAPTURE_SEEN[$container_id]:-}" ]] || stop_capture "$container_id" || stable_error "CAPTURE_WORKER_STOP_FAILED"; done
}


main() {
    case "${1:-}" in
        --follow-worker)
            [[ "$#" -eq 5 ]] || {
                stable_error "CAPTURE_WORKER_ARGUMENT_INVALID"
                return 64
            }
            ;;
        --check|--self-test)
            [[ "$#" -eq 1 ]] || {
                stable_error "CAPTURE_ARGUMENT_INVALID"
                return 64
            }
            ;;
        "")
            [[ "$#" -eq 0 ]] || {
                stable_error "CAPTURE_ARGUMENT_INVALID"
                return 64
            }
            ;;
        *)
            stable_error "CAPTURE_ARGUMENT_INVALID"
            return 64
            ;;
    esac
    secure_reexec_if_needed "$@"
    page6_verify_docker_daemon ||
        { stable_error "CAPTURE_DOCKER_TRUST_REJECTED"; return 1; }
    case "${1:-}" in
        --self-test)
            self_test
            return
            ;;
        --check)
            validate_log_root
            printf '%s\n' "CAPTURE_CONFIGURATION_OK"
            return
            ;;
        --follow-worker)
            [[ "$#" -eq 5 ]] || {
                stable_error "CAPTURE_WORKER_ARGUMENT_INVALID"
                return 64
            }
            validate_log_root
            [[ "$4" == "APPLICATION" || "$4" == "PLATFORM" ]] || {
                stable_error "CAPTURE_WORKER_STREAM_INVALID"; return 64;
            }
            capture_one "$2" "$3" "$4" "$5"
            return
            ;;
        "")
            ;;
        *)
            stable_error "CAPTURE_ARGUMENT_INVALID"
            return 64
            ;;
    esac

    validate_log_root
    acquire_manager_lock
    trap on_exit EXIT
    trap on_interrupt INT
    trap on_terminate TERM
    while true; do
        cleanup_redacted_files || true
        reconcile || stable_error "CAPTURE_RECONCILE_FAILED"
        sleep "$RECONCILE_SECONDS"
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
