#!/usr/bin/bash -p
set +x
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
scanner="$repo_root/scripts/observability-contracts/scan-capability-leaks.sh"
safety_dir=$(/usr/bin/mktemp -d /tmp/obs70-capability-scanner-safety-XXXXXX)
hostile_bin="$safety_dir/hostile"
hostile_log="$safety_dir/hostile.log"
bash_env_file="$safety_dir/bash-env"
bash_env_marker="$safety_dir/bash-env.executed"
mkdir -m 700 "$hostile_bin"
cat >"$hostile_bin/tool" <<'HOSTILE'
#!/bin/sh
set -eu
printf '%s\n' "$0 $*" >>"$OBS70_HOSTILE_TOOL_LOG"
exit 0
HOSTILE
chmod 700 "$hostile_bin/tool"
for hostile_tool in bash realpath python3 python3.12 stat id; do
  ln -s tool "$hostile_bin/$hostile_tool"
done
printf '%s\n' 'printf executed >"$OBS70_BASH_ENV_MARKER"' >"$bash_env_file"
chmod 600 "$bash_env_file"
roots=()
canaries=()
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  local value
  for value in "${roots[@]}"; do
    case "$value" in
      /tmp/obs70-capability-evidence-[0-9a-f][0-9a-f]*)
        rm -rf -- "$value" || status=1
        ;;
      *) echo "OBS_CAPABILITY_SCAN_SELF_TEST_CLEANUP_REFUSED" >&2; status=1 ;;
    esac
  done
  for value in "${canaries[@]}"; do
    case "$value" in
      /tmp/obs-capability-canary-[0-9a-f][0-9a-f]*)
        rm -f -- "$value" || status=1
        ;;
      *) echo "OBS_CAPABILITY_SCAN_SELF_TEST_CLEANUP_REFUSED" >&2; status=1 ;;
    esac
  done
  case "$safety_dir" in
    /tmp/obs70-capability-scanner-safety-*) rm -rf -- "$safety_dir" || status=1 ;;
    *) echo "OBS_CAPABILITY_SCAN_SELF_TEST_CLEANUP_REFUSED" >&2; status=1 ;;
  esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

new_case() {
  CASE_RUN_ID=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  CASE_RANDOM=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  if [[ ! "$CASE_RUN_ID" =~ ^[0-9a-f]{32}$ ||
    ! "$CASE_RANDOM" =~ ^[0-9a-f]{64}$ ]]; then
    echo "OBS_CAPABILITY_SCAN_SELF_TEST_RANDOM_FAILED" >&2
    exit 1
  fi
  CASE_ROOT="/tmp/obs70-capability-evidence-$CASE_RUN_ID"
  CASE_CANARY_FILE="/tmp/obs-capability-canary-$CASE_RUN_ID"
  CASE_CANARY="canary-$CASE_RANDOM-obs70"
  mkdir -m 700 -- "$CASE_ROOT"
  printf '%s\n' "$CASE_CANARY" >"$CASE_CANARY_FILE"
  chmod 600 "$CASE_CANARY_FILE"
  roots+=("$CASE_ROOT")
  canaries+=("$CASE_CANARY_FILE")
}

