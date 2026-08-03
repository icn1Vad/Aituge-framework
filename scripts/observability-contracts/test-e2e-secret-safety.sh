#!/usr/bin/bash -p
set +x
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
reader="$repo_root/scripts/observability-contracts/read-private-e2e-secret.py"
readonly PYTHON_CLI="/usr/bin/python3.12"
readonly REAL_SECRETS_ROOT="/home/aituge/contract-review-code-dev-test-private/secrets"
readonly LEGACY_WORKSPACE_ROOT="/home/aituge/workspace/contract-review-code-dev/secrets"

if [[ $(id -u) != 1000 || $(id -g) != 1000 ]]; then
  echo "OBS_E2E_SECRET_SAFETY_CALLER_INVALID" >&2
  exit 1
fi
[[ -f "$reader" && ! -L "$reader" ]]

suffix=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
if [[ ! "$suffix" =~ ^[0-9a-f]{32}$ ]]; then
  echo "OBS_E2E_SECRET_SAFETY_SUFFIX_INVALID" >&2
  exit 1
fi
secrets_root="/tmp/obs70-e2e-secrets-$suffix"
race_pid=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$race_pid" ]]; then
    wait "$race_pid" 2>/dev/null || true
  fi
  case "$secrets_root" in
    /tmp/obs70-e2e-secrets-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]*)
      rm -rf -- "$secrets_root" ;;
    *)
      echo "OBS_E2E_SECRET_SAFETY_CLEANUP_REFUSED" >&2
      status=1
      ;;
  esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir "$secrets_root"
chmod 700 "$secrets_root"

read_synthetic() {
  local file=$1 label=$2
  "$PYTHON_CLI" -I -B "$reader"     --root "$secrets_root" --file "$file" --label "$label"     --policy synthetic
}

expect_denied() {
  local label=$1 expected=$2
  shift 2
  local stdout_file="$secrets_root/$label.stdout"
  local stderr_file="$secrets_root/$label.stderr"
  local status
  set +e
  "$PYTHON_CLI" -I -B "$reader" "$@" >"$stdout_file" 2>"$stderr_file"
  status=$?
  set -e
  if [[ "$status" -eq 0 || -s "$stdout_file" ]] ||
    ! grep -Fq "$expected" "$stderr_file"; then
    echo "OBS_E2E_SECRET_SAFETY_NEGATIVE_FAILED label=$label" >&2
    exit 1
  fi
}

safe_value='obs70-private-safe-value'
alternate_value='obs70-private-alternate-must-never-be-read'
printf '%s\n' "$safe_value" >"$secrets_root/good"
chmod 600 "$secrets_root/good"
actual=$(read_synthetic "$secrets_root/good" positive)
[[ "$actual" == "$safe_value" ]]
actual=""
printf '%s' "$safe_value" >"$secrets_root/good-no-newline"
chmod 600 "$secrets_root/good-no-newline"
actual=$(read_synthetic "$secrets_root/good-no-newline" positive-no-newline)
[[ "$actual" == "$safe_value" ]]
actual=""

printf '%s\r\n' "$safe_value" >"$secrets_root/good-crlf"
chmod 600 "$secrets_root/good-crlf"
actual=$(read_synthetic "$secrets_root/good-crlf" positive-crlf)
[[ "$actual" == "$safe_value" ]]
actual=""

malicious_pythonpath="$secrets_root/malicious-pythonpath"
site_marker="$secrets_root/sitecustomize.executed"
site_leak="$secrets_root/sitecustomize.leak"
mkdir "$malicious_pythonpath"
chmod 700 "$malicious_pythonpath"
printf '%s\n' \
  'import os' \
  'from pathlib import Path' \
  'Path(os.environ["OBS70_SITE_MARKER"]).write_text("executed")' \
  'Path(os.environ["OBS70_SITE_LEAK"]).write_bytes(Path(os.environ["OBS70_SECRET_TARGET"]).read_bytes())' \
  >"$malicious_pythonpath/sitecustomize.py"
chmod 600 "$malicious_pythonpath/sitecustomize.py"
actual=$(env \
  PYTHONPATH="$malicious_pythonpath" \
  PYTHONHOME= \
  OBS70_SITE_MARKER="$site_marker" \
  OBS70_SITE_LEAK="$site_leak" \
  OBS70_SECRET_TARGET="$secrets_root/good" \
  "$PYTHON_CLI" -I -B "$reader" \
  --root "$secrets_root" --file "$secrets_root/good" \
  --label isolated-python --policy synthetic)
[[ "$actual" == "$safe_value" ]]
[[ ! -e "$site_marker" && ! -e "$site_leak" ]]
actual=""


expect_denied real-on-synthetic OBS_E2E_SECRETS_ROOT_POLICY_INVALID   --root "$secrets_root" --file "$secrets_root/good" --label real-on-synthetic   --policy real --expected-real-root "$REAL_SECRETS_ROOT"
expect_denied synthetic-on-real OBS_E2E_SECRETS_ROOT_POLICY_INVALID   --root "$REAL_SECRETS_ROOT" --file "$REAL_SECRETS_ROOT/never-read"   --label synthetic-on-real --policy synthetic
expect_denied legacy-real-root OBS_E2E_SECRETS_ROOT_POLICY_INVALID   --root "$LEGACY_WORKSPACE_ROOT" --file "$LEGACY_WORKSPACE_ROOT/never-read"   --label legacy-real-root --policy real   --expected-real-root "$LEGACY_WORKSPACE_ROOT"

