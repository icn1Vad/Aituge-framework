#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

readonly RUN_ID="0123456789abcdef0123456789abcdef"
readonly CONTAINER_NAME="contract-review-code-dev-page6-fake-container-${RUN_ID}"
readonly NETWORK_NAME="contract-review-code-dev-page6-fake-network-${RUN_ID}"
readonly CONTAINER_ID="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
readonly NETWORK_ID="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
FAKE_CREATE_FAIL=0
FAKE_CREATE_FAIL_WITH_RESOURCE=0
FAKE_CONTAINER_EXISTS=0
FAKE_NETWORK_EXISTS=0
FAKE_CONTAINER_INSPECT_FAIL_ONCE=0
FAKE_NETWORK_INSPECT_FAIL_ONCE=0
FAKE_BAD_CONTAINER_ID_ONCE=0
FAKE_BAD_NETWORK_ID_ONCE=0
FAKE_DRIFT=0
FAKE_CONTAINER_REMOVE=""
FAKE_NETWORK_REMOVE=""

# This probe replaces the Docker boundary before exercising the production
# ledger logic. It never talks to a real daemon.
page6_verify_docker_daemon() { return 0; }
page6_docker() {
    local command=${1:-}
    shift || true
    case "$command" in
        create)
            if (( FAKE_CREATE_FAIL_WITH_RESOURCE == 1 )); then
                printf '%s\n' "$CONTAINER_ID"
                return 1
            fi
            (( FAKE_CREATE_FAIL == 0 )) || return 1
            FAKE_CONTAINER_EXISTS=1
            if (( FAKE_BAD_CONTAINER_ID_ONCE == 1 )); then
                FAKE_BAD_CONTAINER_ID_ONCE=0
                printf '%s\n' "invalid-container-id"
            else
                printf '%s\n' "$CONTAINER_ID"
            fi
            ;;
        ps)
            if (( FAKE_CONTAINER_EXISTS == 1 )); then
                printf '%s\n' "$CONTAINER_ID|$CONTAINER_NAME"
            fi
            ;;
        inspect)
            (( FAKE_CONTAINER_EXISTS == 1 )) || return 1
            if (( FAKE_CONTAINER_INSPECT_FAIL_ONCE == 1 )) &&
                [[ ${FUNCNAME[1]-} == page6_validate_container ]]; then
                return 1
            fi
            if (( FAKE_DRIFT == 1 )); then
                printf '%s\n' "$CONTAINER_ID|/$CONTAINER_NAME|$PAGE6_PROJECT|formal|$PAGE6_SCOPE|$RUN_ID"
            else
                printf '%s\n' "$CONTAINER_ID|/$CONTAINER_NAME|$PAGE6_PROJECT|$PAGE6_ENVIRONMENT|$PAGE6_SCOPE|$RUN_ID"
            fi
            ;;
        rm)
            [[ ${1:-} == -f && ${2:-} == "$CONTAINER_ID" && $# -eq 2 ]] || return 1
            FAKE_CONTAINER_REMOVE=$2
            FAKE_CONTAINER_EXISTS=0
            ;;
        network)
            local subcommand=${1:-}
            shift || true
            case "$subcommand" in
                create)
                    if (( FAKE_CREATE_FAIL_WITH_RESOURCE == 1 )); then
                        printf '%s\n' "$NETWORK_ID"
                        return 1
                    fi
                    (( FAKE_CREATE_FAIL == 0 )) || return 1
                    FAKE_NETWORK_EXISTS=1
                    if (( FAKE_BAD_NETWORK_ID_ONCE == 1 )); then
                        FAKE_BAD_NETWORK_ID_ONCE=0
                        printf '%s\n' "invalid-network-id"
                    else
                        printf '%s\n' "$NETWORK_ID"
                    fi
                    ;;
                ls)
                    if (( FAKE_NETWORK_EXISTS == 1 )); then
                        printf '%s\n' "$NETWORK_ID|$NETWORK_NAME"
                    fi
                    ;;
                inspect)
                    (( FAKE_NETWORK_EXISTS == 1 )) || return 1
                    if (( FAKE_NETWORK_INSPECT_FAIL_ONCE == 1 )) &&
                        [[ ${FUNCNAME[1]-} == page6_validate_network ]]; then
                        return 1
                    fi
                    if (( FAKE_DRIFT == 1 )); then
                        printf '%s\n' "$NETWORK_ID|$NETWORK_NAME|$PAGE6_PROJECT|formal|$PAGE6_SCOPE|$RUN_ID"
                    else
                        printf '%s\n' "$NETWORK_ID|$NETWORK_NAME|$PAGE6_PROJECT|$PAGE6_ENVIRONMENT|$PAGE6_SCOPE|$RUN_ID"
                    fi
                    ;;
                rm)
                    [[ ${1:-} == "$NETWORK_ID" && $# -eq 1 ]] || return 1
                    FAKE_NETWORK_REMOVE=$1
                    FAKE_NETWORK_EXISTS=0
                    ;;
                *) return 1 ;;
            esac
            ;;
        *) return 1 ;;
    esac
}

