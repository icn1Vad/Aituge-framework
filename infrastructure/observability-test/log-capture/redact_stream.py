#!/usr/bin/env python3
"""Fail-safe streaming redactor for test-only Docker log capture."""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any

SECURITY_ANCHOR_PARENT = Path("/home")
SECURITY_ANCHOR = Path("/home/aituge")
ALLOWED_ROOT = Path("/home/aituge/contract-review-code-dev-test-private/observability-logs")
PROBE_ROOT_RE = re.compile(r"^page6-probe\.[A-Za-z0-9]{6}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
FILE_RE = re.compile(r"^[0-9a-f]{64}\.jsonl(?:\.[1-4])?$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DOCKER_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s+(.*)$", re.S)
JWT_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])")
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
SK_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}")
QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:cursor|locator|retry(?:[_-]?token|Token)|"
    r"access(?:[_-]?(?:context|session|reason|token)|Context|Session|Reason|Token)(?:[_-]?id|Id)?|"
    r"refresh(?:[_-]?token|Token)|high(?:[_-]?watermark|Watermark)|"
    r"query(?:[_-]?snapshot[_-]?id|SnapshotId)|point(?:[_-]?in[_-]?time[_-]?token|InTimeToken)|"
    r"idempotency(?:[_-]?key|Key)|stream(?:[_-]?token|Token)|"
    r"download(?:[_-]?token|Token)|observability[_-]?scope|"
    r"api(?:[_-]?key|Key)|token|secret|password)=)[^&#\s]*"
)
KV_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|cursor|locator|retry(?:[._-]?token|Token)|"
    r"access(?:[._-]?(?:context|session|reason|token)|Context|Session|Reason|Token)(?:[._-]?id|Id)?|"
    r"refresh(?:[._-]?token|Token)|high(?:[._-]?watermark|Watermark)|"
    r"query(?:[._-]?snapshot[._-]?id|SnapshotId)|point(?:[._-]?in[._-]?time[._-]?token|InTimeToken)|"
    r"idempotency(?:[._-]?key|Key)|stream(?:[._-]?token|Token)|"
    r"download(?:[._-]?token|Token)|observability[._-]?scope|"
    r"(?:db|database|mysql|dashscope)?[._-]?(?:password|passwd|secret)|"
    r"api(?:[._-]?key|Key|[._-]?secret)|secret[._-]?key|credential|client[._-]?secret|"
    r"exception[._-]?(?:message|stack|stacktrace)|raw[._-]?exception|"
    r"client[._-]?(?:ip|address)|source[._-]?(?:ip|address)|"
    r"remote[._-]?(?:ip|address)|peer[._-]?(?:ip|address)|"
    r"network[._-]?peer[._-]?address|x-forwarded-for|x-real-ip|forwarded)\s*"
    r"([:=])\s*([^\s,;]+)"
)

MAX_INPUT_BYTES = 256 * 1024
MAX_TEXT_CHARS = 4096
MAX_JSON_DEPTH = 12
MAX_OUTPUT_BYTES = 16 * 1024
ROTATE_BYTES = 20 * 1024 * 1024
BACKUP_COUNT = 4
RETENTION_SECONDS = 10 * 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 300
REDACTED = "[REDACTED]"

