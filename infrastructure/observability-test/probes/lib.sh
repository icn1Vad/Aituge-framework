#!/usr/bin/env bash

PROBE_LIBRARY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=../docker-trust.sh
source "${PROBE_LIBRARY_DIR}/../docker-trust.sh"
readonly TEST_PRIVATE_ROOT="/home/aituge/contract-review-code-dev-test-private"
readonly TEST_SECRETS_DIR="${TEST_PRIVATE_ROOT}/secrets"
readonly TEST_OBSERVABILITY_PARENT="${TEST_PRIVATE_ROOT}/observability-logs"
PROBE_PRIVATE_CREATED=0
PROBE_PARENT_CREATED=0
PROBE_DIR=""
PROBE_CLEANUP_FUNCTION=""
PROBE_CLEANUP_DONE=0
readonly PROBE_CURL_BIN="/usr/bin/curl"

probe_fail() {
    printf '%s\n' "$1" >&2
    return 1
}

probe_pass() {
    printf '%s\n' "$1"
}

probe_invoke_cleanup() {
    if (( PROBE_CLEANUP_DONE )); then
        return 0
    fi
    PROBE_CLEANUP_DONE=1
    if [[ -n "$PROBE_CLEANUP_FUNCTION" ]]; then
        "$PROBE_CLEANUP_FUNCTION"
    fi
}

probe_handle_exit() {
    local status=$?
    trap - EXIT
    if ! probe_invoke_cleanup && (( status == 0 )); then
        status=1
    fi
    exit "$status"
}

probe_handle_interrupt() {
    trap - INT
    probe_invoke_cleanup || true
    exit 130
}

probe_handle_terminate() {
    trap - TERM
    probe_invoke_cleanup || true
    exit 143
}

probe_install_cleanup_traps() {
    local cleanup_function="$1"
    [[ "$cleanup_function" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] ||
        probe_fail "PROBE_CLEANUP_FUNCTION_INVALID"
    declare -F "$cleanup_function" >/dev/null ||
        probe_fail "PROBE_CLEANUP_FUNCTION_MISSING"
    PROBE_CLEANUP_FUNCTION="$cleanup_function"
    PROBE_CLEANUP_DONE=0
    trap probe_handle_exit EXIT
    trap probe_handle_interrupt INT
    trap probe_handle_terminate TERM
}

probe_http_request() {
    local url="${1:-}"
    local argument
    [[ -x "$PROBE_CURL_BIN" ]] || {
        probe_fail "PROBE_CURL_BINARY_MISSING"
        return 69
    }
    [[ "$url" =~ ^https?://[A-Za-z0-9._:-]+(/[^[:space:]]*)?$ ]] || {
        probe_fail "PROBE_CURL_URL_REJECTED"
        return 64
    }
    shift
    for argument in "$@"; do
        case "$argument" in
            -q|-K|-L|--config|--config=*|--disable|--location|--location-trusted|\
            --max-redirs|--max-redirs=*|--proto|--proto=*|--proto-redir|\
            --proto-redir=*|--url|--url=*|--connect-timeout|--connect-timeout=*|\
            --max-time|--max-time=*)
                probe_fail "PROBE_CURL_ARGUMENT_REJECTED"
                return 64
                ;;
        esac
    done
    "$PROBE_CURL_BIN" --disable \
        --noproxy '*' \
        --proto '=http,https' \
        --proto-redir '=http,https' \
        --max-redirs 0 \
        --connect-timeout 2 \
        --max-time 10 \
        "$@" \
        "$url"
}

page6_validate_approved_image_reference() {
    local image_reference="${1:-}"
    local expected_repository_tag="${2:-}"
    local digest
    [[ -n "$image_reference" && -n "$expected_repository_tag" ]] || {
        probe_fail "APPROVED_IMAGE_REFERENCE_EMPTY"
        return 1
    }
    [[ "$image_reference" == "$expected_repository_tag"@sha256:* ]] || {
        probe_fail "APPROVED_IMAGE_REPOSITORY_OR_TAG_REJECTED"
        return 1
    }
    digest="${image_reference#*@}"
    [[ "$digest" != "$image_reference" && "$digest" != *"@"* ]] || {
        probe_fail "APPROVED_IMAGE_REFERENCE_AMBIGUOUS"
        return 1
    }
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
        probe_fail "APPROVED_IMAGE_DIGEST_REJECTED"
        return 1
    }
}

