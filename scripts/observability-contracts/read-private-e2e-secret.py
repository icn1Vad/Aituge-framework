#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import stat
import sys

SYNTHETIC_ROOT_PATTERN = re.compile(r"/tmp/obs70-e2e-secrets-[0-9a-f]{32}")
EXPECTED_REAL_USER_ID = 1000
EXPECTED_REAL_GROUP_ID = 1000
REAL_PRIVATE_ROOT = "/home/aituge/contract-review-code-dev-test-private"
REAL_SECRETS_ROOT = REAL_PRIVATE_ROOT + "/secrets"
FORBIDDEN_SUBSTRINGS = ("formal", "prod", "production")


def fail(code: str, label: str, status: int = 83) -> None:
    print(f"{code} label={label}", file=sys.stderr)
    raise SystemExit(status)


def checked_dir(parent_fd: int, name: str, label: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        metadata = os.fstat(child_fd)
    except OSError:
        fail("OBS_E2E_SECRET_ANCESTOR_INVALID", label)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(child_fd)
        fail("OBS_E2E_SECRET_ANCESTOR_INVALID", label)
    return child_fd, metadata


def validate_ancestor(
    metadata: os.stat_result,
    absolute: str,
    user_id: int,
    group_id: int,
    label: str,
) -> None:
    permissions = stat.S_IMODE(metadata.st_mode)
    if absolute == "/tmp":
        if metadata.st_uid != 0 or metadata.st_gid != 0 or permissions != 0o1777:
            fail("OBS_E2E_SECRET_ANCESTOR_INVALID", label)
        return
    if absolute in (REAL_PRIVATE_ROOT, REAL_SECRETS_ROOT):
        if (
            metadata.st_uid != EXPECTED_REAL_USER_ID
            or metadata.st_gid != EXPECTED_REAL_GROUP_ID
            or permissions != 0o700
        ):
            fail("OBS_E2E_SECRET_ANCESTOR_INVALID", label)
        return
    if metadata.st_uid == 0:
        if metadata.st_gid != 0 or permissions & 0o022:
            fail("OBS_E2E_SECRET_ANCESTOR_INVALID", label)
        return
    if (
        metadata.st_uid != user_id
        or metadata.st_gid != group_id
        or permissions & 0o022
    ):
        fail("OBS_E2E_SECRET_ANCESTOR_INVALID", label)


def open_root(root: str, user_id: int, group_id: int, label: str) -> int:
    current_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    absolute = ""
    try:
        for component in root.split("/")[1:]:
            next_fd, metadata = checked_dir(current_fd, component, label)
            os.close(current_fd)
            current_fd = next_fd
            absolute += "/" + component
            validate_ancestor(metadata, absolute, user_id, group_id, label)
        root_metadata = os.fstat(current_fd)
        if (
            root_metadata.st_uid != user_id
            or root_metadata.st_gid != group_id
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            fail("OBS_E2E_SECRETS_ROOT_PERMISSIONS_INVALID", label)
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def read_secret(
    root: str,
    file_name: str,
    label: str,
    policy: str,
    expected_real_root: str,
) -> bytes:
    caller_user_id = os.getuid()
    caller_group_id = os.getgid()
    if caller_user_id == 0 or caller_group_id == 0:
        fail("OBS_E2E_SECRET_CALLER_INVALID", label)
    lowered_paths = (root.lower(), file_name.lower(), expected_real_root.lower())
    if (
        any(token in value for value in lowered_paths for token in FORBIDDEN_SUBSTRINGS)
        or not os.path.isabs(root)
        or not os.path.isabs(file_name)
        or os.path.normpath(root) != root
        or os.path.normpath(file_name) != file_name
    ):
        fail("OBS_E2E_SECRETS_ROOT_INVALID", label)
    if policy == "synthetic":
        if not SYNTHETIC_ROOT_PATTERN.fullmatch(root) or expected_real_root:
            fail("OBS_E2E_SECRETS_ROOT_POLICY_INVALID", label)
        user_id = caller_user_id
        group_id = caller_group_id
    elif policy == "real":
        if (
            root != REAL_SECRETS_ROOT
            or expected_real_root != REAL_SECRETS_ROOT
        ):
            fail("OBS_E2E_SECRETS_ROOT_POLICY_INVALID", label)
        if (
            caller_user_id != EXPECTED_REAL_USER_ID
            or caller_group_id != EXPECTED_REAL_GROUP_ID
        ):
            fail("OBS_E2E_SECRET_CALLER_INVALID", label)
        user_id = EXPECTED_REAL_USER_ID
        group_id = EXPECTED_REAL_GROUP_ID
    else:
        fail("OBS_E2E_SECRETS_ROOT_POLICY_INVALID", label)
    try:
        if os.path.commonpath((root, file_name)) != root or file_name == root:
            fail("OBS_E2E_SECRET_PATH_INVALID", label)
    except ValueError:
        fail("OBS_E2E_SECRET_PATH_INVALID", label)

    relative = file_name[len(root) + 1 :].split("/")
    if not relative or any(item in ("", ".", "..") for item in relative):
        fail("OBS_E2E_SECRET_PATH_INVALID", label)

    directory_fd = open_root(root, user_id, group_id, label)
    try:
        for component in relative[:-1]:
            next_fd, metadata = checked_dir(directory_fd, component, label)
            os.close(directory_fd)
            directory_fd = next_fd
            if (
                metadata.st_uid != user_id
                or metadata.st_gid != group_id
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                fail("OBS_E2E_SECRET_ANCESTOR_INVALID", label)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            secret_fd = os.open(relative[-1], flags, dir_fd=directory_fd)
        except OSError:
            fail("OBS_E2E_PRIVATE_FILE_INVALID", label)
        try:
            before = os.fstat(secret_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != user_id
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_gid != group_id
                or before.st_nlink != 1
                or not 1 <= before.st_size <= 8192
            ):
                fail("OBS_E2E_PRIVATE_FILE_PERMISSIONS_INVALID", label)
            chunks: list[bytes] = []
            remaining = before.st_size + 1
            while remaining:
                chunk = os.read(secret_fd, min(remaining, 4096))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            value = b"".join(chunks)
            after = os.fstat(secret_fd)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                before.st_mode,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_uid,
                after.st_mode,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if identity_before != identity_after:
                fail("OBS_E2E_PRIVATE_FILE_IDENTITY_CHANGED", label)
            if b"\0" in value:
                fail("OBS_E2E_PRIVATE_FILE_FORMAT_INVALID", label)
            if value.endswith(b"\r\n"):
                normalized = value[:-2]
            elif value.endswith(b"\n"):
                normalized = value[:-1]
            else:
                normalized = value
            if not normalized or b"\r" in normalized or b"\n" in normalized:
                fail("OBS_E2E_PRIVATE_FILE_FORMAT_INVALID", label)
            return normalized
        finally:
            os.close(secret_fd)
    finally:
        os.close(directory_fd)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--policy", choices=("synthetic", "real"), required=True)
    parser.add_argument("--expected-real-root", default="")
    arguments = parser.parse_args()
    os.write(
        1,
        read_secret(
            arguments.root,
            arguments.file,
            arguments.label,
            arguments.policy,
            arguments.expected_real_root,
        ),
    )


if __name__ == "__main__":
    main()