SENSITIVE_NORMALIZED = {
    "authorization", "cookie", "setcookie", "cursor", "retrytoken",
    "locator", "accesscontext", "accesscontextid", "highwatermark",
    "apikey", "credential", "credentials", "secret", "secretkey",
    "clientsecret", "password", "passwd", "privatekey", "accesstoken",
    "refreshtoken", "idtoken", "streamtoken", "downloadtoken",
    "accesssession", "accesssessionid", "accessreason", "querysnapshotid",
    "pointintimetoken", "idempotencykey", "observabilityscope",
    "xobservabilityscope", "xobservabilityaccesscontext",
    "xobservabilityaccesssession", "xobservabilityaccesssessionid",
    "xobservabilityaccessreason", "xobservabilityhighwatermark",
}
SENSITIVE_FRAGMENTS = (
    "password", "passwd", "secret", "apikey", "credential",
)
CONTENT_FRAGMENTS = (
    "prompt", "completion", "modelresponse", "modeloutput", "userinput",
    "contracttext", "contractcontent", "documenttext", "requestbody",
    "responsebody", "rawbody", "rawexception",
)
EXCEPTION_PATHS = {
    "exceptionmessage", "exceptionstack", "exceptionstacktrace",
    "rawexception", "rawexceptionmessage", "rawexceptionstacktrace",
}
RAW_ADDRESS_PATHS = {
    "clientaddress", "clientip", "sourceaddress", "sourceip",
    "remoteaddress", "remoteip", "peeraddress", "peerip",
    "networkpeeraddress", "httpclientip", "xforwardedfor", "xrealip",
    "forwarded",
}
SAFE_TOKEN_COUNTERS = {"inputtokens", "outputtokens", "totaltokens", "tokencount"}
SCHEMA_FIELD_ALIASES = {
    "tenantid": "tenant_id",
    "requestid": "request_id",
    "traceid": "trace_id",
    "taskid": "task_id",
    "runid": "run_id",
    "scopetype": "scope_type",
}
SCHEMA_ID_RE = re.compile(
    r"^(?:[0-9]{1,20}|[0-9a-f]{16}|[0-9a-f]{32}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
SAFE_LEVELS = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}
SAFE_STREAMS = {"APPLICATION", "PLATFORM"}
SAFE_LOGGER_RE = re.compile(
    r"^(?:(?:page6|application|root|observability|aituge|contract_review|"
    r"services|backend|runtime)(?:\.[A-Za-z0-9_$-]{1,64}){0,7}|"
    r"(?:top|cn|com|org|io|ai)(?:\.[A-Za-z0-9_$-]{1,64}){1,8}|__main__)$"
)
SAFE_CODE_RE = re.compile(
    r"^(?:PAGE6|LOG|REDACTOR|CAPTURE|CONTRACT|MODEL|TASK|PROOF|AGENT|"
    r"HTTP|SYSTEM|ROOT|ROTATION|INODE|CONCURRENT|PYTHON|JAVA|SECURITY|"
    r"OBSERVABILITY)_[A-Z0-9_]{1,96}$"
)
SAFE_EXCEPTION_TYPE_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_$]{0,63}\.){0,8}"
    r"[A-Z][A-Za-z0-9_$]{0,63}(?:Error|Exception|Failure)$"
)
SAFE_MODEL_RE = re.compile(
    r"^(?:deepseek|qwen|gpt|text-embedding|bge|rerank|local|vllm|"
    r"ollama)[a-z0-9._/-]{0,96}$"
)
SAFE_MODEL_PACK_RE = re.compile(r"^(?:pack[_-])?[a-z0-9][a-z0-9._-]{0,95}$")
SAFE_SEMVER_RE = re.compile(r"^[0-9]{1,5}(?:\.[0-9]{1,5}){1,3}(?:-[a-z0-9.-]{1,32})?$")

