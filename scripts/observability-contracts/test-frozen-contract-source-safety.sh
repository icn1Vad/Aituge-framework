#!/usr/bin/bash -p
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
gate="$repo_root/scripts/observability-contracts/verify-frozen-contract-source.sh"
work_dir=$(mktemp -d /tmp/obs70-source-gate-safety-XXXXXX)
synthetic="$work_dir/worktree"
stdout_file="$work_dir/stdout"
stderr_file="$work_dir/stderr"
hostile_bin="$work_dir/hostile"
hostile_log="$work_dir/hostile.log"
bash_env_file="$work_dir/bash-env"
bash_env_marker="$work_dir/bash-env.executed"

cleanup() {
  case "$work_dir" in
    /tmp/obs70-source-gate-safety-*) rm -rf -- "$work_dir" ;;
    *) echo "OBS_CONTRACT_SOURCE_SAFETY_CLEANUP_REFUSED" >&2; exit 1 ;;
  esac
}
trap cleanup EXIT INT TERM

mkdir -p \
  "$synthetic/scripts/observability-contracts" \
  "$synthetic/docs/observability/1.5" \
  "$hostile_bin"
cp -- "$gate" "$synthetic/scripts/observability-contracts/"
for frozen in \
  README.md \
  01-日志与管理员观测平台总体基线.md \
  02-前端页面与接口设计建议.md \
  03-新功能日志接入登记模板.md \
  04-管理员观测接口草案.openapi.yaml \
  05-文件清单与校验记录.md \
  06-Java-Python内部观测接口草案.openapi.yaml \
  07-实现任务与DDL契约测试清单.md; do
  cp -- "$repo_root/docs/observability/1.5/$frozen" \
    "$synthetic/docs/observability/1.5/$frozen"
done
git -C "$synthetic" init -q
git -C "$synthetic" add \
  scripts/observability-contracts/verify-frozen-contract-source.sh \
  docs/observability/1.5

cat >"$hostile_bin/tool" <<'HOSTILE_TOOL'
#!/bin/sh
set -eu
printf '%s\n' "$0 $*" >>"$OBS70_HOSTILE_TOOL_LOG"
exit 0
HOSTILE_TOOL
chmod 700 "$hostile_bin/tool"
for tool in bash git sha256sum realpath stat awk grep dirname; do
  ln -s tool "$hostile_bin/$tool"
done
printf '%s\n' 'printf executed >"$OBS70_BASH_ENV_MARKER"' >"$bash_env_file"
chmod 600 "$bash_env_file"

"$synthetic/scripts/observability-contracts/verify-frozen-contract-source.sh" \
  >"$stdout_file" 2>"$stderr_file"
grep -Fq 'OBS_CONTRACT_SOURCE_OK' "$stdout_file"

/usr/bin/env \
  PATH="$hostile_bin:/usr/bin:/bin" \
  OBS70_HOSTILE_TOOL_LOG="$hostile_log" \
  OBS70_BASH_ENV_MARKER="$bash_env_marker" \
  BASH_ENV="$bash_env_file" ENV="$bash_env_file" SHELLOPTS=xtrace \
  "$synthetic/scripts/observability-contracts/verify-frozen-contract-source.sh" \
  >"$stdout_file" 2>"$stderr_file"
grep -Fq 'OBS_CONTRACT_SOURCE_OK' "$stdout_file"
[[ ! -s "$hostile_log" ]]
[[ ! -e "$bash_env_marker" ]]


set +e
OBS_CONTRACT_DOCS_ROOT="$synthetic/docs/observability/1.5" \
  /usr/bin/env \
  PATH="$hostile_bin:/usr/bin:/bin" \
  OBS70_HOSTILE_TOOL_LOG="$hostile_log" \
  OBS70_BASH_ENV_MARKER="$bash_env_marker" \
  BASH_ENV="$bash_env_file" \
  "$synthetic/scripts/observability-contracts/verify-frozen-contract-source.sh" \
  >"$stdout_file" 2>"$stderr_file"
status=$?
set -e
[[ "$status" -eq 65 ]]
grep -Fq 'OBS_CONTRACT_DOCS_OVERRIDE_DENIED' "$stderr_file"

tampered="$synthetic/docs/observability/1.5/05-文件清单与校验记录.md"
printf 'X' >>"$tampered"
set +e
"$synthetic/scripts/observability-contracts/verify-frozen-contract-source.sh" \
  >"$stdout_file" 2>"$stderr_file"
status=$?
set -e
[[ "$status" -eq 68 ]]
grep -Fq 'OBS_CONTRACT_SOURCE_HASH_MISMATCH file=05-文件清单与校验记录.md' \
  "$stderr_file"
[[ ! -s "$hostile_log" ]]
[[ ! -e "$bash_env_marker" ]]

echo 'OBS_CONTRACT_SOURCE_SAFETY_OK path_override=denied byte_tamper=denied toolchain=absolute-trusted shell=privileged-bash-env-ignored'