page6_assert_probe_name "$CONTAINER_NAME"
for forbidden_name in \
    "contract-review-code-dev-page6-prod2-${RUN_ID}" \
    "contract-review-code-dev-page6-ProductionX-${RUN_ID}" \
    "contract-review-code-dev-page6-FoRmAlReview-${RUN_ID}"
do
    if page6_assert_probe_name "$forbidden_name"; then
        probe_fail "DOCKER_SAFETY_FORBIDDEN_NAME_ACCEPTED"
    fi
done

# A Docker command failure with an independently confirmed exact-name absence
# clears the pre-create quarantine without issuing any removal.
FAKE_CREATE_FAIL=1
FAKE_CONTAINER_EXISTS=0
FAKE_NETWORK_EXISTS=0
if page6_create_container "$CONTAINER_NAME" "$RUN_ID" caddy:2.11.4-alpine; then
    probe_fail "DOCKER_SAFETY_CREATE_FAILURE_ACCEPTED"
fi
[[ ${#PAGE6_CONTAINER_NAME[@]} -eq 0 &&
    ${#PAGE6_CONTAINER_QUARANTINE_RUN[@]} -eq 0 &&
    $FAKE_CONTAINER_EXISTS -eq 0 &&
    -z $FAKE_CONTAINER_REMOVE ]] ||
    probe_fail "DOCKER_SAFETY_CREATE_FAILURE_REGISTERED_TARGET"
if page6_create_network "$NETWORK_NAME" "$RUN_ID" --internal; then
    probe_fail "DOCKER_SAFETY_NETWORK_CREATE_FAILURE_ACCEPTED"
fi
[[ ${#PAGE6_NETWORK_NAME[@]} -eq 0 &&
    ${#PAGE6_NETWORK_QUARANTINE_RUN[@]} -eq 0 &&
    $FAKE_NETWORK_EXISTS -eq 0 &&
    -z $FAKE_NETWORK_REMOVE ]] ||
    probe_fail "DOCKER_SAFETY_NETWORK_CREATE_FAILURE_REGISTERED_TARGET"

# Docker can return non-zero after the daemon has already created the resource.
# The pre-create quarantine must find, verify and remove that exact resource.
FAKE_CREATE_FAIL=0
FAKE_CREATE_FAIL_WITH_RESOURCE=1
FAKE_CONTAINER_REMOVE=
FAKE_CONTAINER_EXISTS=1
if page6_create_container "$CONTAINER_NAME" "$RUN_ID" caddy:2.11.4-alpine; then
    probe_fail "DOCKER_SAFETY_FAILED_CONTAINER_COMMAND_ACCEPTED"
fi
[[ $FAKE_CONTAINER_REMOVE == "$CONTAINER_ID" &&
    $FAKE_CONTAINER_EXISTS -eq 0 &&
    ${#PAGE6_CONTAINER_NAME[@]} -eq 0 &&
    ${#PAGE6_CONTAINER_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_FAILED_CONTAINER_COMMAND_LEFT_RESIDUE"

FAKE_NETWORK_REMOVE=
FAKE_NETWORK_EXISTS=1
if page6_create_network "$NETWORK_NAME" "$RUN_ID" --internal; then
    probe_fail "DOCKER_SAFETY_FAILED_NETWORK_COMMAND_ACCEPTED"
fi
[[ $FAKE_NETWORK_REMOVE == "$NETWORK_ID" &&
    $FAKE_NETWORK_EXISTS -eq 0 &&
    ${#PAGE6_NETWORK_NAME[@]} -eq 0 &&
    ${#PAGE6_NETWORK_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_FAILED_NETWORK_COMMAND_LEFT_RESIDUE"
FAKE_CREATE_FAIL_WITH_RESOURCE=0

# A pure name conflict carrying different labels is never ours. Recovery must
# refuse deletion, retain quarantine evidence, and clear it only after the
# external owner removes the conflicting object.
FAKE_CREATE_FAIL=1
FAKE_DRIFT=1
FAKE_CONTAINER_REMOVE=
FAKE_CONTAINER_EXISTS=1
if page6_create_container "$CONTAINER_NAME" "$RUN_ID" caddy:2.11.4-alpine; then
    probe_fail "DOCKER_SAFETY_CONTAINER_NAME_CONFLICT_ACCEPTED"
fi
[[ -z $FAKE_CONTAINER_REMOVE &&
    $FAKE_CONTAINER_EXISTS -eq 1 &&
    ${PAGE6_CONTAINER_QUARANTINE_RUN[$CONTAINER_NAME]-} == "$RUN_ID" ]] ||
    probe_fail "DOCKER_SAFETY_CONTAINER_NAME_CONFLICT_DELETED"
FAKE_CONTAINER_EXISTS=0
FAKE_DRIFT=0
page6_cleanup_registered
[[ -z $FAKE_CONTAINER_REMOVE &&
    ${#PAGE6_CONTAINER_NAME[@]} -eq 0 &&
    ${#PAGE6_CONTAINER_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_CONTAINER_NAME_CONFLICT_NOT_RELEASED"

FAKE_DRIFT=1
FAKE_NETWORK_REMOVE=
FAKE_NETWORK_EXISTS=1
if page6_create_network "$NETWORK_NAME" "$RUN_ID" --internal; then
    probe_fail "DOCKER_SAFETY_NETWORK_NAME_CONFLICT_ACCEPTED"
fi
[[ -z $FAKE_NETWORK_REMOVE &&
    $FAKE_NETWORK_EXISTS -eq 1 &&
    ${PAGE6_NETWORK_QUARANTINE_RUN[$NETWORK_NAME]-} == "$RUN_ID" ]] ||
    probe_fail "DOCKER_SAFETY_NETWORK_NAME_CONFLICT_DELETED"
FAKE_NETWORK_EXISTS=0
FAKE_DRIFT=0
page6_cleanup_registered
[[ -z $FAKE_NETWORK_REMOVE &&
    ${#PAGE6_NETWORK_NAME[@]} -eq 0 &&
    ${#PAGE6_NETWORK_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_NETWORK_NAME_CONFLICT_NOT_RELEASED"

# A registered resource is removed only by its 64-character ID.
FAKE_CREATE_FAIL=0
FAKE_CONTAINER_EXISTS=1
page6_create_container "$CONTAINER_NAME" "$RUN_ID" caddy:2.11.4-alpine
[[ $PAGE6_LAST_CONTAINER_ID == "$CONTAINER_ID" ]] || probe_fail "DOCKER_SAFETY_CONTAINER_ID_NOT_RECORDED"
[[ ${#PAGE6_CONTAINER_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_CONTAINER_QUARANTINE_NOT_CLEARED"
page6_cleanup_container "$CONTAINER_ID"
[[ $FAKE_CONTAINER_REMOVE == "$CONTAINER_ID" &&
    $FAKE_CONTAINER_EXISTS -eq 0 &&
    ${#PAGE6_CONTAINER_NAME[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_CONTAINER_NOT_REMOVED_BY_ID"

FAKE_NETWORK_EXISTS=1
page6_create_network "$NETWORK_NAME" "$RUN_ID" --internal
[[ $PAGE6_LAST_NETWORK_ID == "$NETWORK_ID" ]] || probe_fail "DOCKER_SAFETY_NETWORK_ID_NOT_RECORDED"
[[ ${#PAGE6_NETWORK_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_NETWORK_QUARANTINE_NOT_CLEARED"
page6_cleanup_network "$NETWORK_ID"
[[ $FAKE_NETWORK_REMOVE == "$NETWORK_ID" &&
    $FAKE_NETWORK_EXISTS -eq 0 &&
    ${#PAGE6_NETWORK_NAME[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_NETWORK_NOT_REMOVED_BY_ID"

# A successful create with malformed stdout cannot enter the trusted ledger.
# Quarantine recovery must re-list the exact name, verify every label and
# remove only the daemon's full 64-character ID.
FAKE_CONTAINER_REMOVE=
FAKE_BAD_CONTAINER_ID_ONCE=1
FAKE_CONTAINER_EXISTS=1
if page6_create_container "$CONTAINER_NAME" "$RUN_ID" caddy:2.11.4-alpine; then
    probe_fail "DOCKER_SAFETY_BAD_CONTAINER_ID_ACCEPTED"
fi
FAKE_BAD_CONTAINER_ID_ONCE=0
[[ $FAKE_CONTAINER_REMOVE == "$CONTAINER_ID" &&
    $FAKE_CONTAINER_EXISTS -eq 0 &&
    -z $PAGE6_LAST_CONTAINER_ID &&
    ${#PAGE6_CONTAINER_NAME[@]} -eq 0 &&
    ${#PAGE6_CONTAINER_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_BAD_CONTAINER_ID_NOT_RECOVERED"

FAKE_NETWORK_REMOVE=
FAKE_BAD_NETWORK_ID_ONCE=1
FAKE_NETWORK_EXISTS=1
if page6_create_network "$NETWORK_NAME" "$RUN_ID" --internal; then
    probe_fail "DOCKER_SAFETY_BAD_NETWORK_ID_ACCEPTED"
fi
FAKE_BAD_NETWORK_ID_ONCE=0
[[ $FAKE_NETWORK_REMOVE == "$NETWORK_ID" &&
    $FAKE_NETWORK_EXISTS -eq 0 &&
    -z $PAGE6_LAST_NETWORK_ID &&
    ${#PAGE6_NETWORK_NAME[@]} -eq 0 &&
    ${#PAGE6_NETWORK_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_BAD_NETWORK_ID_NOT_RECOVERED"

# A successful create followed by an initial inspect failure must also recover
# through the independently verified quarantine path.
FAKE_CONTAINER_REMOVE=
FAKE_CONTAINER_INSPECT_FAIL_ONCE=1
FAKE_CONTAINER_EXISTS=1
if page6_create_container "$CONTAINER_NAME" "$RUN_ID" caddy:2.11.4-alpine; then
    probe_fail "DOCKER_SAFETY_CONTAINER_VERIFY_FAILURE_ACCEPTED"
fi
FAKE_CONTAINER_INSPECT_FAIL_ONCE=0
[[ $FAKE_CONTAINER_REMOVE == "$CONTAINER_ID" &&
    $FAKE_CONTAINER_EXISTS -eq 0 &&
    -z $PAGE6_LAST_CONTAINER_ID &&
    ${#PAGE6_CONTAINER_NAME[@]} -eq 0 &&
    ${#PAGE6_CONTAINER_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_CONTAINER_VERIFY_FAILURE_NOT_RECOVERED"

FAKE_NETWORK_REMOVE=
FAKE_NETWORK_INSPECT_FAIL_ONCE=1
FAKE_NETWORK_EXISTS=1
if page6_create_network "$NETWORK_NAME" "$RUN_ID" --internal; then
    probe_fail "DOCKER_SAFETY_NETWORK_VERIFY_FAILURE_ACCEPTED"
fi
FAKE_NETWORK_INSPECT_FAIL_ONCE=0
[[ $FAKE_NETWORK_REMOVE == "$NETWORK_ID" &&
    $FAKE_NETWORK_EXISTS -eq 0 &&
    -z $PAGE6_LAST_NETWORK_ID &&
    ${#PAGE6_NETWORK_NAME[@]} -eq 0 &&
    ${#PAGE6_NETWORK_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_NETWORK_VERIFY_FAILURE_NOT_RECOVERED"

# If identity cannot be proven, quarantine recovery emits a stable manual
# action marker, performs no blind name deletion and retains evidence. The
# global cleanup retries quarantines before normal ledger entries.
FAKE_CONTAINER_REMOVE=
FAKE_CONTAINER_EXISTS=1
FAKE_DRIFT=1
if page6_create_container "$CONTAINER_NAME" "$RUN_ID" caddy:2.11.4-alpine; then
    probe_fail "DOCKER_SAFETY_CONTAINER_DRIFT_ACCEPTED"
fi
[[ -z $FAKE_CONTAINER_REMOVE &&
    $FAKE_CONTAINER_EXISTS -eq 1 &&
    ${PAGE6_CONTAINER_QUARANTINE_RUN[$CONTAINER_NAME]-} == "$RUN_ID" &&
    ${PAGE6_CONTAINER_NAME[$CONTAINER_ID]-} == "$CONTAINER_NAME" &&
    -z $PAGE6_LAST_CONTAINER_ID ]] ||
    probe_fail "DOCKER_SAFETY_CONTAINER_QUARANTINE_EVIDENCE_LOST"
FAKE_DRIFT=0
page6_cleanup_registered
[[ $FAKE_CONTAINER_REMOVE == "$CONTAINER_ID" &&
    $FAKE_CONTAINER_EXISTS -eq 0 &&
    ${#PAGE6_CONTAINER_NAME[@]} -eq 0 &&
    ${#PAGE6_CONTAINER_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_CONTAINER_QUARANTINE_RETRY_FAILED"

FAKE_NETWORK_REMOVE=
FAKE_NETWORK_EXISTS=1
FAKE_DRIFT=1
if page6_create_network "$NETWORK_NAME" "$RUN_ID" --internal; then
    probe_fail "DOCKER_SAFETY_NETWORK_DRIFT_ACCEPTED"
fi
[[ -z $FAKE_NETWORK_REMOVE &&
    $FAKE_NETWORK_EXISTS -eq 1 &&
    ${PAGE6_NETWORK_QUARANTINE_RUN[$NETWORK_NAME]-} == "$RUN_ID" &&
    ${PAGE6_NETWORK_NAME[$NETWORK_ID]-} == "$NETWORK_NAME" &&
    -z $PAGE6_LAST_NETWORK_ID ]] ||
    probe_fail "DOCKER_SAFETY_NETWORK_QUARANTINE_EVIDENCE_LOST"
FAKE_DRIFT=0
page6_cleanup_registered
[[ $FAKE_NETWORK_REMOVE == "$NETWORK_ID" &&
    $FAKE_NETWORK_EXISTS -eq 0 &&
    ${#PAGE6_NETWORK_NAME[@]} -eq 0 &&
    ${#PAGE6_NETWORK_QUARANTINE_RUN[@]} -eq 0 ]] ||
    probe_fail "DOCKER_SAFETY_NETWORK_QUARANTINE_RETRY_FAILED"

probe_pass "DOCKER_QUARANTINE_RECOVERY_OK"
probe_pass "DOCKER_SAFETY_VALIDATION_OK"
