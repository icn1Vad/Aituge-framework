#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

readonly HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
readonly REPO_ROOT="$(repo_root_from_probe)"
readonly STACK_DIR="${REPO_ROOT}/infrastructure/observability-test"
readonly REDACTOR="${STACK_DIR}/log-capture/redact_stream.py"
readonly CAPTURE_SCRIPT="${STACK_DIR}/log-capture/capture-test-container-logs.sh"
readonly ALLOY_CONFIG="${STACK_DIR}/alloy/config.alloy"
readonly OBS_COMPOSE="${STACK_DIR}/compose.observability-test.yml"
readonly CONTAINER_ID="$(printf 'a%.0s' {1..64})"
readonly SYMLINK_ID="$(printf 'b%.0s' {1..64})"
readonly OLD_ID="$(printf 'c%.0s' {1..64})"
readonly HARDLINK_ID="$(printf 'f%.0s' {1..64})"
readonly MODE_ID="$(printf '1%.0s' {1..64})"
readonly ANCESTOR_ID="$(printf '6%.0s' {1..64})"
readonly CANARY="$(python3 -c 'import secrets; print("PAGE6_" + secrets.token_hex(24))')"

page6_verify_docker_daemon || probe_fail "REDACTION_DOCKER_TRUST_REJECTED"
create_probe_dir
readonly OUTSIDE_TRAP="$(mktemp -d \
    "${TEST_PRIVATE_ROOT}/page6-redactor-outside.XXXXXX")"
chmod 700 -- "$OUTSIDE_TRAP"
probe_local_cleanup() {
    local resolved=""
    if [[ -d "$OUTSIDE_TRAP" && ! -L "$OUTSIDE_TRAP" ]]; then
        resolved="$(realpath -e -- "$OUTSIDE_TRAP")"
        case "$resolved" in
            "$TEST_PRIVATE_ROOT"/page6-redactor-outside.*)
                find "$resolved" -xdev -type f -delete
                find "$resolved" -xdev -type l -delete
                find "$resolved" -xdev -depth -type d -empty -delete
                ;;
            *) probe_fail "REDACTION_OUTSIDE_CLEANUP_SCOPE_REJECTED" || true ;;
        esac
    fi
    cleanup_probe_dir
}
probe_install_cleanup_traps probe_local_cleanup
readonly LOG_ROOT="${PROBE_DIR}/capture"
readonly INPUT_FILE="${PROBE_DIR}/input.bin"
readonly ERROR_FILE="${PROBE_DIR}/redactor.err"
readonly CAPTURE_SELECTOR_FILE="${PROBE_DIR}/capture-selectors"
readonly CAPTURE_SELECTOR_ID="$(printf '2%.0s' {1..64})"
readonly CAPTURE_SELECTOR_IMAGE="sha256:$(printf '3%.0s' {1..64})"
mkdir -m 700 -- "$LOG_ROOT"
printf '%s\n' \
    "${CAPTURE_SELECTOR_ID}|contract-review-code-dev-page6-redaction-probe|contract-review-code-dev|page6-test|contract-review-code-dev|APPLICATION|${CAPTURE_SELECTOR_IMAGE}|STRICT" \
    > "$CAPTURE_SELECTOR_FILE"
chmod 600 -- "$CAPTURE_SELECTOR_FILE"

python3 - "$CANARY" > "$INPUT_FILE" <<'PY'
import json
import sys