for forbidden_file in   "$secrets_root/gluedFoRmAlReviewSecret"   "$secrets_root/prefixPrOdSuffix"   "$secrets_root/mixedProductionMarker"; do
  expect_denied forbidden-substring OBS_E2E_SECRETS_ROOT_INVALID     --root "$secrets_root" --file "$forbidden_file"     --label forbidden-substring --policy synthetic
done

printf '%s\n' "$safe_value" >"$secrets_root/wrong-mode"
chmod 640 "$secrets_root/wrong-mode"
expect_denied wrong-mode OBS_E2E_PRIVATE_FILE_PERMISSIONS_INVALID   --root "$secrets_root" --file "$secrets_root/wrong-mode"   --label wrong-mode --policy synthetic

printf '%s\n' "$safe_value" >"$secrets_root/wrong-group"
chmod 600 "$secrets_root/wrong-group"
chgrp 100 "$secrets_root/wrong-group"
expect_denied wrong-group OBS_E2E_PRIVATE_FILE_PERMISSIONS_INVALID   --root "$secrets_root" --file "$secrets_root/wrong-group"   --label wrong-group --policy synthetic

printf '%s\n' "$alternate_value" >"$secrets_root/alternate"
chmod 600 "$secrets_root/alternate"
ln -s alternate "$secrets_root/final-link"
expect_denied final-symlink OBS_E2E_PRIVATE_FILE_INVALID   --root "$secrets_root" --file "$secrets_root/final-link"   --label final-symlink --policy synthetic

mkdir "$secrets_root/actual-directory"
chmod 700 "$secrets_root/actual-directory"
printf '%s\n' "$safe_value" >"$secrets_root/actual-directory/value"
chmod 600 "$secrets_root/actual-directory/value"
ln -s actual-directory "$secrets_root/symlink-directory"
expect_denied ancestor-symlink OBS_E2E_SECRET_ANCESTOR_INVALID   --root "$secrets_root" --file "$secrets_root/symlink-directory/value"   --label ancestor-symlink --policy synthetic

printf '%s\n' "$safe_value" >"$secrets_root/hardlink-one"
chmod 600 "$secrets_root/hardlink-one"
ln "$secrets_root/hardlink-one" "$secrets_root/hardlink-two"
expect_denied multiple-hardlinks OBS_E2E_PRIVATE_FILE_PERMISSIONS_INVALID   --root "$secrets_root" --file "$secrets_root/hardlink-one"   --label multiple-hardlinks --policy synthetic

printf 'left\nright' >"$secrets_root/embedded-lf"
printf 'left\rright' >"$secrets_root/embedded-cr"
printf 'left\r\nright' >"$secrets_root/embedded-crlf"
printf 'left\0right' >"$secrets_root/nul"
: >"$secrets_root/empty"
chmod 600 "$secrets_root/embedded-lf" "$secrets_root/embedded-cr" \
  "$secrets_root/embedded-crlf" "$secrets_root/nul" "$secrets_root/empty"
for invalid_format in embedded-lf embedded-cr embedded-crlf nul; do
  expect_denied "$invalid_format" OBS_E2E_PRIVATE_FILE_FORMAT_INVALID \
    --root "$secrets_root" --file "$secrets_root/$invalid_format" \
    --label "$invalid_format" --policy synthetic
done
expect_denied empty OBS_E2E_PRIVATE_FILE_PERMISSIONS_INVALID \
  --root "$secrets_root" --file "$secrets_root/empty" \
  --label empty --policy synthetic

chmod 750 "$secrets_root"
expect_denied root-mode OBS_E2E_SECRETS_ROOT_PERMISSIONS_INVALID   --root "$secrets_root" --file "$secrets_root/good"   --label root-mode --policy synthetic
chmod 700 "$secrets_root"

printf '%s\n' "$safe_value" >"$secrets_root/race"
chmod 600 "$secrets_root/race"
(
  for _ in $(seq 1 240); do
    printf '%s\n' "$safe_value" >"$secrets_root/race-next"
    chmod 600 "$secrets_root/race-next"
    mv -Tf -- "$secrets_root/race-next" "$secrets_root/race"
    ln -s alternate "$secrets_root/race-next"
    mv -Tf -- "$secrets_root/race-next" "$secrets_root/race"
  done
) &
race_pid=$!

for _ in $(seq 1 240); do
  set +e
  "$PYTHON_CLI" -I -B "$reader"     --root "$secrets_root" --file "$secrets_root/race" --label swap-race     --policy synthetic >"$secrets_root/race.stdout" 2>"$secrets_root/race.stderr"
  race_status=$?
  set -e
  if [[ "$race_status" -eq 0 ]] &&
    [[ $(<"$secrets_root/race.stdout") != "$safe_value" ]]; then
    echo "OBS_E2E_SECRET_SAFETY_SWAP_LEAKED" >&2
    exit 1
  fi
  if grep -Fq "$alternate_value"     "$secrets_root/race.stdout" "$secrets_root/race.stderr"; then
    echo "OBS_E2E_SECRET_SAFETY_SWAP_LEAKED" >&2
    exit 1
  fi
done
wait "$race_pid"
race_pid=""

echo "OBS_E2E_SECRET_SAFETY_OK roots=split formats=no-newline,lf,crlf invalid=embedded-newline,nul,empty python=isolated-sitecustomize-denied forbidden=substring-case-insensitive ancestors=no-symlink identity=fd-stable owner=uid-gid mode=strict swap=alternate-denied"