# Key names are data too. Unknown keys are dropped instead of being copied with
# a redacted value because a producer can otherwise encode a secret in the key.
ALLOWED_KEY_PATHS = {
    "level",
    "logger",
    "tenantid",
    "requestid",
    "traceid",
    "taskid",
    "runid",
    "scopetype",
    "eventcode",
    "displaycode",
    "errorcode",
    "errortype",
    "exceptiontype",
    "operation",
    "outcome",
    "status",
    "provider",
    "modelname",
    "modelpackid",
    "modelpackversion",
    "privacymode",
    "routetype",
    "featurecode",
    "stagecode",
    "httprequestmethod",
    "inputtokens",
    "outputtokens",
    "totaltokens",
    "tokencount",
    "attempt",
    "attemptno",
    "retrycount",
    "sequence",
    "durationms",
    "latencyms",
    "ttftms",
    "elapsedms",
    "httpresponsestatuscode",
    "costamount",
    "successrate",
    "retryrate",
    "retryable",
    "crosstenant",
    "sampled",
    "cached",
    "complete",
    "hasmore",
    "success",
    "usage",
    "usage.inputtokens",
    "usage.outputtokens",
    "usage.totaltokens",
    "metrics",
    "metrics.durationms",
    "metrics.latencyms",
    "metrics.ttftms",
    "metrics.costamount",
    "exception",
    "exception.type",
    "exception.exceptiontype",
}
SAFE_ENUM_VALUES = {
    "operation": {
        "CREATE", "READ", "UPDATE", "DELETE", "SEARCH", "EXPORT", "DOWNLOAD",
        "DISPATCH", "CALLBACK", "RETRY", "CANCEL", "RESOLVE_PARTIES",
        "CONTRACT_REVIEW", "MODEL_INVOKE", "PAGE6_PROBE",
    },
    "outcome": {
        "SUCCESS", "FAILURE", "UNKNOWN", "ABANDONED", "REJECTED",
    },
    "status": {
        "CREATED", "PENDING", "PENDING_DISPATCH", "DISPATCHING", "RUNNING",
        "SUCCEEDED", "FAILED", "CANCELLED", "RETRYING", "TERMINAL",
        "FIRING", "RESOLVED", "HEALTHY", "UNHEALTHY",
    },
    "provider": {
        "dashscope", "deepseek", "openai", "local", "vllm", "ollama",
    },
    "privacymode": {"STANDARD", "PRIVATE"},
    "routetype": {"LOCAL", "EXTERNAL"},
    "httprequestmethod": {
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
    },
}

MAX_SAFE_INTEGER = (1 << 63) - 1
SAFE_INTEGER_RANGES = {
    "inputtokens": (0, MAX_SAFE_INTEGER),
    "outputtokens": (0, MAX_SAFE_INTEGER),
    "totaltokens": (0, MAX_SAFE_INTEGER),
    "tokencount": (0, MAX_SAFE_INTEGER),
    "usage.inputtokens": (0, MAX_SAFE_INTEGER),
    "usage.outputtokens": (0, MAX_SAFE_INTEGER),
    "usage.totaltokens": (0, MAX_SAFE_INTEGER),
    "attempt": (0, 1_000_000),
    "attemptno": (0, 1_000_000),
    "retrycount": (0, 1_000_000),
    "sequence": (0, MAX_SAFE_INTEGER),
    "durationms": (0, MAX_SAFE_INTEGER),
    "latencyms": (0, MAX_SAFE_INTEGER),
    "ttftms": (0, MAX_SAFE_INTEGER),
    "elapsedms": (0, MAX_SAFE_INTEGER),
    "metrics.durationms": (0, MAX_SAFE_INTEGER),
    "metrics.latencyms": (0, MAX_SAFE_INTEGER),
    "metrics.ttftms": (0, MAX_SAFE_INTEGER),
    "httpresponsestatuscode": (100, 599),
    "http.statuscode": (100, 599),
}
SAFE_NUMBER_RANGES = {
    "costamount": (0.0, 1_000_000_000.0),
    "metrics.costamount": (0.0, 1_000_000_000.0),
    "successrate": (0.0, 1.0),
    "retryrate": (0.0, 1.0),
}
SAFE_BOOLEAN_PATHS = {
    "retryable",
    "crosstenant",
    "sampled",
    "cached",
    "complete",
    "hasmore",
    "success",
}
SAFE_NULLABLE_PATHS = {"hasmore"}


def normalized_field_path(path: tuple[str, ...]) -> str:
    return ".".join(
        "[]" if part.isdigit() else normalized_key(part)
        for part in path
    )