sentinel = sys.argv[1]
encoded_key = sentinel.encode("utf-8").hex()
record = {
    "level": "ERROR",
    "logger": "page6.test",
    "event_code": "CONTRACT_REVIEW_STARTED",
    "display_code": "CONTRACT_REVIEW_STARTED",
    "error_code": "PYTHON_FILE_HTTP_500",
    "exception_type": "Page6SafeError",
    "operation": "CONTRACT_REVIEW",
    "outcome": "FAILURE",
    "status": "FAILED",
    "provider": "deepseek",
    "model_name": "deepseek-v4-pro",
    "model_pack_id": "pack_contract_review",
    "model_pack_version": "1.5.0",
    "privacy_mode": "PRIVATE",
    "route_type": "EXTERNAL",
    "feature_code": "CONTRACT_REVIEW",
    "stage_code": "CONTRACT_REVIEW_PARTY_RESOLUTION",
    "http_request_method": "POST",
    "authorization": "Bearer " + sentinel,
    "prompt": sentinel,
    "response_body": sentinel,
    "accessSessionId": sentinel,
    "accessReason": sentinel,
    "querySnapshotId": sentinel,
    "pointInTimeToken": sentinel,
    "idempotencyKey": sentinel,
    "streamToken": sentinel,
    "downloadToken": sentinel,
    "download_token": sentinel,
    "db_password": sentinel,
    "databasePassword": sentinel,
    "secret_key": sentinel,
    "api_secret": sentinel,
    "exception": {"type": "Page6SafeError", "message": sentinel, "stacktrace": sentinel},
    "nested": {"mysql_password": sentinel, "dashscope_api_key": sentinel},
    "client": {"address": "203.0.113.19"},
    "source_ip": "198.51.100.27",
    "network": {"peer": {"address": "192.0.2.44"}},
    "X-Observability-Scope": sentinel,
    "input_tokens": 12,
    "attempt_no": 2,
    "sequence": 42,
    "duration_ms": 1234,
    "http_response_status_code": 503,
    "cost_amount": 1.25,
    "retryable": True,
    "contract_amount": 1000000,
    "\u5408\u540c\u91d1\u989d": 1000000,
    "chinese_contract_fields": [
        {"label": "\u5408\u540c\u91d1\u989d", "amount": 1000000}
    ],
    "bank_account": 6222020202020202020,
    "phone_number": 13800138000,
    "coordinates": [116.397, 39.908],
    "privacy_confirmed": True,
    "unknown_null": None,
    "numeric_matrix": [
        [1000000, 6222020202020202020, 13800138000, True, None]
    ],
    "invalid_attempt_no": 1000001,
    "invalid_status_code": 999,
    "negative_cost_amount": -1.0,
    "invalid_success_rate": 2.0,
    "tenantId": "123456789012345678",
    "request_id": "11111111111111111111111111111111",
    "traceId": "22222222222222222222222222222222",
    "task-id": "1235209075512332288",
    "run.id": "33333333-3333-4333-8333-333333333333",
    "scopeType": "TENANT",
    sentinel: "value-hidden-with-dynamic-key",
    encoded_key: "value-hidden-with-encoded-key",
    "\u5408\u540c" + sentinel: "value-hidden-with-chinese-key",
    "message": (
        "safe-prefix\u0001 cursor=" + sentinel + " locator=" + sentinel +
        " access_token=" + sentinel + " refresh_token=" + sentinel
    ),
    "arbitrary_text": "未注册字段中的合同正文" + sentinel,
    "unregistered_array": ["未注册数组中的模型回答" + sentinel],
    "nested_error": {
        "detail": "嵌套错误中的合同正文" + sentinel,
        "items": [{"content": "嵌套数组中的模型输出" + sentinel}],
    },
}
sys.stdout.buffer.write(
    ("2026-07-31T00:00:00.000000000Z " + json.dumps(record) + "\n").encode()
)
sys.stdout.buffer.write(
    ("\x1b[31mAuthorization=" + sentinel + " accessSessionId=" + sentinel +
     " downloadToken=" + sentinel + " db_password=" + sentinel +
     " exception.message=" + sentinel + " remote_ip=198.51.100.27" +
     " https://test.invalid/path?highWatermark=" + sentinel +
     "&download_token=" + sentinel + "\x1b[0m\n").encode()
)
sys.stdout.buffer.write(("甲乙双方约定的完整合同正文" + sentinel + "\n").encode())
sys.stdout.buffer.write(("请审查以上合同并输出完整回答" + sentinel + "\n").encode())
sys.stdout.buffer.write(b'{"malformed":\n')
sys.stdout.buffer.write(b"x" * (300 * 1024) + b"\n")
sys.stdout.buffer.write(b'{"number":NaN}\n')
sys.stdout.buffer.write(b'{"number":Infinity}\n')
sys.stdout.buffer.write(b'{"cost_amount":1e999}\n')
deep = []
cursor = deep
for _ in range(20):
    cursor.append([])
    cursor = cursor[0]
sys.stdout.buffer.write((json.dumps(deep) + "\n").encode())
unsafe_allowed_values = {
    "sequence": 777,
    "level": sentinel,
    "logger": sentinel,
    "event_code": sentinel,
    "operation": sentinel,
    "provider": sentinel,
    "model_name": sentinel,
    "scope_type": "SYSTEM",
}
sys.stdout.buffer.write((json.dumps(unsafe_allowed_values) + "\n").encode())
sys.stdout.buffer.write(
    b'{"event_code":"PAGE6_INVALID_SCOPE_SHOULD_DROP","scope_type":"TENANTX"}\n'
)
sys.stdout.buffer.write(
    (
        '{"event_code":"PAGE6_INVALID_SCHEMA_ID_SHOULD_DROP",'
        '"tenant_id":' + json.dumps(sentinel) + "}\n"
    ).encode()
)
sys.stdout.buffer.write(b'{"event_code":"PAGE6_SAFE_AFTER_REJECTIONS","message":"discard-me","scope_type":"SYSTEM"}\n')
PY
chmod 600 -- "$INPUT_FILE"

python3 "$REDACTOR"     --output-dir "$LOG_ROOT"     --container-id "$CONTAINER_ID"     --service "page6-test" --stream APPLICATION     < "$INPUT_FILE" 2> "$ERROR_FILE"

readonly OUTPUT_FILE="${LOG_ROOT}/${CONTAINER_ID}.jsonl"
readonly INVALID_STREAM_ID="$(printf '9%.0s' {1..64})"
if printf '%s\n' '{"event_code":"PAGE8_STREAM_REJECT"}' | \
    python3 "$REDACTOR" --output-dir "$LOG_ROOT" \
        --container-id "$INVALID_STREAM_ID" --service page6-test \
        --stream SECURITY >/dev/null 2>"${PROBE_DIR}/invalid-stream.err"; then
    probe_fail "REDACTION_ARBITRARY_STREAM_ACCEPTED"
fi
grep -Fxq "REDACTOR_STREAM_INVALID" "${PROBE_DIR}/invalid-stream.err" ||
    probe_fail "REDACTION_STREAM_ERROR_NOT_STABLE"
readonly PLATFORM_STREAM_ID="$(printf '8%.0s' {1..64})"
printf '%s\n' '{"event_code":"PAGE8_PLATFORM_STREAM"}' | \
    python3 "$REDACTOR" --output-dir "$LOG_ROOT" \
        --container-id "$PLATFORM_STREAM_ID" --service alloy --stream PLATFORM
python3 - "$LOG_ROOT/${PLATFORM_STREAM_ID}.jsonl" <<'PY_STREAM'
import json
import pathlib
import sys
record = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()[-1])
if record.get("stream") != "PLATFORM":
    raise SystemExit("REDACTION_PLATFORM_STREAM_NOT_PRESERVED")
PY_STREAM
[[ -f "$OUTPUT_FILE" && ! -L "$OUTPUT_FILE" ]] ||     probe_fail "REDACTION_OUTPUT_MISSING"
[[ "$(stat -c '%a' -- "$OUTPUT_FILE")" == "600" ]] ||     probe_fail "REDACTION_OUTPUT_MODE_INVALID"
if grep -Fq -- "$CANARY" "$OUTPUT_FILE"; then
    probe_fail "REDACTION_SENTINEL_LEAK"