page6_validate_local_image_reference() {
    local image_reference="${1:-}"
    local expected_repository_tag="${2:-}"
    local digest
    local repository
    local expected_repo_digest
    local exact_id
    local tagged_id
    local repo_digests
    page6_validate_approved_image_reference "$image_reference" "$expected_repository_tag" ||
        return 1
    digest="${image_reference#*@}"
    repository="${expected_repository_tag%:*}"
    expected_repo_digest="${repository}@${digest}"
    page6_docker image inspect "$image_reference" >/dev/null 2>&1 || {
        probe_fail "APPROVED_IMAGE_NOT_LOCAL"
        return 1
    }
    exact_id="$(page6_docker image inspect --format '{{.Id}}' "$image_reference")" ||
        return 1
    tagged_id="$(page6_docker image inspect --format '{{.Id}}' "$expected_repository_tag")" ||
        return 1
    [[ "$exact_id" == "$tagged_id" && "$exact_id" =~ ^sha256:[0-9a-f]{64}$ ]] || {
        probe_fail "APPROVED_IMAGE_ID_MISMATCH"
        return 1
    }
    repo_digests="$(page6_docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image_reference")" ||
        return 1
    grep -Fxq -- "$expected_repo_digest" <<< "$repo_digests" || {
        probe_fail "APPROVED_IMAGE_REPODIGEST_MISMATCH"
        return 1
    }
}

path_has_symlink_component() {
    local candidate="$1"
    local current="/"
    local part
    local -a parts=()
    IFS='/' read -r -a parts <<< "$candidate"
    for part in "${parts[@]}"; do
        [[ -z "$part" ]] && continue
        current="${current%/}/$part"
        [[ -L "$current" ]] && return 0
    done
    return 1
}

create_probe_dir() {
    [[ -d /home/aituge && ! -L /home/aituge ]] ||
        probe_fail "PROBE_TEST_HOME_INVALID"
    [[ "$(stat -c '%a|%u|%g' -- /home/aituge)" == "750|$(id -u)|$(id -g)" ]] ||
        probe_fail "PROBE_TEST_HOME_PERMISSIONS"
    path_has_symlink_component "$TEST_PRIVATE_ROOT" &&
        probe_fail "PROBE_PRIVATE_ROOT_SYMLINK"
    path_has_symlink_component "$TEST_OBSERVABILITY_PARENT" &&
        probe_fail "PROBE_TEST_PARENT_SYMLINK"

    if [[ ! -d "$TEST_PRIVATE_ROOT" ]]; then
        mkdir -m 700 -- "$TEST_PRIVATE_ROOT"
        PROBE_PRIVATE_CREATED=1
    fi
    [[ -d "$TEST_PRIVATE_ROOT" && ! -L "$TEST_PRIVATE_ROOT" ]] ||
        probe_fail "PROBE_PRIVATE_ROOT_INVALID"
    [[ "$(stat -c '%a|%u|%g' -- "$TEST_PRIVATE_ROOT")" == \
        "700|$(id -u)|$(id -g)" ]] ||
        probe_fail "PROBE_PRIVATE_ROOT_PERMISSIONS"
    if [[ ! -d "$TEST_OBSERVABILITY_PARENT" ]]; then
        mkdir -m 700 -- "$TEST_OBSERVABILITY_PARENT"
        PROBE_PARENT_CREATED=1
    fi
    [[ -d "$TEST_OBSERVABILITY_PARENT" && ! -L "$TEST_OBSERVABILITY_PARENT" ]] ||
        probe_fail "PROBE_TEST_PARENT_INVALID"
    path_has_symlink_component "$TEST_OBSERVABILITY_PARENT" &&
        probe_fail "PROBE_TEST_PARENT_SYMLINK"
    PROBE_DIR="$(mktemp -d "${TEST_OBSERVABILITY_PARENT}/page6-probe.XXXXXX")"
    chmod 700 -- "$PROBE_DIR"
}

