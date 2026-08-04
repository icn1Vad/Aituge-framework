#!/usr/bin/env python3
"""Open the capture root and manager lock without path-based TOCTOU."""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import stat
import sys

from redact_stream import (
    RedactorFailure,
    open_output_root,
    validate_output_root,
    validate_root_fd,
)

LOCK_NAME = ".capture-manager.lock"
SCRIPT = Path(__file__).with_name("capture-test-container-logs.sh")

UNSAFE_ENVIRONMENT = {
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "BASH_ENV",
    "ENV",
}


class LauncherFailure(RuntimeError):
    pass


def stable_error(code: str) -> None:
    try:
        os.write(2, (code + "\n").encode("ascii", "strict"))
    except OSError:
        pass


def validate_lock_info(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
        or info.st_gid != os.getegid()
        or info.st_nlink != 1
    ):
        raise LauncherFailure("CAPTURE_MANAGER_LOCK_UNSAFE")


def lock_entry_info(root_fd: int) -> os.stat_result | None:
    try:
        info = os.stat(LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    validate_lock_info(info)
    return info


def open_manager_lock(root_fd: int) -> int:
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise LauncherFailure("CAPTURE_NOFOLLOW_UNAVAILABLE")
    flags |= nofollow
    for _ in range(3):
        expected = lock_entry_info(root_fd)
        try:
            if expected is None:
                lock_fd = os.open(
                    LOCK_NAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=root_fd,
                )
                os.fchmod(lock_fd, 0o600)
            else:
                lock_fd = os.open(LOCK_NAME, flags, dir_fd=root_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise LauncherFailure("CAPTURE_MANAGER_LOCK_UNSAFE") from exc
            raise
        try:
            opened = os.fstat(lock_fd)
            validate_lock_info(opened)
            if expected is not None and (
                opened.st_dev != expected.st_dev
                or opened.st_ino != expected.st_ino
            ):
                raise LauncherFailure("CAPTURE_MANAGER_LOCK_CHANGED")
            current = lock_entry_info(root_fd)
            if current is None or (
                current.st_dev != opened.st_dev
                or current.st_ino != opened.st_ino
            ):
                raise LauncherFailure("CAPTURE_MANAGER_LOCK_CHANGED")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LauncherFailure("CAPTURE_MANAGER_ALREADY_RUNNING") from exc
            return lock_fd
        except Exception:
            os.close(lock_fd)
            raise
    raise LauncherFailure("CAPTURE_MANAGER_LOCK_CREATE_RACE")


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--output-dir":
        stable_error("CAPTURE_SECURE_LAUNCH_ARGUMENT_INVALID")
        return 64
    raw_root = sys.argv[2]
    arguments = sys.argv[3:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    root_fd: int | None = None
    lock_fd: int | None = None
    try:
        root = validate_output_root(raw_root)
        root_fd = open_output_root(root)
        root_info = validate_root_fd(root_fd)
        if not arguments:
            lock_fd = open_manager_lock(root_fd)
        os.set_inheritable(root_fd, True)
        if lock_fd is not None:
            os.set_inheritable(lock_fd, True)
        environment = os.environ.copy()
        for name in UNSAFE_ENVIRONMENT:
            environment.pop(name, None)
        environment["PAGE6_CAPTURE_SECURE_LAUNCHED"] = "1"
        environment["PAGE6_CAPTURE_ROOT_FD"] = str(root_fd)
        environment["PAGE6_CAPTURE_ROOT_DEVICE"] = str(root_info.st_dev)
        environment["PAGE6_CAPTURE_ROOT_INODE"] = str(root_info.st_ino)
        if lock_fd is None:
            environment.pop("PAGE6_CAPTURE_LOCK_FD", None)
        else:
            environment["PAGE6_CAPTURE_LOCK_FD"] = str(lock_fd)
        os.execve(
            "/usr/bin/bash",
            ["bash", str(SCRIPT), *arguments],
            environment,
        )
    except LauncherFailure as exc:
        stable_error(str(exc))
        return 73 if str(exc) == "CAPTURE_MANAGER_ALREADY_RUNNING" else 1
    except RedactorFailure as exc:
        stable_error(str(exc))
        return 1
    except OSError:
        stable_error("CAPTURE_SECURE_LAUNCH_FAILED")
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if root_fd is not None:
            os.close(root_fd)
    return 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