fi

python3 - "$OUTPUT_FILE" "$CANARY" <<'PY'
import json
import pathlib
import sys

lines = pathlib.Path(sys.argv[1]).read_bytes().splitlines()
encoded_key = sys.argv[2].encode("utf-8").hex().encode("ascii")
if any(encoded_key in line for line in lines):
    raise SystemExit("REDACTION_ENCODED_KEY_LEAK")
if len(lines) != 14:
    raise SystemExit("REDACTION_OUTPUT_COUNT_INVALID")
records = [json.loads(line) for line in lines]
for line, record in zip(lines, records):
    if len(line) > 16 * 1024:
        raise SystemExit("REDACTION_OUTPUT_NOT_BOUNDED")
    if record.get("environment") != "test":
        raise SystemExit("REDACTION_ENVIRONMENT_MISSING")
    if record.get("stream") != "APPLICATION":
        raise SystemExit("REDACTION_STREAM_MISSING")
    if any(
        ord(ch) < 32 and ch not in "\t\n\r"
        for ch in json.dumps(record, ensure_ascii=False)
    ):
        raise SystemExit("REDACTION_CONTROL_CHARACTER_RETAINED")

first = records[0]
payload = first.get("record", {})
for key in (
    "authorization", "prompt", "response_body", "accessSessionId",
    "accessReason", "querySnapshotId", "pointInTimeToken", "idempotencyKey",
    "streamToken", "downloadToken", "download_token", "db_password",
    "databasePassword", "secret_key", "api_secret", "source_ip",
    "X-Observability-Scope",
):
    if key in payload:
        raise SystemExit("REDACTION_SENSITIVE_FIELD_RETAINED")
if "message" in payload.get("exception", {}):
    raise SystemExit("REDACTION_EXCEPTION_MESSAGE_RETAINED")
if "stacktrace" in payload.get("exception", {}):
    raise SystemExit("REDACTION_EXCEPTION_STACK_RETAINED")
if payload.get("exception", {}).get("type") != "Page6SafeError":
    raise SystemExit("REDACTION_SAFE_EXCEPTION_TYPE_REMOVED")
for key in ("nested", "client", "network"):
    if key in payload:
        raise SystemExit("REDACTION_UNKNOWN_NESTED_OBJECT_RETAINED")
if payload.get("input_tokens") != 12:
    raise SystemExit("REDACTION_SAFE_COUNTER_REMOVED")
safe_text = {
    "level": "ERROR",
    "logger": "page6.test",
    "event_code": "CONTRACT_REVIEW_STARTED",
    "display_code": "CONTRACT_REVIEW_STARTED",
    "error_code": "PYTHON_FILE_HTTP_500",
    "exception_type": "Page6SafeError",
    "operation": "CONTRACT_REVIEW",
    "outcome": "FAILURE",
    "status": "FAILED",
    "provider": "deepseek",
    "model_name": "deepseek-v4-pro",
    "model_pack_id": "pack_contract_review",
    "model_pack_version": "1.5.0",
    "privacy_mode": "PRIVATE",
    "route_type": "EXTERNAL",
    "feature_code": "CONTRACT_REVIEW",
    "stage_code": "CONTRACT_REVIEW_PARTY_RESOLUTION",
    "http_request_method": "POST",
}
for key, expected in safe_text.items():
    if payload.get(key) != expected:
        raise SystemExit("REDACTION_SAFE_STRUCTURED_VALUE_REMOVED")
safe_scalars = {
    "attempt_no": 2,
    "sequence": 42,
    "duration_ms": 1234,
    "http_response_status_code": 503,
    "cost_amount": 1.25,
    "retryable": True,
}
for key, expected in safe_scalars.items():
    if payload.get(key) != expected:
        raise SystemExit("REDACTION_TYPED_SCALAR_REMOVED")
for key in (
    "contract_amount",
    "bank_account",
    "phone_number",
    "privacy_confirmed",
    "unknown_null",
    "invalid_attempt_no",
    "invalid_status_code",
    "negative_cost_amount",
    "invalid_success_rate",
):
    if key in payload:
        raise SystemExit("REDACTION_UNKNOWN_OR_INVALID_SCALAR_RETAINED")
for key in (
    "coordinates", "chinese_contract_fields", "numeric_matrix", "message",
    "arbitrary_text", "unregistered_array", "nested_error",
):
    if key in payload:
        raise SystemExit("REDACTION_UNKNOWN_KEY_RETAINED")

expected_schema = {
    "tenant_id": "123456789012345678",
    "request_id": "11111111111111111111111111111111",
    "trace_id": "22222222222222222222222222222222",
    "task_id": "1235209075512332288",
    "run_id": "33333333-3333-4333-8333-333333333333",
    "scope_type": "TENANT",
}
for key, value in expected_schema.items():
    if first.get(key) != value:
        raise SystemExit("REDACTION_SCHEMA_FIELD_MISSING")
if first.get("source_timestamp") != "2026-07-31T00:00:00.000000000Z":
    raise SystemExit("REDACTION_SOURCE_TIMESTAMP_MISSING")

codes = [record.get("event_code") for record in records]
if codes.count("LOG_RECORD_DROPPED_INVALID_JSON") != 3:
    raise SystemExit("REDACTION_INVALID_JSON_NOT_DROPPED")
if codes.count("LOG_RECORD_DROPPED_UNSTRUCTURED_TEXT") != 3:
    raise SystemExit("REDACTION_UNSTRUCTURED_TEXT_NOT_DROPPED")
if "LOG_LINE_DROPPED_TOO_LARGE" not in codes:
    raise SystemExit("REDACTION_LARGE_LINE_NOT_DROPPED")