write_manifest() {
  local root="$1" run_id="$2"
  python3 - "$root" "$run_id" <<'PY'
from pathlib import Path
import hashlib
import json
import sys

root = Path(sys.argv[1])
run_id = sys.argv[2]
manifest = root / "evidence-manifest.json"
files = []
for path in sorted(root.rglob("*")):
    if path == manifest or not path.is_file() or path.is_symlink():
        continue
    files.append(
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )
value = {
    "schemaVersion": 1,
    "kind": "observability-capability-evidence",
    "runId": run_id,
    "root": str(root),
    "files": files,
}
manifest.write_text(
    json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
manifest.chmod(0o600)
PY
}

run_scan() {
  set +e
  SCAN_OUTPUT=$(
    /usr/bin/env \
      PATH="$hostile_bin:/usr/bin:/bin" \
      BASH_ENV="$bash_env_file" ENV="$bash_env_file" SHELLOPTS=xtrace \
      OBS70_HOSTILE_TOOL_LOG="$hostile_log" \
      OBS70_BASH_ENV_MARKER="$bash_env_marker" \
      "$scanner" \
      --root "$CASE_ROOT" \
      --canary-file "$CASE_CANARY_FILE" \
      --manifest "$CASE_ROOT/evidence-manifest.json" 2>&1
  )
  SCAN_STATUS=$?
  set -e
  if [[ "$SCAN_OUTPUT" == *"$CASE_CANARY"* ]]; then
    echo "OBS_CAPABILITY_SCAN_SELF_TEST_CANARY_DISCLOSED" >&2
    exit 1
  fi
  [[ ! -s "$hostile_log" ]]
  [[ ! -e "$bash_env_marker" ]]
}

new_case
first_canary="$CASE_CANARY"
printf '%s\n' safe >"$CASE_ROOT/untracked-safe.log"
write_manifest "$CASE_ROOT" "$CASE_RUN_ID"
run_scan
[[ "$SCAN_STATUS" -eq 0 ]]
[[ "$SCAN_OUTPUT" == *"OBS_CAPABILITY_SCAN_OK regular_files=2 manifest_files=1 forms=14"* ]]

new_case
printf '%s\n' "${CASE_CANARY%-obs70}-obs90" >"$CASE_CANARY_FILE"
printf '%s\n' safe >"$CASE_ROOT/binding.log"
write_manifest "$CASE_ROOT" "$CASE_RUN_ID"
run_scan
[[ "$SCAN_STATUS" -eq 71 ]]
[[ "$SCAN_OUTPUT" == *"OBS_CAPABILITY_SCAN_CANARY_BINDING_INVALID"* ]]

new_case
if [[ "$CASE_CANARY" == "$first_canary" ]]; then
  echo "OBS_CAPABILITY_SCAN_SELF_TEST_RANDOM_REUSE" >&2
  exit 1
fi
python3 - "$CASE_ROOT" "$CASE_CANARY_FILE" <<'PY'
from pathlib import Path
import base64
import hashlib
import sys

root = Path(sys.argv[1])
canary = Path(sys.argv[2]).read_text(encoding="ascii").strip().encode()
forms = {
    "01-full.log": canary,
    "02-prefix.log": canary[:32],
    "03-suffix.log": canary[-32:],
    "04-url-percent-upper.log": b"".join(f"%{byte:02X}".encode() for byte in canary),
    "05-url-percent-lower.log": b"".join(f"%{byte:02x}".encode() for byte in canary),
    "06-base64.log": base64.b64encode(canary),
    "07-base64-nopad.log": base64.b64encode(canary).rstrip(b"="),
    "08-base64url.log": base64.urlsafe_b64encode(canary).rstrip(b"="),
    "09-base64url-padded.log": base64.urlsafe_b64encode(canary),
    "10-hex.log": canary.hex().encode(),
    "11-sha256.log": hashlib.sha256(canary).hexdigest().encode(),
    "12-sha1.log": hashlib.sha1(canary, usedforsecurity=False).hexdigest().encode(),
    "13-md5.log": hashlib.md5(canary, usedforsecurity=False).hexdigest().encode(),
    "14-url-mixed.log": canary.replace(b"-", b"%2d"),
}
for name, value in forms.items():
    (root / name).write_bytes(value + b"\n")
(root / ("15-name-" + canary.decode() + ".log")).write_text("safe\n", encoding="ascii")
PY
write_manifest "$CASE_ROOT" "$CASE_RUN_ID"
run_scan
[[ "$SCAN_STATUS" -eq 3 ]]
for form in \
  full prefix suffix url_percent_upper url_percent_lower \
  base64 base64_nopad base64url base64url_padded \
  hex sha256 sha1 md5 url_decoded; do
  [[ "$SCAN_OUTPUT" == *"form=content:$form"* ]]
done
[[ "$SCAN_OUTPUT" == *"form=name:full"* ]]

new_case
printf '%s\n' safe >"$CASE_ROOT/tampered.log"
write_manifest "$CASE_ROOT" "$CASE_RUN_ID"
printf '%s\n' changed >>"$CASE_ROOT/tampered.log"
run_scan
[[ "$SCAN_STATUS" -eq 72 ]]
[[ "$SCAN_OUTPUT" == *"OBS_CAPABILITY_SCAN_MANIFEST_HASH_MISMATCH"* ]]

new_case
printf '%s\n' safe >"$CASE_ROOT/listed.log"
write_manifest "$CASE_ROOT" "$CASE_RUN_ID"
printf '%s\n' unlisted >"$CASE_ROOT/unlisted.log"
run_scan
[[ "$SCAN_STATUS" -eq 72 ]]
[[ "$SCAN_OUTPUT" == *"OBS_CAPABILITY_SCAN_MANIFEST_FILE_SET_MISMATCH"* ]]

new_case
printf '%s\n' safe >"$CASE_ROOT/listed.log"
write_manifest "$CASE_ROOT" "$CASE_RUN_ID"
ln -s listed.log "$CASE_ROOT/forbidden-link"
run_scan
[[ "$SCAN_STATUS" -eq 72 ]]
[[ "$SCAN_OUTPUT" == *"OBS_CAPABILITY_SCAN_NONREGULAR_ENTRY"* ]]

first_canary=""
CASE_CANARY=""
SCAN_OUTPUT=""
echo "OBS_CAPABILITY_SCAN_SELF_TEST_OK files=all-regular forms=14 manifest=sha256 canary=random toolchain=absolute-trusted python=isolated shell=privileged-bash-env-ignored"
