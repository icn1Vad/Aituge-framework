#!/usr/bin/bash -p
set +x
set -euo pipefail
umask 077

unset BASH_ENV ENV CDPATH GLOBIGNORE POSIXLY_CORRECT 2>/dev/null || true
export -n BASHOPTS SHELLOPTS 2>/dev/null || true
readonly PATH="/usr/bin:/bin"
export PATH

readonly GIT_CLI="/usr/bin/git"
readonly REALPATH_CLI="/usr/bin/realpath"
readonly SHA256_CLI="/usr/bin/sha256sum"
readonly STAT_CLI="/usr/bin/stat"
readonly GREP_CLI="/usr/bin/grep"
readonly DIRNAME_CLI="/usr/bin/dirname"

if [[ ! -d /usr || -L /usr || ! -d /usr/bin || -L /usr/bin ||
  "$("$STAT_CLI" -Lc '%u:%g:%a:%F' -- /usr)" != "0:0:755:directory" ||
  "$("$STAT_CLI" -Lc '%u:%g:%a:%F' -- /usr/bin)" != "0:0:755:directory" ]]; then
  echo "OBS_CONTRACT_SOURCE_TOOL_ROOT_INVALID" >&2
  exit 64
fi
for tool in /usr/bin/bash "$GIT_CLI" "$REALPATH_CLI" "$SHA256_CLI" \
  "$STAT_CLI" "$GREP_CLI" "$DIRNAME_CLI"; do
  if [[ ! -f "$tool" || ! -x "$tool" || -L "$tool" ||
    "$("$STAT_CLI" -Lc '%u:%g:%a:%h:%F' -- "$tool")" != "0:0:755:1:regular file" ]]; then
    echo "OBS_CONTRACT_SOURCE_TOOL_INVALID path=$tool" >&2
    exit 64
  fi
done

if [[ -v OBS_CONTRACT_DOCS_ROOT ]]; then
  echo "OBS_CONTRACT_DOCS_OVERRIDE_DENIED" >&2
  exit 65
fi

script_path=$("$REALPATH_CLI" -e -- "${BASH_SOURCE[0]}") || {
  echo "OBS_CONTRACT_SOURCE_SCRIPT_INVALID" >&2
  exit 66
}
if [[ -L "${BASH_SOURCE[0]}" ]]; then
  echo "OBS_CONTRACT_SOURCE_SCRIPT_INVALID" >&2
  exit 66
fi
repo_root=$(cd "$("$DIRNAME_CLI" "$script_path")/../.." && pwd -P)
repo_real=$("$REALPATH_CLI" -e -- "$repo_root") || {
  echo "OBS_CONTRACT_SOURCE_WORKTREE_INVALID" >&2
  exit 66
}
git_root=$("$GIT_CLI" -C "$repo_root" rev-parse --show-toplevel 2>/dev/null) || {
  echo "OBS_CONTRACT_SOURCE_WORKTREE_INVALID" >&2
  exit 66
}
git_root=$("$REALPATH_CLI" -e -- "$git_root") || {
  echo "OBS_CONTRACT_SOURCE_WORKTREE_INVALID" >&2
  exit 66
}
if [[ "$repo_real" != "$repo_root" || "$git_root" != "$repo_root" ||
  "$("$GIT_CLI" -C "$repo_root" rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]] ||
  ! "$GIT_CLI" -C "$repo_root" worktree list --porcelain |
    "$GREP_CLI" -Fxq "worktree $repo_root"; then
  echo "OBS_CONTRACT_SOURCE_WORKTREE_INVALID" >&2
  exit 66
fi

docs_root="$repo_root/docs/observability/1.5"
docs_real=$("$REALPATH_CLI" -e -- "$docs_root" 2>/dev/null) || {
  echo "OBS_CONTRACT_SOURCE_PATH_INVALID" >&2
  exit 66
}
if [[ "$docs_real" != "$docs_root" || -L "$docs_root" ]]; then
  echo "OBS_CONTRACT_SOURCE_PATH_INVALID" >&2
  exit 66
fi
for component in \
  "$repo_root/docs" \
  "$repo_root/docs/observability" \
  "$docs_root"; do
  if [[ ! -d "$component" || -L "$component" ||
    "$("$REALPATH_CLI" -e -- "$component")" != "$component" ]]; then
    echo "OBS_CONTRACT_SOURCE_PATH_INVALID" >&2
    exit 66
  fi
done

readonly -a FROZEN_FILES=(
  "README.md"
  "01-日志与管理员观测平台总体基线.md"
  "02-前端页面与接口设计建议.md"
  "03-新功能日志接入登记模板.md"
  "04-管理员观测接口草案.openapi.yaml"
  "05-文件清单与校验记录.md"
  "06-Java-Python内部观测接口草案.openapi.yaml"
  "07-实现任务与DDL契约测试清单.md"
)
readonly -a FROZEN_SHA256=(
  "86d30a294b0ce41744986edcc565bd70cdef8179b2a2935a75010fa6936362b9"
  "33719e142bad9eba5364a4c436811c94c10f01dcca6f31d9e2e04d8baf87e812"
  "db78612a61c1f4a4f7085cebeea9c20e7bae1075d4014f4fa3c090df840dfd99"
  "0d66549eed691fff406c9ba8c410c5cd2d24151ac56a797308018584b2cf7a67"
  "37b25475e01ecfc586ac8e548b960b076e875acf65863c81802ed044290ca01f"
  "cc09546a4e557a6aea6991d81cd3ccdd5ff67624e1c65948dab9da78d3187faf"
  "9ccd7a241408e545dfdbfc51727eb57532b90be0cd434ecc37291c31d5a735c8"
  "3e90028733c31fea4fae7c966b3d54a4b332d905a7484a9a35a6a3c75584f7a6"
)

for index in "${!FROZEN_FILES[@]}"; do
  relative="docs/observability/1.5/${FROZEN_FILES[$index]}"
  candidate="$repo_root/$relative"
  if [[ ! -f "$candidate" || -L "$candidate" ||
    "$("$REALPATH_CLI" -e -- "$candidate")" != "$candidate" ]] ||
    ! "$GIT_CLI" -C "$repo_root" ls-files --error-unmatch -- "$relative" >/dev/null 2>&1; then
    echo "OBS_CONTRACT_SOURCE_FILE_INVALID file=${FROZEN_FILES[$index]}" >&2
    exit 67
  fi
  mode_record=$("$GIT_CLI" -C "$repo_root" ls-files --stage -- "$relative")
  read -r mode _ <<<"$mode_record"
  if [[ "$mode" != "100644" ]]; then
    echo "OBS_CONTRACT_SOURCE_FILE_MODE_INVALID file=${FROZEN_FILES[$index]}" >&2
    exit 67
  fi
  actual=$("$SHA256_CLI" -- "$candidate")
  actual=${actual%% *}
  if [[ "$actual" != "${FROZEN_SHA256[$index]}" ]]; then
    echo "OBS_CONTRACT_SOURCE_HASH_MISMATCH file=${FROZEN_FILES[$index]}" >&2
    exit 68
  fi
  echo "OBS_CONTRACT_SOURCE_HASH_OK file=${FROZEN_FILES[$index]} sha256=$actual"
done

echo "OBS_CONTRACT_SOURCE_OK worktree=$repo_root files=${#FROZEN_FILES[@]}"
