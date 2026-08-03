#!/usr/bin/bash -p
set +x
set -euo pipefail
umask 077

unset BASH_ENV ENV CDPATH GLOBIGNORE POSIXLY_CORRECT 2>/dev/null || true
export -n BASHOPTS SHELLOPTS 2>/dev/null || true
readonly PATH="/usr/bin:/bin"
export PATH

readonly REALPATH_CLI="/usr/bin/realpath"
readonly PYTHON_CLI="/usr/bin/python3.12"
readonly STAT_CLI="/usr/bin/stat"
readonly ID_CLI="/usr/bin/id"
if [[ ! -d /usr/bin || -L /usr/bin ||
  "$("$STAT_CLI" -Lc '%u:%g:%a:%F' -- /usr/bin)" != "0:0:755:directory" ]]; then
  echo "OBS_CAPABILITY_SCAN_TOOL_ROOT_INVALID" >&2
  exit 66
fi
for trusted_tool in "$REALPATH_CLI" "$PYTHON_CLI" "$STAT_CLI" "$ID_CLI"; do
  if [[ ! -f "$trusted_tool" || ! -x "$trusted_tool" || -L "$trusted_tool" ||
    "$("$STAT_CLI" -Lc '%u:%g:%a:%h:%F' -- "$trusted_tool")" != "0:0:755:1:regular file" ]]; then
    echo "OBS_CAPABILITY_SCAN_TOOL_INVALID path=$trusted_tool" >&2
    exit 66
  fi
done
for loader_override in LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT PYTHONPATH \
  PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE; do
  if [[ -v $loader_override ]]; then
    echo "OBS_CAPABILITY_SCAN_LOADER_OVERRIDE_DENIED name=$loader_override" >&2
    exit 66
  fi
done

root=""
canary_file=""
manifest=""
usage() {
  echo "usage: scan-capability-leaks.sh --root EVIDENCE_ROOT --canary-file PRIVATE_CANARY --manifest EVIDENCE_MANIFEST" >&2
}
while (($#)); do
  case "$1" in
    --root) root="${2:-}"; shift 2 ;;
    --canary-file) canary_file="${2:-}"; shift 2 ;;
    --manifest) manifest="${2:-}"; shift 2 ;;
    *) usage; echo "OBS_CAPABILITY_SCAN_UNKNOWN_ARGUMENT" >&2; exit 64 ;;
  esac
done
if [[ -z "$root" || -z "$canary_file" || -z "$manifest" ]]; then
  usage
  echo "OBS_CAPABILITY_SCAN_ARGUMENT_REQUIRED" >&2
  exit 65
fi
if [[ ! -d "$root" || -L "$root" ||
  ! -f "$canary_file" || -L "$canary_file" ||
  ! -f "$manifest" || -L "$manifest" ]]; then
  echo "OBS_CAPABILITY_SCAN_INPUT_INVALID" >&2
  exit 67