cleanup_probe_dir() {
    local resolved=""
    if [[ -n "$PROBE_DIR" && -d "$PROBE_DIR" && ! -L "$PROBE_DIR" ]]; then
        resolved="$(realpath -e -- "$PROBE_DIR")"
        case "$resolved" in
            "$TEST_OBSERVABILITY_PARENT"/page6-probe.*)
                find "$resolved" -xdev -type f -delete
                find "$resolved" -xdev -type l -delete
                find "$resolved" -xdev -depth -type d -empty -delete
                ;;
            *)
                probe_fail "PROBE_CLEANUP_SCOPE_REJECTED" || true
                ;;
        esac
    fi
    if [[ "$PROBE_PARENT_CREATED" -eq 1 ]]; then
        rmdir -- "$TEST_OBSERVABILITY_PARENT" 2>/dev/null || true
    fi
    if [[ "$PROBE_PRIVATE_CREATED" -eq 1 ]]; then
        rmdir -- "$TEST_PRIVATE_ROOT" 2>/dev/null || true
    fi
}

validate_private_directory() {
    local raw_path="$1"
    local allowed_parent="$2"
    local error_code="$3"
    local resolved
    [[ -n "$raw_path" && "$raw_path" == /* && -d "$raw_path" && ! -L "$raw_path" ]] || {
        probe_fail "$error_code"
        return 1
    }
    if path_has_symlink_component "$raw_path"; then
        probe_fail "$error_code"
        return 1
    fi
    resolved="$(realpath -e -- "$raw_path")" || {
        probe_fail "$error_code"
        return 1
    }
    case "$resolved" in
        "$allowed_parent"/*) ;;
        *)
            probe_fail "$error_code"
            return 1
            ;;
    esac
    [[ "$(stat -c '%a|%u|%g' -- "$resolved")" == \
        "700|$(id -u)|$(id -g)" ]] || {
        probe_fail "$error_code"
        return 1
    }
    printf '%s\n' "$resolved"
}

validate_secret_files() {
    local raw_path="$1"
    local leaf
    local candidate
    for leaf in deepseek_api_key dashscope_api_key grafana_admin_password; do
        candidate="${raw_path}/${leaf}"
        [[ -f "$candidate" && ! -L "$candidate" && -r "$candidate" ]] || {
            probe_fail "PROBE_SECRET_FILE_INVALID"
            return 1
        }
        [[ "$(stat -c '%a|%u|%g|%h' -- "$candidate")" == \
            "600|$(id -u)|$(id -g)|1" ]] || {
            probe_fail "PROBE_SECRET_FILE_METADATA_INVALID"
            return 1
        }
    done
}

validate_test_secret_directory() {
    local raw_path="$1"
    local resolved
    [[ "$raw_path" == "$TEST_SECRETS_DIR" ]] || {
        probe_fail "PROBE_SECRET_DIRECTORY_OUTSIDE_EXACT_ROOT"
        return 1
    }
    validate_private_directory \
        "$TEST_PRIVATE_ROOT" /home/aituge "PROBE_PRIVATE_ROOT_INVALID" \
        >/dev/null || return 1
    resolved="$(validate_private_directory \
        "$raw_path" "$TEST_PRIVATE_ROOT" "PROBE_SECRET_DIRECTORY_INVALID")" ||
        return 1
    [[ "$resolved" == "$TEST_SECRETS_DIR" ]] || {
        probe_fail "PROBE_SECRET_DIRECTORY_OUTSIDE_EXACT_ROOT"
        return 1
    }
    validate_secret_files "$resolved"
}

validate_test_secret_fixture_directory() {
    local raw_path="$1"
    local resolved
    [[ -n "$PROBE_DIR" ]] || {
        probe_fail "PROBE_SECRET_FIXTURE_CONTEXT_MISSING"
        return 1
    }
    resolved="$(validate_private_directory \
        "$raw_path" "$TEST_OBSERVABILITY_PARENT" \
        "PROBE_SECRET_FIXTURE_DIRECTORY_INVALID")" || return 1
    [[ "$resolved" == "$PROBE_DIR/secrets" ]] || {
        probe_fail "PROBE_SECRET_FIXTURE_OUTSIDE_RUN"
        return 1
    }
    validate_secret_files "$resolved"
}

repo_root_from_probe() {
    local probe_dir
    probe_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[1]}")" && pwd -P)"
    realpath -e -- "${probe_dir}/../../.."
}