if "LOG_RECORD_DROPPED_NON_FINITE_NUMBER" not in codes:
    raise SystemExit("REDACTION_NON_FINITE_NOT_DROPPED")
if "LOG_RECORD_DROPPED_TOO_DEEP" not in codes:
    raise SystemExit("REDACTION_TOO_DEEP_NOT_DROPPED")
if codes.count("LOG_RECORD_DROPPED_SCHEMA_FIELD_INVALID") != 2:
    raise SystemExit("REDACTION_INVALID_SCHEMA_FIELD_NOT_DROPPED")

unsafe = next(
    (
        record.get("record", {})
        for record in records
        if record.get("record", {}).get("sequence") == 777
    ),
    None,
)
if unsafe is None:
    raise SystemExit("REDACTION_ALLOWED_VALUE_CANARY_RECORD_MISSING")
for key in ("level", "logger", "event_code", "operation", "provider", "model_name"):
    if unsafe.get(key) != "[REDACTED]":
        raise SystemExit("REDACTION_ALLOWED_VALUE_CANARY_RETAINED")
if unsafe.get("scope_type") != "SYSTEM":
    raise SystemExit("REDACTION_VALID_SYSTEM_SCOPE_REMOVED")
if first.get("scope_type") != "TENANT":
    raise SystemExit("REDACTION_VALID_TENANT_SCOPE_REMOVED")
if records[-1].get("record", {}).get("event_code") != "PAGE6_SAFE_AFTER_REJECTIONS":
    raise SystemExit("REDACTION_STREAM_DID_NOT_CONTINUE")
if records[-1].get("scope_type") != "SYSTEM":
    raise SystemExit("REDACTION_SYSTEM_SCOPE_MISSING")
PY

python3 -B - "$REDACTOR" "$LOG_ROOT" <<'PY'
import os
import pathlib
import shutil
import subprocess
import sys
import time

redactor = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2])
bound_root = root.with_name(root.name + ".bound-inode")
container_id = "7" * 64
cleanup_id = "8" * 64
replacement_trap = root / f"{cleanup_id}.jsonl.1"
root_fd = os.open(
    root,
    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
)
root_info = os.fstat(root_fd)
os.rename(root, bound_root)
root.mkdir(mode=0o700)

def inherited_args():
    return [
        str(redactor),
        "--output-dir",
        str(root),
        "--root-fd",
        str(root_fd),
        "--root-device",
        str(root_info.st_dev),
        "--root-inode",
        str(root_info.st_ino),
    ]

try:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            *inherited_args(),
            "--container-id",
            container_id,
            "--service",
            "page6-test",
            "--stream",
            "APPLICATION",
        ],
        input=b'{"event_code":"PAGE6_ROOT_FD_SWAP_WRITE","scope_type":"SYSTEM"}\n',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(root_fd,),
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit("REDACTION_ROOT_FD_SWAP_WRITE_FAILED")
    original_output = bound_root / f"{container_id}.jsonl"
    replacement_output = root / f"{container_id}.jsonl"
    if not original_output.is_file():
        raise SystemExit("REDACTION_ROOT_FD_ORIGINAL_INODE_NOT_WRITTEN")
    if replacement_output.exists():
        raise SystemExit("REDACTION_ROOT_FD_REPLACEMENT_WRITTEN")

    old_file = bound_root / f"{cleanup_id}.jsonl.1"
    old_file.write_text("", encoding="utf-8")
    old_file.chmod(0o600)
    replacement_trap.write_text("replacement-must-survive", encoding="utf-8")
    replacement_trap.chmod(0o600)
    old_time = time.time() - 11 * 24 * 60 * 60
    os.utime(old_file, (old_time, old_time))
    os.utime(replacement_trap, (old_time, old_time))
    cleanup = subprocess.run(
        [sys.executable, "-B", *inherited_args(), "--cleanup-only"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(root_fd,),
        check=False,
    )
    if cleanup.returncode != 0:
        raise SystemExit("REDACTION_ROOT_FD_SWAP_CLEANUP_FAILED")
    if old_file.exists():
        raise SystemExit("REDACTION_ROOT_FD_OLD_INODE_NOT_CLEANED")
    if replacement_trap.read_text(encoding="utf-8") != "replacement-must-survive":
        raise SystemExit("REDACTION_ROOT_FD_REPLACEMENT_CLEANED")

    wrong_identity = subprocess.run(
        [
            sys.executable,
            "-B",
            *inherited_args()[:-1],
            str(root_info.st_ino + 1),
            "--cleanup-only",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(root_fd,),
        check=False,
    )
    if (
        wrong_identity.returncode != 70
        or wrong_identity.stderr.strip() != b"REDACTOR_ROOT_FD_CHANGED"
    ):
        raise SystemExit("REDACTION_ROOT_FD_IDENTITY_CHANGE_ACCEPTED")
finally:
    os.close(root_fd)
    if root.exists() and not root.is_symlink():
        shutil.rmtree(root)
    if bound_root.exists():
        os.rename(bound_root, root)
PY

[[ "$(grep -Fc -- '--root-fd "$CAPTURE_ROOT_FD"' "$CAPTURE_SCRIPT")" -eq 2 ]] ||
    probe_fail "CAPTURE_ROOT_FD_NOT_REUSED_BY_WORKER_AND_CLEANUP"
grep -Fq 'PAGE6_CAPTURE_ROOT_DEVICE' "$CAPTURE_SCRIPT" ||
    probe_fail "CAPTURE_ROOT_DEVICE_BINDING_MISSING"
grep -Fq 'PAGE6_CAPTURE_ROOT_INODE' "$CAPTURE_SCRIPT" ||
    probe_fail "CAPTURE_ROOT_INODE_BINDING_MISSING"
probe_pass "CAPTURE_ROOT_FD_SWAP_OK"

readonly TRAP_TARGET="${PROBE_DIR}/trap-target"
: > "$TRAP_TARGET"
ln -s -- "$TRAP_TARGET" "${LOG_ROOT}/${SYMLINK_ID}.jsonl"
if printf '%s\n' '{"message":"safe"}' | python3 "$REDACTOR"     --output-dir "$LOG_ROOT"     --container-id "$SYMLINK_ID"     --service "page6-test" --stream APPLICATION     >/dev/null 2> "${PROBE_DIR}/symlink.err"; then
    probe_fail "REDACTION_OUTPUT_SYMLINK_ACCEPTED"
fi
[[ ! -s "$TRAP_TARGET" ]] || probe_fail "REDACTION_SYMLINK_TARGET_WRITTEN"
[[ "$(tr -d '\r\n' < "${PROBE_DIR}/symlink.err")" ==     "REDACTOR_OUTPUT_PATH_UNSAFE" ]] ||     probe_fail "REDACTION_ERROR_NOT_STABLE"
unlink -- "${LOG_ROOT}/${SYMLINK_ID}.jsonl"

readonly HARDLINK_TARGET="${PROBE_DIR}/hardlink-target"
: > "$HARDLINK_TARGET"
chmod 600 -- "$HARDLINK_TARGET"
ln -- "$HARDLINK_TARGET" "${LOG_ROOT}/${HARDLINK_ID}.jsonl"
if printf '%s\n' '{"message":"safe"}' | python3 "$REDACTOR" \
    --output-dir "$LOG_ROOT" \
    --container-id "$HARDLINK_ID" \
    --service "page6-test" --stream APPLICATION \
    >/dev/null 2> "${PROBE_DIR}/hardlink.err"; then
    probe_fail "REDACTION_OUTPUT_HARDLINK_ACCEPTED"
fi
[[ ! -s "$HARDLINK_TARGET" ]] ||
    probe_fail "REDACTION_HARDLINK_TARGET_WRITTEN"
grep -Fxq "REDACTOR_OUTPUT_PATH_UNSAFE" "${PROBE_DIR}/hardlink.err" ||
    probe_fail "REDACTION_HARDLINK_ERROR_NOT_STABLE"
unlink -- "${LOG_ROOT}/${HARDLINK_ID}.jsonl"

readonly MODE_FILE="${LOG_ROOT}/${MODE_ID}.jsonl"
: > "$MODE_FILE"
chmod 0644 -- "$MODE_FILE"
if printf '%s\n' '{"message":"safe"}' | python3 "$REDACTOR" \
    --output-dir "$LOG_ROOT" \
    --container-id "$MODE_ID" \
    --service "page6-test" --stream APPLICATION \
    >/dev/null 2> "${PROBE_DIR}/mode.err"; then
    probe_fail "REDACTION_OUTPUT_MODE_ACCEPTED"
fi
grep -Fxq "REDACTOR_OUTPUT_PATH_UNSAFE" "${PROBE_DIR}/mode.err" ||
    probe_fail "REDACTION_MODE_ERROR_NOT_STABLE"
unlink -- "$MODE_FILE"

mkdir -m 700 -- "${OUTSIDE_TRAP}/ancestor-capture"
ln -s -- "$OUTSIDE_TRAP" "${PROBE_DIR}/ancestor-link"
if printf '%s\n' '{"event_code":"ANCESTOR_SYMLINK_TEST"}' |
    python3 "$REDACTOR" \
        --output-dir "${PROBE_DIR}/ancestor-link/ancestor-capture" \
        --container-id "$ANCESTOR_ID" \
        --service "page6-test" --stream APPLICATION \
        >/dev/null 2> "${PROBE_DIR}/ancestor-symlink.err"
then
    probe_fail "REDACTION_ANCESTOR_SYMLINK_ACCEPTED"
fi
grep -Fxq "REDACTOR_LOG_ROOT_SYMLINK" \
    "${PROBE_DIR}/ancestor-symlink.err" ||
    probe_fail "REDACTION_ANCESTOR_SYMLINK_ERROR_NOT_STABLE"
[[ ! -e "${OUTSIDE_TRAP}/ancestor-capture/${ANCESTOR_ID}.jsonl" ]] ||
    probe_fail "REDACTION_ANCESTOR_SYMLINK_WROTE_OUTSIDE"
unlink -- "${PROBE_DIR}/ancestor-link"

readonly OLD_FILE="${LOG_ROOT}/${OLD_ID}.jsonl.1"
: > "$OLD_FILE"
chmod 600 -- "$OLD_FILE"
touch -d '11 days ago' -- "$OLD_FILE"
python3 "$REDACTOR" --output-dir "$LOG_ROOT" --cleanup-only
[[ ! -e "$OLD_FILE" ]] || probe_fail "REDACTION_RETENTION_FAILED"

python3 - "$REDACTOR" "$LOG_ROOT" "$OUTSIDE_TRAP" <<'PY'
import importlib.util
import json
import multiprocessing
import os
import pathlib
import sys
import time

module_path = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2])
outside_root = pathlib.Path(sys.argv[3])
spec = importlib.util.spec_from_file_location("page6_redactor", module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
module.ROTATE_BYTES = 900

race_ancestor = root.parent / "race-ancestor"
race_capture = race_ancestor / "capture"
race_capture.mkdir(parents=True, mode=0o700)
outside_capture = outside_root / "capture"
outside_capture.mkdir(mode=0o700)
validated_race_root = module.validate_output_root(str(race_capture))
moved_ancestor = root.parent / "race-ancestor.original"
race_output = outside_capture / (("6" * 64) + ".jsonl")
original_root_open = module.os.open
root_race_fired = False

def racing_root_open(path_value, flags, mode=0o777, *, dir_fd=None):
    global root_race_fired
    if not root_race_fired and path_value == "race-ancestor":
        root_race_fired = True
        os.rename(race_ancestor, moved_ancestor)
        os.symlink(outside_root, race_ancestor, target_is_directory=True)
    return original_root_open(path_value, flags, mode, dir_fd=dir_fd)

module.os.open = racing_root_open
try:
    record = module.synthetic_record(
        "page6-test", "6" * 64, "APPLICATION", "ROOT_ANCESTOR_RACE_TEST"
    )
    try:
        module.write_record(
            validated_race_root / (("6" * 64) + ".jsonl"),
            record,
        )
    except module.RedactorFailure as exc:
        if str(exc) != "REDACTOR_LOG_ROOT_SYMLINK":
            raise
    else:
        raise SystemExit("REDACTION_ROOT_ANCESTOR_RACE_ACCEPTED")
finally:
    module.os.open = original_root_open
    if race_ancestor.is_symlink():
        race_ancestor.unlink()
    if moved_ancestor.exists():
        os.rename(moved_ancestor, race_ancestor)
if not root_race_fired:
    raise SystemExit("REDACTION_ROOT_ANCESTOR_RACE_NOT_EXERCISED")
if race_output.exists():
    raise SystemExit("REDACTION_ROOT_ANCESTOR_RACE_WROTE_OUTSIDE")

container_id = "d" * 64
path = root / f"{container_id}.jsonl"
for sequence in range(1, 13):
    record = module.synthetic_record("page6-test", container_id, "APPLICATION", "ROTATION_TEST")
    record["record"] = {"sequence": sequence, "padding": "x" * 100}
    module.write_record(path, record)

seen = []
candidates = sorted(root.glob(f"{container_id}.jsonl*"))
if len(candidates) > 5:
    raise SystemExit("REDACTION_ROTATION_FILE_LIMIT_EXCEEDED")
for candidate in candidates:
    if candidate.is_symlink():
        raise SystemExit("REDACTION_ROTATION_SYMLINK")
    for line in candidate.read_text(encoding="utf-8").splitlines():
        seen.append(json.loads(line)["record"]["sequence"])
ordered = sorted(seen)
if (
    not ordered
    or ordered[-1] != 12
    or ordered != list(range(ordered[0], 13))
    or len(ordered) != len(set(ordered))
):
    raise SystemExit("REDACTION_ROTATION_RETAINED_WINDOW_INVALID")

inode_id = "e" * 64
inode_path = root / f"{inode_id}.jsonl"
first = module.synthetic_record("page6-test", inode_id, "APPLICATION", "INODE_TEST")
first["record"] = {"sequence": 1}
inode_root_fd = module.open_output_root(root)
try:
    module.write_record(inode_path, first, inode_root_fd)
    reader = inode_path.open("r", encoding="utf-8")
    module.fcntl.flock(inode_root_fd, module.fcntl.LOCK_EX)
    try:
        module.rotate_entry(inode_root_fd, inode_path.name)
    finally:
        module.fcntl.flock(inode_root_fd, module.fcntl.LOCK_UN)
    second = module.synthetic_record("page6-test", inode_id, "APPLICATION", "INODE_TEST")
    second["record"] = {"sequence": 2}
    module.write_record(inode_path, second, inode_root_fd)
finally:
    os.close(inode_root_fd)
old_sequences = [json.loads(line)["record"]["sequence"] for line in reader]
reader.close()
new_sequences = [
    json.loads(line)["record"]["sequence"]
    for line in inode_path.read_text(encoding="utf-8").splitlines()
]
if old_sequences + new_sequences != [1, 2]:
    raise SystemExit("REDACTION_RENAME_INODE_CONTINUITY_FAILED")

owner_id = "2" * 64
owner_path = root / f"{owner_id}.jsonl"
owner_path.write_text("", encoding="utf-8")
owner_path.chmod(0o600)
owner_info = owner_path.stat()
original_geteuid = module.os.geteuid
module.os.geteuid = lambda: original_geteuid() + 1
try:
    try:
        module.validate_output_info(owner_info)
    except module.RedactorFailure as exc:
        if str(exc) != "REDACTOR_OUTPUT_PATH_UNSAFE":
            raise
    else:
        raise SystemExit("REDACTION_OUTPUT_OWNER_ACCEPTED")
finally:
    module.os.geteuid = original_geteuid
owner_path.unlink()

race_id = "3" * 64
race_name = f"{race_id}.jsonl"
race_path = root / race_name
race_path.write_text("", encoding="utf-8")
race_path.chmod(0o600)
root_fd = module.open_output_root(root)
original_open = module.os.open
race_fired = False

def racing_open(path_value, flags, mode=0o777, *, dir_fd=None):
    global race_fired
    if (
        not race_fired
        and path_value == race_name
        and dir_fd == root_fd
        and not (flags & os.O_CREAT)
    ):
        race_fired = True
        os.replace(
            race_name,
            race_name + ".attacker",
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        replacement_fd = original_open(
            race_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=root_fd,
        )
        os.close(replacement_fd)
    return original_open(path_value, flags, mode, dir_fd=dir_fd)

module.os.open = racing_open
try:
    try:
        module.secure_open_entry(root_fd, race_name)
    except module.RedactorFailure as exc:
        if str(exc) != "REDACTOR_OUTPUT_INODE_CHANGED":
            raise
    else:
        raise SystemExit("REDACTION_INODE_SWAP_ACCEPTED")
finally:
    module.os.open = original_open
    os.close(root_fd)
for candidate in (race_path, root / (race_name + ".attacker")):
    candidate.unlink(missing_ok=True)

module.ROTATE_BYTES = 4096
concurrent_id = "4" * 64
concurrent_path = root / f"{concurrent_id}.jsonl"

def concurrent_writer(worker_id):
    for sequence in range(30):
        record = module.synthetic_record(
            "page6-test", concurrent_id, "APPLICATION", "CONCURRENT_TEST"
        )
        record["record"] = {
            "worker": worker_id,
            "sequence": sequence,
            "padding": "x" * 96,
        }
        module.write_record(concurrent_path, record)

def concurrent_cleanup():
    for _ in range(20):
        module.cleanup_old_files(root, time.time())

context = multiprocessing.get_context("fork")
processes = [
    context.Process(target=concurrent_writer, args=(worker_id,))
    for worker_id in range(4)
]
processes.append(context.Process(target=concurrent_cleanup))
for process in processes:
    process.start()
for process in processes:
    process.join(15)
    if process.exitcode != 0:
        raise SystemExit("REDACTION_CONCURRENT_PROCESS_FAILED")

observed = set()
for candidate in sorted(root.glob(f"{concurrent_id}.jsonl*")):
    info = candidate.stat()
    if candidate.is_symlink() or (info.st_mode & 0o777) != 0o600:
        raise SystemExit("REDACTION_CONCURRENT_FILE_UNSAFE")
    for line in candidate.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)["record"]
        key = (payload["worker"], payload["sequence"])
        if key in observed:
            raise SystemExit("REDACTION_CONCURRENT_DUPLICATE")
        observed.add(key)
if not observed:
    raise SystemExit("REDACTION_CONCURRENT_OUTPUT_EMPTY")

cleanup_id = "5" * 64
cleanup_path = root / f"{cleanup_id}.jsonl.1"
cleanup_path.write_text("", encoding="utf-8")
cleanup_path.chmod(0o600)
old_time = time.time() - module.RETENTION_SECONDS - 60
os.utime(cleanup_path, (old_time, old_time))
module.cleanup_old_files(root, time.time())
module.cleanup_old_files(root, time.time())
if cleanup_path.exists():

    raise SystemExit("REDACTION_CLEANUP_NOT_IDEMPOTENT")
PY

CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$LOG_ROOT" \
    "$CAPTURE_SCRIPT" --self-test > "${PROBE_DIR}/capture-self-test.out" \
    2> "${PROBE_DIR}/capture-self-test.err"
grep -Fxq "CAPTURE_SELF_TEST_OK" "${PROBE_DIR}/capture-self-test.out" ||
    probe_fail "CAPTURE_SELF_TEST_FAILED"
probe_pass "CAPTURE_SELF_TEST_OK"


readonly PYTHON_POISON_DIR="${PROBE_DIR}/python-poison"
readonly PYTHON_POISON_MARKER="${PROBE_DIR}/python-poison-executed"
readonly PYTHON_POISON_ID="$(printf '7%.0s' {1..64})"
mkdir -m 700 -- "$PYTHON_POISON_DIR"
printf '%s\n' 'import os' 'open(os.environ["PAGE6_PYTHON_POISON_MARKER"], "w", encoding="utf-8").write("executed")' > "${PYTHON_POISON_DIR}/sitecustomize.py"
chmod 600 -- "${PYTHON_POISON_DIR}/sitecustomize.py"
PYTHONPATH="$PYTHON_POISON_DIR" \
    PAGE6_PYTHON_POISON_MARKER="$PYTHON_POISON_MARKER" \
    CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$LOG_ROOT" \
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    "$CAPTURE_SCRIPT" --check > "${PROBE_DIR}/python-env-check.out"
grep -Fxq "CAPTURE_CONFIGURATION_OK" "${PROBE_DIR}/python-env-check.out" ||
    probe_fail "PYTHON_ENVIRONMENT_CAPTURE_CHECK_FAILED"
[[ ! -e "$PYTHON_POISON_MARKER" ]] ||
    probe_fail "PYTHON_ENVIRONMENT_CAPTURE_POISON_EXECUTED"
printf '%s\n' '{"message":"safe"}' | PYTHONPATH="$PYTHON_POISON_DIR" PAGE6_PYTHON_POISON_MARKER="$PYTHON_POISON_MARKER" /usr/bin/python3 -E -s -B "$REDACTOR" --output-dir "$LOG_ROOT" --container-id "$PYTHON_POISON_ID" --service "page6-test" --stream APPLICATION
[[ ! -e "$PYTHON_POISON_MARKER" ]] ||
    probe_fail "PYTHON_ENVIRONMENT_REDACTOR_POISON_EXECUTED"
if find "$PYTHON_POISON_DIR" -xdev -type f -name '*.pyc' -print -quit | grep -q .; then
    probe_fail "PYTHON_ENVIRONMENT_BYTECODE_CREATED"
fi
if find "$PYTHON_POISON_DIR" -xdev -type d -name '__pycache__' -print -quit | grep -q .; then
    probe_fail "PYTHON_ENVIRONMENT_CACHE_DIRECTORY_CREATED"
fi
probe_pass "PYTHON_ENVIRONMENT_ISOLATION_OK"
readonly LOCK_TRAP="${OUTSIDE_TRAP}/manager-lock-target"
: > "$LOCK_TRAP"
chmod 600 -- "$LOCK_TRAP"
ln -s -- "$LOCK_TRAP" "${LOG_ROOT}/.capture-manager.lock"
if /usr/bin/env \
    -u PAGE6_CAPTURE_SECURE_LAUNCHED \
    -u PAGE6_CAPTURE_ROOT_FD \
    -u PAGE6_CAPTURE_LOCK_FD \
    CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$LOG_ROOT" \
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    /usr/bin/bash "$CAPTURE_SCRIPT" \
    >/dev/null 2> "${PROBE_DIR}/manager-lock-symlink.err"
then
    probe_fail "CAPTURE_MANAGER_LOCK_SYMLINK_ACCEPTED"
fi
grep -Fxq "CAPTURE_MANAGER_LOCK_UNSAFE" \
    "${PROBE_DIR}/manager-lock-symlink.err" ||
    probe_fail "CAPTURE_MANAGER_LOCK_SYMLINK_ERROR_INVALID"
[[ ! -s "$LOCK_TRAP" ]] ||
    probe_fail "CAPTURE_MANAGER_LOCK_SYMLINK_TARGET_WRITTEN"
unlink -- "${LOG_ROOT}/.capture-manager.lock"

ln -- "$LOCK_TRAP" "${LOG_ROOT}/.capture-manager.lock"
if /usr/bin/env \
    -u PAGE6_CAPTURE_SECURE_LAUNCHED \
    -u PAGE6_CAPTURE_ROOT_FD \
    -u PAGE6_CAPTURE_LOCK_FD \
    CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$LOG_ROOT" \
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    /usr/bin/bash "$CAPTURE_SCRIPT" \
    >/dev/null 2> "${PROBE_DIR}/manager-lock-hardlink.err"
then
    probe_fail "CAPTURE_MANAGER_LOCK_HARDLINK_ACCEPTED"
fi
grep -Fxq "CAPTURE_MANAGER_LOCK_UNSAFE" \
    "${PROBE_DIR}/manager-lock-hardlink.err" ||
    probe_fail "CAPTURE_MANAGER_LOCK_HARDLINK_ERROR_INVALID"
[[ ! -s "$LOCK_TRAP" ]] ||
    probe_fail "CAPTURE_MANAGER_LOCK_HARDLINK_TARGET_WRITTEN"
unlink -- "${LOG_ROOT}/.capture-manager.lock"

wait_for_manager_lock() {
    local pid="$1"
    local attempt
    for attempt in {1..40}; do
        kill -0 "$pid" 2>/dev/null ||
            probe_fail "CAPTURE_MANAGER_EXITED_EARLY"
        if [[ -f "${LOG_ROOT}/.capture-manager.lock" ]] &&
            ! /usr/bin/flock --nonblock "${LOG_ROOT}/.capture-manager.lock" true
        then
            return 0
        fi
        sleep 0.05
    done
    probe_fail "CAPTURE_MANAGER_LOCK_TIMEOUT"
}

run_manager_signal_test() {
    local signal="$1"
    local expected_status="$2"
    local manager_pid
    local status
    CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
    CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$LOG_ROOT" \
        /usr/bin/python3 -c \
        'import os, signal, sys; os.setsid(); signal.signal(signal.SIGINT, signal.SIG_DFL); signal.signal(signal.SIGTERM, signal.SIG_DFL); os.execv("/usr/bin/bash", ["bash", sys.argv[1]])' \
        "$CAPTURE_SCRIPT" \
        > "${PROBE_DIR}/manager-${signal}.out" \
        2> "${PROBE_DIR}/manager-${signal}.err" &
    manager_pid=$!
    wait_for_manager_lock "$manager_pid"

    if [[ "$signal" == "TERM" ]]; then
        CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT="$LOG_ROOT" \
            CONTRACT_REVIEW_DEV_CAPTURE_SELECTOR_FILE="$CAPTURE_SELECTOR_FILE" \
            /usr/bin/bash "$CAPTURE_SCRIPT" \
            > "${PROBE_DIR}/manager-second.out" \
            2> "${PROBE_DIR}/manager-second.err" &
        local second_pid=$!
        set +e
        wait "$second_pid"
        status=$?
        set -e
        [[ "$status" -eq 73 ]] ||
            probe_fail "CAPTURE_SINGLETON_EXIT_STATUS_INVALID"
        grep -Fxq "CAPTURE_MANAGER_ALREADY_RUNNING" \
            "${PROBE_DIR}/manager-second.err" ||
            probe_fail "CAPTURE_SINGLETON_ERROR_INVALID"
    fi

    kill "-$signal" -- "-$manager_pid"
    set +e
    wait "$manager_pid"
    status=$?
    set -e
    [[ "$status" -eq "$expected_status" ]] ||
        probe_fail "CAPTURE_SIGNAL_EXIT_STATUS_INVALID"
    if pgrep -f -- "${PROBE_DIR}" >/dev/null 2>&1; then
        probe_fail "CAPTURE_SIGNAL_PROCESS_RESIDUE"
    fi
}

run_manager_signal_test TERM 143
run_manager_signal_test INT 130

for schema_field in tenant_id request_id trace_id task_id run_id scope_type; do
    if grep -Eq "^[[:space:]]*${schema_field}[[:space:]]*=[[:space:]]*\\\"\\\"" \
        "$ALLOY_CONFIG"; then
        probe_fail "REDACTION_HIGH_CARDINALITY_LABEL_PRESENT"
    fi
done

grep -Fq '"__path__" = "/var/log/contract-review-code-dev/*.jsonl"'     "$ALLOY_CONFIG" || probe_fail "REDACTION_ALLOY_ACTIVE_GLOB_INVALID"
if grep -Fq '*.jsonl*' "$ALLOY_CONFIG"; then
    probe_fail "REDACTION_ALLOY_ROTATION_GLOB_DUPLICATES"
fi
grep -Fq 'tail_from_end = false' "$ALLOY_CONFIG" ||     probe_fail "REDACTION_ALLOY_INITIAL_POSITION_INVALID"
grep -Fq 'stream      = "stream"' "$ALLOY_CONFIG" ||     probe_fail "REDACTION_ALLOY_STREAM_PARSE_MISSING"
grep -Fq 'stream      = ""' "$ALLOY_CONFIG" ||     probe_fail "REDACTION_ALLOY_STREAM_LABEL_MISSING"
grep -Fq 'alloy-data:/var/lib/alloy/data' "$OBS_COMPOSE" ||     probe_fail "REDACTION_ALLOY_POSITIONS_NOT_PERSISTENT"

probe_pass "REDACTION_VALIDATION_OK"