fi
root=$("$REALPATH_CLI" -e -- "$root") || exit 67
canary_file=$("$REALPATH_CLI" -e -- "$canary_file") || exit 67
manifest=$("$REALPATH_CLI" -e -- "$manifest") || exit 67
if [[ "$root" != /tmp/obs70-capability-evidence-[0-9a-f]* &&
  "$root" != /tmp/obs90-capability-evidence-[0-9a-f]* ]] ||
  [[ "$manifest" != "$root/evidence-manifest.json" ]] ||
  [[ "$canary_file" == "$root"/* ]]; then
  echo "OBS_CAPABILITY_SCAN_PATH_INVALID" >&2
  exit 68
fi
root_name="${root##*/}"
run_id="${root_name##*-}"
if [[ ! "$run_id" =~ ^[0-9a-f]{32}$ ||
  "$root_name" != "obs70-capability-evidence-$run_id" &&
  "$root_name" != "obs90-capability-evidence-$run_id" ]]; then
  echo "OBS_CAPABILITY_SCAN_PATH_INVALID" >&2
  exit 68
fi
canary_name="${canary_file##*/}"
if [[ "$canary_name" != "obs-capability-canary-$run_id" ]]; then
  echo "OBS_CAPABILITY_SCAN_CANARY_BINDING_INVALID" >&2
  exit 69
fi
if [[ "$("$STAT_CLI" -c '%u' -- "$root")" != "$("$ID_CLI" -u)" ||
  "$("$STAT_CLI" -c '%u' -- "$canary_file")" != "$("$ID_CLI" -u)" ||
  "$("$STAT_CLI" -c '%u' -- "$manifest")" != "$("$ID_CLI" -u)" ||
  "$("$STAT_CLI" -c '%a' -- "$canary_file")" != 600 ||
  "$("$STAT_CLI" -c '%a' -- "$manifest")" != 600 ]] ||
  ((8#$("$STAT_CLI" -c '%a' -- "$root") & 8#077)); then
  echo "OBS_CAPABILITY_SCAN_PERMISSIONS_INVALID" >&2
  exit 70
fi

set +e
"$PYTHON_CLI" -I -B - "$root" "$manifest" "$canary_file" "$run_id" <<'PY'
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import urllib.parse

root = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
canary_path = Path(sys.argv[3])
run_id = sys.argv[4]


def fail(code: str, status: int = 72) -> None:
    print(code, file=sys.stderr)
    raise SystemExit(status)


try:
    raw_canary = canary_path.read_bytes()
except OSError:
    fail("OBS_CAPABILITY_SCAN_CANARY_READ_FAILED", 71)
if not raw_canary.endswith(b"\n") or raw_canary.count(b"\n") != 1:
    fail("OBS_CAPABILITY_SCAN_CANARY_FORMAT_INVALID", 71)
canary = raw_canary[:-1]
if not re.fullmatch(rb"canary-[0-9a-f]{64}-obs(?:70|90)", canary):
    fail("OBS_CAPABILITY_SCAN_CANARY_FORMAT_INVALID", 71)
expected_page = root.name.split("-", 1)[0].encode()
if not canary.endswith(b"-" + expected_page):
    fail("OBS_CAPABILITY_SCAN_CANARY_BINDING_INVALID", 71)

try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    fail("OBS_CAPABILITY_SCAN_MANIFEST_INVALID")
if not isinstance(manifest, dict) or set(manifest) != {
    "schemaVersion", "kind", "runId", "root", "files"
}:
    fail("OBS_CAPABILITY_SCAN_MANIFEST_INVALID")
if (
    manifest["schemaVersion"] != 1
    or manifest["kind"] != "observability-capability-evidence"
    or manifest["runId"] != run_id
    or manifest["root"] != str(root)
    or not isinstance(manifest["files"], list)
):
    fail("OBS_CAPABILITY_SCAN_MANIFEST_INVALID")

regular_files: dict[str, Path] = {}
try:
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            candidate = current_path / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                fail("OBS_CAPABILITY_SCAN_NONREGULAR_ENTRY")
        for name in file_names:
            candidate = current_path / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                fail("OBS_CAPABILITY_SCAN_NONREGULAR_ENTRY")
            if candidate.stat().st_uid != os.getuid():
                fail("OBS_CAPABILITY_SCAN_FILE_OWNER_INVALID")
            relative = candidate.relative_to(root).as_posix()
            regular_files[relative] = candidate
except OSError:
    fail("OBS_CAPABILITY_SCAN_EVIDENCE_WALK_FAILED")

manifest_relative = "evidence-manifest.json"
if manifest_relative not in regular_files:
    fail("OBS_CAPABILITY_SCAN_MANIFEST_INVALID")
payload_files = {
    name: path for name, path in regular_files.items() if name != manifest_relative
}
listed: dict[str, str] = {}
for entry in manifest["files"]:
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
        fail("OBS_CAPABILITY_SCAN_MANIFEST_INVALID")
    relative = entry["path"]
    digest = entry["sha256"]
    if not isinstance(relative, str) or not isinstance(digest, str):
        fail("OBS_CAPABILITY_SCAN_MANIFEST_INVALID")
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or relative != pure.as_posix()
        or relative == manifest_relative
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or relative in listed
    ):
        fail("OBS_CAPABILITY_SCAN_MANIFEST_INVALID")
    listed[relative] = digest
if set(listed) != set(payload_files):
    fail("OBS_CAPABILITY_SCAN_MANIFEST_FILE_SET_MISMATCH")
for relative, path in payload_files.items():
    actual = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                actual.update(chunk)
    except OSError:
        fail("OBS_CAPABILITY_SCAN_EVIDENCE_READ_FAILED")
    if actual.hexdigest() != listed[relative]:
        fail("OBS_CAPABILITY_SCAN_MANIFEST_HASH_MISMATCH")

forms: dict[str, bytes] = {
    "full": canary,
    "prefix": canary[:32],
    "suffix": canary[-32:],
    "url_percent_upper": b"".join(f"%{byte:02X}".encode() for byte in canary),
    "url_percent_lower": b"".join(f"%{byte:02x}".encode() for byte in canary),
    "base64": base64.b64encode(canary),
    "base64_nopad": base64.b64encode(canary).rstrip(b"="),
    "base64url": base64.urlsafe_b64encode(canary).rstrip(b"="),
    "base64url_padded": base64.urlsafe_b64encode(canary),
    "hex": canary.hex().encode(),
    "sha256": hashlib.sha256(canary).hexdigest().encode(),
    "sha1": hashlib.sha1(canary, usedforsecurity=False).hexdigest().encode(),
    "md5": hashlib.md5(canary, usedforsecurity=False).hexdigest().encode(),
}
max_pattern = max(map(len, forms.values()))
leaks: list[tuple[str, str, str]] = []
for ordinal, (relative, path) in enumerate(sorted(regular_files.items()), start=1):
    encoded_name = os.fsencode(relative)
    path_digest = hashlib.sha256(encoded_name).hexdigest()
    for label, pattern in forms.items():
        if pattern in encoded_name:
            leaks.append((str(ordinal), path_digest, f"name:{label}"))
    if canary in urllib.parse.unquote_to_bytes(encoded_name):
        leaks.append((str(ordinal), path_digest, "name:url_decoded"))
    carry = b""
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                window = carry + chunk
                for label, pattern in forms.items():
                    if pattern in window:
                        leaks.append((str(ordinal), path_digest, f"content:{label}"))
                if canary in urllib.parse.unquote_to_bytes(window):
                    leaks.append((str(ordinal), path_digest, "content:url_decoded"))
                carry = window[-(max_pattern - 1):]
    except OSError:
        fail("OBS_CAPABILITY_SCAN_EVIDENCE_READ_FAILED")

if leaks:
    for ordinal, path_digest, location in sorted(set(leaks)):
        print(
            "OBS_CAPABILITY_LEAK_FOUND "
            f"file_ordinal={ordinal} path_sha256={path_digest} form={location}",
            file=sys.stderr,
        )
    raise SystemExit(3)
print(
    "OBS_CAPABILITY_SCAN_OK "
    f"regular_files={len(regular_files)} manifest_files={len(payload_files)} "
    f"forms={len(forms) + 1}"
)
PY
scan_status=$?
set -e
exit "$scan_status"