def sanitize_scalar(value: Any, path: tuple[str, ...]) -> Any:
    field_path = normalized_field_path(path)
    if isinstance(value, bool):
        return value if field_path in SAFE_BOOLEAN_PATHS else REDACTED
    if value is None:
        return None if field_path in SAFE_NULLABLE_PATHS else REDACTED
    if isinstance(value, int):
        bounds = SAFE_INTEGER_RANGES.get(field_path)
        return (
            value
            if bounds is not None and bounds[0] <= value <= bounds[1]
            else REDACTED
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RecordRejected("LOG_RECORD_DROPPED_NON_FINITE_NUMBER")
        bounds = SAFE_NUMBER_RANGES.get(field_path)
        return (
            value
            if bounds is not None and bounds[0] <= value <= bounds[1]
            else REDACTED
        )
    return REDACTED


class RedactorFailure(RuntimeError):
    pass


class RecordRejected(RuntimeError):

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def stable_error(code: str) -> None:
    try:
        os.write(2, (code + "\n").encode("ascii", "strict"))
    except OSError:
        pass


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def sensitive_key(value: str, path: tuple[str, ...] = ()) -> bool:
    normalized = normalized_key(value)
    full_path = normalized_key(".".join((*path, value)))
    if normalized in SAFE_TOKEN_COUNTERS:
        return False
    if normalized in SENSITIVE_NORMALIZED:
        return True
    if normalized.endswith("token") and normalized not in SAFE_TOKEN_COUNTERS:
        return True
    if any(fragment in normalized for fragment in SENSITIVE_FRAGMENTS):
        return True
    if any(fragment in normalized or fragment in full_path for fragment in CONTENT_FRAGMENTS):
        return True
    if any(full_path.endswith(item) for item in EXCEPTION_PATHS):
        return True
    return any(
        normalized == item or full_path.endswith(item)
        for item in RAW_ADDRESS_PATHS
    )


def scrub_text(value: str) -> str:
    value = ANSI_RE.sub("", value)
    value = CONTROL_RE.sub("", value)
    value = BEARER_RE.sub("Bearer " + REDACTED, value)
    value = JWT_RE.sub(REDACTED, value)
    value = SK_RE.sub(REDACTED, value)
    value = QUERY_SECRET_RE.sub(lambda match: match.group(1) + REDACTED, value)
    value = KV_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", value
    )
    return value[:MAX_TEXT_CHARS]


def sanitize_structured_text(value: str, path: tuple[str, ...]) -> str:
    if not path:
        return REDACTED
    key = normalized_key(path[-1])
    field_path = normalized_field_path(path)
    canonical = SCHEMA_FIELD_ALIASES.get(key)
    if canonical is not None:
        valid = (
            value in {"TENANT", "SYSTEM"}
            if canonical == "scope_type"
            else SCHEMA_ID_RE.fullmatch(value) is not None
        )
        if not valid:
            raise RecordRejected("LOG_RECORD_DROPPED_SCHEMA_FIELD_INVALID")
        return value
    if key == "level":
        normalized = value.upper()
        return normalized if normalized in SAFE_LEVELS else REDACTED
    if key == "logger":
        return value if SAFE_LOGGER_RE.fullmatch(value) else REDACTED
    if key in {
        "eventcode", "displaycode", "errorcode", "errortype",
        "featurecode", "stagecode",
    }:
        return value if SAFE_CODE_RE.fullmatch(value) else REDACTED
    if (
        field_path in {"exception.type", "exception.exceptiontype"}
        or key == "exceptiontype"
    ):
        return value if SAFE_EXCEPTION_TYPE_RE.fullmatch(value) else REDACTED
    allowed = SAFE_ENUM_VALUES.get(key)
    if allowed is not None:
        return value if value in allowed else REDACTED
    if key == "modelname":
        return value if SAFE_MODEL_RE.fullmatch(value) else REDACTED
    if key == "modelpackid":
        return value if SAFE_MODEL_PACK_RE.fullmatch(value) else REDACTED
    if key == "modelpackversion":
        return value if SAFE_SEMVER_RE.fullmatch(value) else REDACTED
    # Arbitrary strings may be contract text, prompts, model responses, or user
    # input even when their producer used an unexpected key. Never retain them.
    return REDACTED


def sanitize(
    value: Any,
    depth: int = 0,
    path: tuple[str, ...] = (),
) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise RecordRejected("LOG_RECORD_DROPPED_TOO_DEEP")
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= 100:
                break
            key = str(raw_key)
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key):
                continue
            field_path = normalized_field_path((*path, key))
            if field_path not in ALLOWED_KEY_PATHS:
                continue
            result[key] = (
                REDACTED
                if sensitive_key(key, path)
                else sanitize(raw_value, depth + 1, (*path, key))
            )
        return result
    if isinstance(value, list):
        return [
            sanitize(item, depth + 1, (*path, str(index)))
            for index, item in enumerate(value[:100])
        ]
    if isinstance(value, str):
        return sanitize_structured_text(scrub_text(value), path)
    if value is None or isinstance(value, (bool, int, float)):
        return sanitize_scalar(value, path)
    return REDACTED


def extract_schema_fields(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        canonical = SCHEMA_FIELD_ALIASES.get(normalized_key(str(raw_key)))
        if canonical is None:
            continue
        valid = (
            isinstance(raw_value, str)
            and (
                raw_value in {"TENANT", "SYSTEM"}
                if canonical == "scope_type"
                else SCHEMA_ID_RE.fullmatch(raw_value) is not None
            )
        )
        if not valid:
            raise RecordRejected("LOG_RECORD_DROPPED_SCHEMA_FIELD_INVALID")
        existing = result.get(canonical)
        if existing is not None and existing != raw_value:
            raise RecordRejected("LOG_RECORD_DROPPED_SCHEMA_FIELD_CONFLICT")
        result[canonical] = raw_value
    return result


def validate_output_root(raw_path: str) -> Path:
    root = Path(raw_path)
    if not root.is_absolute() or raw_path != str(root):
        raise RedactorFailure("REDACTOR_LOG_ROOT_INVALID")
    allowed_parts = ALLOWED_ROOT.parts
    relative_parts = root.parts[len(allowed_parts) :]
    if (
        root.parts[: len(allowed_parts)] != allowed_parts
        or not relative_parts
        or any(part in {"", ".", ".."} for part in root.parts[1:])
    ):
        raise RedactorFailure("REDACTOR_LOG_ROOT_OUTSIDE_TEST")
    is_deployment_root = relative_parts == ("redacted",)
    is_probe_root = (
        len(relative_parts) >= 2
        and PROBE_ROOT_RE.fullmatch(relative_parts[0]) is not None
    )
    if not is_deployment_root and not is_probe_root:
        raise RedactorFailure("REDACTOR_LOG_ROOT_OUTSIDE_TEST")
    return root


def expected_directory_identity(expected: Path) -> tuple[int, int, int]:
    if expected == SECURITY_ANCHOR_PARENT:
        return 0, 0, 0o755
    if expected == SECURITY_ANCHOR:
        return os.geteuid(), os.getegid(), 0o750
    private_root = ALLOWED_ROOT.parent
    if expected == private_root or expected == ALLOWED_ROOT:
        return os.geteuid(), os.getegid(), 0o700
    try:
        expected.relative_to(ALLOWED_ROOT)
    except ValueError as exc:
        raise RedactorFailure("REDACTOR_LOG_ROOT_OUTSIDE_TEST") from exc
    return os.geteuid(), os.getegid(), 0o700


def validate_directory_fd(root_fd: int, expected_path: Path) -> os.stat_result:
    expected_uid, expected_gid, expected_mode = expected_directory_identity(
        expected_path
    )
    info = os.fstat(root_fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != expected_mode
        or info.st_uid != expected_uid
        or info.st_gid != expected_gid
    ):
        raise RedactorFailure("REDACTOR_LOG_ROOT_PERMISSIONS")
    try:
        opened_path = os.readlink(f"/proc/self/fd/{root_fd}")
    except OSError as exc:
        raise RedactorFailure("REDACTOR_LOG_ROOT_CHANGED") from exc
    if opened_path != str(expected_path):
        raise RedactorFailure("REDACTOR_LOG_ROOT_CHANGED")
    return info


def validate_root_fd(root_fd: int, expected: os.stat_result | None = None) -> os.stat_result:
    info = os.fstat(root_fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
        or info.st_gid != os.getegid()
    ):
        raise RedactorFailure("REDACTOR_LOG_ROOT_PERMISSIONS")
    if expected is not None and (
        info.st_dev != expected.st_dev or info.st_ino != expected.st_ino
    ):
        raise RedactorFailure("REDACTOR_LOG_ROOT_CHANGED")
    return info


def adopt_output_root_fd(
    raw_fd: str | None,
    raw_device: str | None,
    raw_inode: str | None,
) -> int:
    values = (raw_fd, raw_device, raw_inode)
    if all(value is None for value in values):
        raise RedactorFailure("REDACTOR_ROOT_FD_ARGUMENT_MISSING")
    if any(value is None or re.fullmatch(r"[0-9]{1,32}", value) is None for value in values):
        raise RedactorFailure("REDACTOR_ROOT_FD_ARGUMENT_INVALID")
    assert raw_fd is not None and raw_device is not None and raw_inode is not None
    fd = int(raw_fd)
    expected_device = int(raw_device)
    expected_inode = int(raw_inode)
    if fd <= 2:
        raise RedactorFailure("REDACTOR_ROOT_FD_ARGUMENT_INVALID")
    try:
        info = validate_root_fd(fd)
    except OSError as exc:
        raise RedactorFailure("REDACTOR_ROOT_FD_INVALID") from exc
    if info.st_dev != expected_device or info.st_ino != expected_inode:
        raise RedactorFailure("REDACTOR_ROOT_FD_CHANGED")
    try:
        os.set_inheritable(fd, False)
    except OSError as exc:
        raise RedactorFailure("REDACTOR_ROOT_FD_INVALID") from exc
    return fd


def open_output_root(root: Path) -> int:
    root = validate_output_root(str(root))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise RedactorFailure("REDACTOR_NOFOLLOW_UNAVAILABLE")
    flags |= nofollow
    try:
        fd = os.open(SECURITY_ANCHOR_PARENT, flags)
    except OSError as exc:
        raise RedactorFailure("REDACTOR_LOG_ROOT_INVALID") from exc
    try:
        validate_directory_fd(fd, SECURITY_ANCHOR_PARENT)
        current = SECURITY_ANCHOR_PARENT
        for part in root.relative_to(SECURITY_ANCHOR_PARENT).parts:
            expected = current / part
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise RedactorFailure("REDACTOR_LOG_ROOT_SYMLINK") from exc
                if exc.errno == errno.ENOENT:
                    raise RedactorFailure("REDACTOR_LOG_ROOT_INVALID") from exc
                raise
            try:
                validate_directory_fd(next_fd, expected)
            except Exception:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
            current = expected
        validate_root_fd(fd)
        return fd
    except Exception:
        os.close(fd)
        raise


def validate_output_info(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        raise RedactorFailure("REDACTOR_OUTPUT_PATH_UNSAFE")


def entry_info(root_fd: int, name: str) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    validate_output_info(info)
    return info


def secure_open_entry(root_fd: int, name: str) -> int:
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _ in range(3):
        expected = entry_info(root_fd, name)
        try:
            if expected is None:
                fd = os.open(
                    name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=root_fd,
                )
                os.fchmod(fd, 0o600)
            else:
                fd = os.open(name, flags, dir_fd=root_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise RedactorFailure("REDACTOR_OUTPUT_PATH_UNSAFE") from exc
            raise
        try:
            opened = os.fstat(fd)
            validate_output_info(opened)
            if expected is not None and (
                opened.st_dev != expected.st_dev
                or opened.st_ino != expected.st_ino
            ):
                raise RedactorFailure("REDACTOR_OUTPUT_INODE_CHANGED")
            current = entry_info(root_fd, name)
            if current is None or (
                current.st_dev != opened.st_dev
                or current.st_ino != opened.st_ino
            ):
                raise RedactorFailure("REDACTOR_OUTPUT_INODE_CHANGED")
            return fd
        except Exception:
            os.close(fd)
            raise
    raise RedactorFailure("REDACTOR_OUTPUT_CREATE_RACE")


def cleanup_old_files(
    root: Path,
    now: float,
    root_fd: int | None = None,
) -> None:
    owned_fd = root_fd is None
    fd = open_output_root(root) if owned_fd else root_fd
    assert fd is not None
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        validate_root_fd(fd)
        cutoff = now - RETENTION_SECONDS
        for name in os.listdir(fd):
            if not FILE_RE.fullmatch(name):
                continue
            info = entry_info(fd, name)
            if info is not None and info.st_mtime < cutoff:
                os.unlink(name, dir_fd=fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        if owned_fd:
            os.close(fd)


def rotate_entry(root_fd: int, name: str) -> None:
    for index in range(BACKUP_COUNT, 0, -1):
        source = name if index == 1 else f"{name}.{index - 1}"
        destination = f"{name}.{index}"
        source_info = entry_info(root_fd, source)
        if source_info is None:
            continue
        destination_info = entry_info(root_fd, destination)
        if destination_info is not None:
            os.unlink(destination, dir_fd=root_fd)
        os.replace(
            source,
            destination,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def synthetic_record(
    service: str,
    container_id: str,
    stream: str,
    code: str,
) -> dict[str, Any]:
    return {
        "ingested_at": utc_now(),
        "environment": "test",
        "service": service,
        "container_id": container_id,
        "stream": stream,
        "level": "WARN",
        "logger": "observability.redactor",
        "event_code": code,
        "message": "source record removed by fail-safe redaction",
    }


def write_record(
    path: Path,
    record: dict[str, Any],
    root_fd: int | None = None,
) -> None:
    if re.fullmatch(r"[0-9a-f]{64}\.jsonl", path.name) is None:
        raise RedactorFailure("REDACTOR_OUTPUT_PATH_UNSAFE")
    try:
        payload = json.dumps(
            record, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        payload = json.dumps(
            synthetic_record(
                str(record.get("service", "unknown")),
                str(record.get("container_id", "0" * 64)),
                str(record.get("stream", "APPLICATION")),
                "LOG_RECORD_DROPPED_NON_FINITE_NUMBER",
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    if len(payload) > MAX_OUTPUT_BYTES:
        payload = json.dumps(
            synthetic_record(
                record["service"], record["container_id"],
                record["stream"],
                "LOG_RECORD_DROPPED_AFTER_REDACTION",
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"

    owned_fd = root_fd is None
    fd_root = (
        open_output_root(validate_output_root(str(path.parent)))
        if owned_fd
        else root_fd
    )
    assert fd_root is not None
    fcntl.flock(fd_root, fcntl.LOCK_EX)
    output_fd: int | None = None
    try:
        validate_root_fd(fd_root)
        current = entry_info(fd_root, path.name)
        if current is not None and current.st_size + len(payload) > ROTATE_BYTES:
            rotate_entry(fd_root, path.name)
        output_fd = secure_open_entry(fd_root, path.name)
        opened = os.fstat(output_fd)
        validate_output_info(opened)
        current = entry_info(fd_root, path.name)
        if current is None or (
            current.st_dev != opened.st_dev or current.st_ino != opened.st_ino
        ):
            raise RedactorFailure("REDACTOR_OUTPUT_INODE_CHANGED")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(output_fd, remaining)
            if written <= 0:
                raise RedactorFailure("REDACTOR_OUTPUT_WRITE_FAILED")
            remaining = remaining[written:]
        final = os.fstat(output_fd)
        validate_output_info(final)
        current = entry_info(fd_root, path.name)
        if current is None or (
            current.st_dev != final.st_dev or current.st_ino != final.st_ino
        ):
            raise RedactorFailure("REDACTOR_OUTPUT_INODE_CHANGED")
    finally:
        if output_fd is not None:
            os.close(output_fd)
        fcntl.flock(fd_root, fcntl.LOCK_UN)
        if owned_fd:
            os.close(fd_root)

def split_docker_timestamp(text: str) -> tuple[str | None, str]:
    match = DOCKER_TS_RE.match(text)
    if match is None:
        return None, text
    return match.group(1), match.group(2)


def transform_line(
    raw: bytes,
    service: str,
    container_id: str,
    stream: str,
) -> dict[str, Any] | None:
    decoded = raw.decode("utf-8", "replace").rstrip("\r\n")
    source_timestamp, payload = split_docker_timestamp(decoded)
    payload = ANSI_RE.sub("", payload)
    if not payload.strip():
        return None

    level = "INFO"
    logger = "application"
    schema_fields: dict[str, str] = {}
    if payload.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(
                payload,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number")
                ),
            )
            sanitized = sanitize(parsed)
            schema_fields = extract_schema_fields(sanitized)
        except RecursionError:
            return synthetic_record(
                service, container_id, stream, "LOG_RECORD_DROPPED_TOO_DEEP"
            )
        except RecordRejected as exc:
            return synthetic_record(service, container_id, stream, exc.code)
        except (json.JSONDecodeError, ValueError):
            return synthetic_record(
                service, container_id, stream, "LOG_RECORD_DROPPED_INVALID_JSON"
            )
        if isinstance(sanitized, dict):
            level_value = sanitized.get("level")
            logger_value = sanitized.get("logger")
            if level_value in SAFE_LEVELS:
                level = level_value
            if isinstance(logger_value, str) and SAFE_LOGGER_RE.fullmatch(logger_value):
                logger = logger_value
        record_field: tuple[str, Any] = ("record", sanitized)
    else:
        record = synthetic_record(
            service, container_id, stream, "LOG_RECORD_DROPPED_UNSTRUCTURED_TEXT"
        )
        return record

    record: dict[str, Any] = {
        "ingested_at": utc_now(),
        "environment": "test",
        "service": service,
        "container_id": container_id,
        "stream": stream,
        "level": level,
        "logger": logger,
    }
    if source_timestamp is not None:
        record["source_timestamp"] = source_timestamp
    record.update(schema_fields)
    record[record_field[0]] = record_field[1]
    return record


def read_bounded_line() -> tuple[bytes | None, bool]:
    raw = sys.stdin.buffer.readline(MAX_INPUT_BYTES + 1)
    if not raw:
        return None, False
    too_large = len(raw) > MAX_INPUT_BYTES
    if too_large and not raw.endswith(b"\n"):
        while raw and not raw.endswith(b"\n"):
            raw = sys.stdin.buffer.readline(MAX_INPUT_BYTES + 1)
    return raw, too_large


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--container-id")
    parser.add_argument("--service")
    parser.add_argument("--stream")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--root-fd")
    parser.add_argument("--root-device")
    parser.add_argument("--root-inode")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root_fd: int | None = None
    try:
        root = validate_output_root(args.output_dir)
        inherited = (
            args.root_fd is not None
            or args.root_device is not None
            or args.root_inode is not None
        )
        root_fd = (
            adopt_output_root_fd(
                args.root_fd, args.root_device, args.root_inode
            )
            if inherited
            else open_output_root(root)
        )
        if args.cleanup_only:
            if (
                args.container_id is not None
                or args.service is not None
                or args.stream is not None
            ):
                raise RedactorFailure("REDACTOR_CLEANUP_ARGUMENT_INVALID")
            cleanup_old_files(root, time.time(), root_fd)
            return 0
        if args.container_id is None or CONTAINER_ID_RE.fullmatch(
            args.container_id
        ) is None:
            stable_error("REDACTOR_CONTAINER_ID_INVALID")
            return 64
        if args.service is None or SERVICE_RE.fullmatch(args.service) is None:
            stable_error("REDACTOR_SERVICE_INVALID")
            return 64
        if args.stream not in SAFE_STREAMS:
            stable_error("REDACTOR_STREAM_INVALID")
            return 64
        output = root / f"{args.container_id}.jsonl"
        cleanup_old_files(root, time.time(), root_fd)
        last_cleanup = time.monotonic()
        while True:
            raw, too_large = read_bounded_line()
            if raw is None:
                break
            if too_large:
                record = synthetic_record(
                    args.service,
                    args.container_id,
                    args.stream,
                    "LOG_LINE_DROPPED_TOO_LARGE",
                )
            else:
                record = transform_line(
                    raw, args.service, args.container_id, args.stream
                )
            if record is not None:
                write_record(output, record, root_fd)
            if time.monotonic() - last_cleanup >= CLEANUP_INTERVAL_SECONDS:
                cleanup_old_files(root, time.time(), root_fd)
                last_cleanup = time.monotonic()
    except RedactorFailure as exc:
        stable_error(str(exc))
        return 70
    except Exception:
        stable_error("REDACTOR_INTERNAL_FAILURE")
        return 70
    finally:
        if root_fd is not None:
            os.close(root_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
