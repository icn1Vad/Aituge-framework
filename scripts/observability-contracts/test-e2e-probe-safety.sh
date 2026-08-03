#!/usr/bin/bash -p
set +x
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
probe="$repo_root/scripts/observability-contracts/e2e-observability-probe.sh"
readonly PYTHON_CLI="/usr/bin/python3.12"
work_dir=$(mktemp -d /tmp/obs70-e2e-probe-safety-XXXXXX)
fake_bash_log="$work_dir/fake-bash.log"
bash_env_file="$work_dir/bash-env"
shell_marker="$work_dir/shell-startup.executed"
shell_leak="$work_dir/shell-startup.leak"
suffix=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
if [[ ! "$suffix" =~ ^[0-9a-f]{32}$ ]]; then
  echo "OBS_E2E_PROBE_SAFETY_SUFFIX_INVALID" >&2
  exit 1
fi
secrets_root="/tmp/obs70-e2e-secrets-$suffix"
server_pid=""
probe_pid=""
server_identity_captured=0
probe_identity_captured=0
declare -A process_uid=()
declare -A process_pgid=()
declare -A process_session=()
declare -A process_start=()
declare -A process_command_hash=()

forget_process() {
  local pid=$1
  unset 'process_uid[$pid]' 'process_pgid[$pid]' 'process_session[$pid]' \
    'process_start[$pid]' 'process_command_hash[$pid]'
}

capture_process_identity() {
  local pid=$1 raw rest uid command_hash
  local -a fields
  [[ $pid =~ ^[0-9]+$ && -r /proc/$pid/stat && -r /proc/$pid/cmdline ]] || return 1
  uid=$(stat -Lc '%u' "/proc/$pid")
  [[ "$uid" == "$(id -u)" ]] || return 1
  raw=$(<"/proc/$pid/stat")
  rest=${raw#*) }
  read -r -a fields <<<"$rest"
  [[ ${#fields[@]} -ge 20 && ${fields[2]} == "$pid" && ${fields[3]} == "$pid" ]] ||
    return 1
  command_hash=$(sha256sum "/proc/$pid/cmdline" | awk '{print $1}')
  process_uid[$pid]=$uid
  process_pgid[$pid]=${fields[2]}
  process_session[$pid]=${fields[3]}
  process_start[$pid]=${fields[19]}
  process_command_hash[$pid]=$command_hash
}

verify_process_identity() {
  local pid=$1 raw rest uid command_hash
  local -a fields
  [[ -n ${process_start[$pid]-} ]] || return 2
  [[ -e /proc/$pid ]] || return 1
  [[ -r /proc/$pid/stat && -r /proc/$pid/cmdline ]] || return 2
  uid=$(stat -Lc '%u' "/proc/$pid") || return 2
  raw=$(<"/proc/$pid/stat")
  rest=${raw#*) }
  read -r -a fields <<<"$rest"
  [[ ${#fields[@]} -ge 20 ]] || return 2
  command_hash=$(sha256sum "/proc/$pid/cmdline" | awk '{print $1}') || return 2
  [[ "$uid" == "${process_uid[$pid]}" &&
    ${fields[2]} == "${process_pgid[$pid]}" &&
    ${fields[3]} == "${process_session[$pid]}" &&
    ${fields[19]} == "${process_start[$pid]}" &&
    "$command_hash" == "${process_command_hash[$pid]}" &&
    ${fields[2]} == "$pid" && ${fields[3]} == "$pid" ]] || return 2
}

terminate_process_group() {
  local pid=$1 identity_status index
  if verify_process_identity "$pid"; then identity_status=0; else identity_status=$?; fi
  if [[ "$identity_status" -eq 1 ]]; then
    forget_process "$pid"
    return 0
  fi
  [[ "$identity_status" -eq 0 ]] || return 1
  kill -TERM -- "-$pid" 2>/dev/null || true
  for index in {1..50}; do
    [[ -e /proc/$pid ]] || { forget_process "$pid"; return 0; }
    sleep 0.1
  done
  if verify_process_identity "$pid"; then identity_status=0; else identity_status=$?; fi
  [[ "$identity_status" -eq 0 ]] || return 1
  kill -KILL -- "-$pid" 2>/dev/null || true
  for index in {1..20}; do
    [[ -e /proc/$pid ]] || { forget_process "$pid"; return 0; }
    sleep 0.1
  done
  return 1
}

cleanup() {
  local status=$? cleanup_status=0
  trap - EXIT INT TERM
  if [[ "$probe_identity_captured" -eq 1 ]]; then
    terminate_process_group "$probe_pid" || cleanup_status=1
    wait "$probe_pid" 2>/dev/null || true
  elif [[ -n "$probe_pid" ]]; then
    wait "$probe_pid" 2>/dev/null || true
  fi
  if [[ "$server_identity_captured" -eq 1 ]]; then
    terminate_process_group "$server_pid" || cleanup_status=1
    wait "$server_pid" 2>/dev/null || true
  elif [[ -n "$server_pid" ]]; then
    wait "$server_pid" 2>/dev/null || true
  fi
  if [[ "$secrets_root" =~ ^/tmp/obs70-e2e-secrets-[0-9a-f]{32}$ ]]; then
    rm -rf -- "$secrets_root" || cleanup_status=1
  else
    echo "OBS_E2E_PROBE_SAFETY_SECRET_CLEANUP_REFUSED" >&2
    cleanup_status=1
  fi
  case "$work_dir" in
    /tmp/obs70-e2e-probe-safety-*) rm -rf -- "$work_dir" || cleanup_status=1 ;;
    *) echo "OBS_E2E_PROBE_SAFETY_CLEANUP_REFUSED" >&2; cleanup_status=1 ;;
  esac
  [[ "$status" -ne 0 ]] || status=$cleanup_status
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$work_dir/bin" "$work_dir/policy" \
  "$work_dir/malicious-home" "$work_dir/xdg/curl"
mkdir "$secrets_root"
chmod 700 "$work_dir" "$work_dir/policy" "$work_dir/malicious-home" \
  "$work_dir/xdg" "$work_dir/xdg/curl" "$secrets_root"
printf '%s\n' 1 >"$work_dir/policy/formal-port-denylist"
chmod 600 "$work_dir/policy/formal-port-denylist"

bearer_value="obs70syntheticbearer$suffix"
cursor_value="obs70-e2e-cursor-$suffix"
idempotency_value="obs70-e2e-$suffix"
locator_value="obs70-e2e-locator-$suffix"
printf '%s\n' "$bearer_value" >"$secrets_root/bearer"
printf '%s\n' "$cursor_value" >"$secrets_root/cursor"
printf '%s\n' "$idempotency_value" >"$secrets_root/idempotency"
printf '%s\n' "$locator_value" >"$secrets_root/locator"
chmod 600 "$secrets_root/bearer" "$secrets_root/cursor" \
  "$secrets_root/idempotency" "$secrets_root/locator"
bearer_value=""
cursor_value=""
idempotency_value=""
locator_value=""

printf 'header = "X-Injected-Curlrc: yes"\noutput = "%s"\n' \
  "$work_dir/curlrc-owned" >"$work_dir/malicious-home/.curlrc"
cp "$work_dir/malicious-home/.curlrc" "$work_dir/xdg/curlrc"
cp "$work_dir/malicious-home/.curlrc" "$work_dir/xdg/curl/curlrc"

fake_curl_log="$work_dir/fake-curl.log"
cat >"$work_dir/bin/curl" <<'SH'
#!/bin/sh
set -eu
printf '%s\n' invoked >>"$FAKE_CURL_LOG"
exit 99
SH
chmod 700 "$work_dir/bin/curl"

cat >"$work_dir/bin/bash" <<'SH'
#!/bin/sh
set -eu
printf '%s\n' invoked >>"$OBS70_FAKE_BASH_LOG"
if [ -n "${OBS_E2E_BEARER_FILE:-}" ] && [ -r "$OBS_E2E_BEARER_FILE" ]; then
  /usr/bin/cp -- "$OBS_E2E_BEARER_FILE" "$OBS70_SHELL_LEAK"
fi
exit 99
SH
chmod 700 "$work_dir/bin/bash"
cat >"$bash_env_file" <<'SH'
if [ -n "${OBS_E2E_BEARER_FILE:-}" ] && [ -r "$OBS_E2E_BEARER_FILE" ]; then
  /usr/bin/cp -- "$OBS_E2E_BEARER_FILE" "$OBS70_SHELL_LEAK"
fi
printf executed >"$OBS70_SHELL_MARKER"
SH
chmod 600 "$bash_env_file"

malicious_loader_dir="$work_dir/malicious-python-loader"
loader_marker="$work_dir/python-loader.executed"
mkdir "$malicious_loader_dir"
chmod 700 "$malicious_loader_dir"
printf '%s\n' \
  'import os' \
  'from pathlib import Path' \
  'Path(os.environ["OBS70_LOADER_MARKER"]).write_text("executed")' \
  >"$malicious_loader_dir/sitecustomize.py"
chmod 600 "$malicious_loader_dir/sitecustomize.py"
for loader_setting in PYTHONPATH LD_LIBRARY_PATH; do
  set +e
  env "$loader_setting=$malicious_loader_dir" \
    PATH="$work_dir/bin:/usr/bin:/bin" \
    BASH_ENV="$bash_env_file" ENV="$bash_env_file" SHELLOPTS=xtrace \
    OBS70_FAKE_BASH_LOG="$fake_bash_log" \
    OBS70_SHELL_MARKER="$shell_marker" \
    OBS70_SHELL_LEAK="$shell_leak" \
    OBS70_LOADER_MARKER="$loader_marker" \
    OBS_E2E_RUN=I_UNDERSTAND_TEST_ONLY \
    "$probe" \
    >"$work_dir/loader-$loader_setting.stdout" \
    2>"$work_dir/loader-$loader_setting.stderr"
  loader_status=$?
  set -e
  [[ "$loader_status" -eq 80 ]]
  grep -Fq "OBS_E2E_PYTHON_LOADER_OVERRIDE_DENIED name=$loader_setting" \
    "$work_dir/loader-$loader_setting.stderr"
  [[ ! -e "$loader_marker" ]]
  [[ ! -s "$fake_bash_log" ]]
  [[ ! -e "$shell_marker" ]]
  [[ ! -e "$shell_leak" ]]
done
port_file="$work_dir/port"
request_log="$work_dir/requests"
command -v setsid >/dev/null
setsid "$PYTHON_CLI" - \
  "$port_file" "$request_log" \
  "$secrets_root/bearer" "$secrets_root/cursor" \
  "$secrets_root/idempotency" "$secrets_root/locator" <<'PY' &
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import sys
import time

port_file = Path(sys.argv[1])
request_log = Path(sys.argv[2])
bearer = Path(sys.argv[3]).read_text(encoding="utf-8").rstrip("\n")
cursor = Path(sys.argv[4]).read_text(encoding="utf-8").rstrip("\n")
idempotency = Path(sys.argv[5]).read_text(encoding="utf-8").rstrip("\n")
locator = Path(sys.argv[6]).read_text(encoding="utf-8").rstrip("\n")


class Handler(BaseHTTPRequestHandler):
    def respond(self, status):
        length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(length) if length else b""
        injected = bool(self.headers.get("X-Injected-Curlrc", ""))
        authorized = self.headers.get("Authorization", "") == f"Bearer {bearer}"
        argv_canaries = (
            cursor in self.path
            and self.headers.get("Idempotency-Key", "") == idempotency
            and locator.encode() in request_body
            and authorized
        )
        with request_log.open("a", encoding="utf-8") as stream:
            stream.write(
                f"method={self.command} injected={injected} "
                f"authorized={authorized} argv_canaries={argv_canaries}\n"
            )
        time.sleep(0.25)
        body = json.dumps({"code": f"STATUS_{status}"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Vary", "Authorization")
        self.send_header("X-Request-ID", "obs70-probe-safety")
        self.end_headers()
        self.wfile.write(body)

    def route(self):
        path = self.path.split("?", 1)[0]
        authorized = self.headers.get("Authorization", "") == f"Bearer {bearer}"
        if path.endswith("/overview"):
            return 200 if authorized else 401
        if path.endswith("/timeline"):
            return 400
        if path.endswith("/events"):
            return 400
        if path.endswith("/runtime-logs/detail"):
            return 404
        if path.endswith("/security-events/evt_obs70_synthetic_missing"):
            return 404
        return 500

    def do_GET(self):
        self.respond(self.route())

    def do_POST(self):
        self.respond(self.route())

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.timeout = 0.2
port_file.write_text(str(server.server_address[1]), encoding="ascii")
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    server.handle_request()
PY
server_pid=$!

for _ in {1..100}; do
  [[ -s "$port_file" ]] && break
  sleep 0.02
done
if [[ ! -s "$port_file" ]]; then
  echo "OBS_E2E_PROBE_SAFETY_SERVER_FAILED" >&2
  exit 1
fi
capture_process_identity "$server_pid"
server_identity_captured=1
verify_process_identity "$server_pid"

original_start=${process_start[$server_pid]}
process_start[$server_pid]=0
if verify_process_identity "$server_pid"; then
  drift_status=0
else
  drift_status=$?
fi
process_start[$server_pid]=$original_start
[[ "$drift_status" -eq 2 ]]
verify_process_identity "$server_pid"

port=$(<"$port_file")
if [[ ! "$port" =~ ^[0-9]{4,5}$ ]]; then
  echo "OBS_E2E_PROBE_SAFETY_PORT_INVALID" >&2
  exit 1
fi

gateway_id=$(printf 'a%.0s' {1..64})
gateway_network_id=$(printf 'b%.0s' {1..64})
gateway_image_id="sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
cat >"$work_dir/bin/docker" <<'SH'
#!/bin/sh
set -eu
if [ "$1" = context ] && [ "$2" = show ]; then
  printf '%s\n' default
  exit 0
fi
if [ "$1" = context ] && [ "$2" = inspect ]; then
  printf '%s\n' 'unix:///var/run/docker.sock|{}'
  exit 0
fi
if [ "$1" = --context ]; then
  [ "$2" = default ] || exit 90
  shift 2
fi
case "$1" in
  info)
    printf '%s\n' '3196b392-cce0-4178-a30a-2a9ff44d271c|afs2600151|/var/lib/docker'
    ;;
  network)
    [ "$2" = inspect ] || exit 90
    printf '%s\n' "$FAKE_NETWORK_METADATA"
    ;;
  inspect)
    printf '%s\n' "$FAKE_DOCKER_METADATA"
    ;;
  *)
    exit 90
    ;;
esac
SH
chmod 700 "$work_dir/bin/docker"
metadata="$gateway_id|/contract-review-dev-caddy-1|contract-review-code-dev|test|test-gateway|$gateway_image_id|true|contract-review-code-dev-agent-internal@$gateway_network_id,|8080/tcp@127.0.0.1@$port,"
network_metadata="$gateway_network_id|contract-review-code-dev-agent-internal|contract-review-code-dev|agent_internal"

stdout_file="$work_dir/stdout"
stderr_file="$work_dir/stderr"
probe_environment=(
  "PATH=$work_dir/bin:/usr/bin:/bin"
  "BASH_ENV=$bash_env_file"
  "ENV=$bash_env_file"
  "SHELLOPTS=xtrace"
  "OBS70_FAKE_BASH_LOG=$fake_bash_log"
  "OBS70_SHELL_MARKER=$shell_marker"
  "OBS70_SHELL_LEAK=$shell_leak"
  "HOME=$work_dir/malicious-home"
  "CURL_HOME=$work_dir/malicious-home"
  "XDG_CONFIG_HOME=$work_dir/xdg"
  "FAKE_CURL_LOG=$fake_curl_log"
  "FAKE_DOCKER_METADATA=$metadata"
  "OBS_E2E_RUN=I_UNDERSTAND_TEST_ONLY"
  "FAKE_NETWORK_METADATA=$network_metadata"
  "OBS_E2E_PROBE_TEST_MODE=SYNTHETIC_NO_NETWORK"
  "OBS_E2E_GUARD_TEST_MODE=SYNTHETIC_NO_NETWORK"
  "OBS_E2E_GUARD_TEST_DOCKER_CLI=$work_dir/bin/docker"
  "OBS_E2E_GUARD_TEST_POLICY_FILE=$work_dir/policy/formal-port-denylist"
  "OBS_E2E_GUARD_TEST_TRUST_ROOT=$work_dir"
  "OBS_E2E_ENVIRONMENT=test"
  "OBS_E2E_COMPOSE_PROJECT=contract-review-code-dev"
  "OBS_E2E_CONFIRM=contract-review-code-dev"
  "OBS_E2E_ORIGIN=http://127.0.0.1:$port"
  "OBS_E2E_ALLOWED_ORIGINS=http://127.0.0.1:$port"
  "OBS_E2E_SECRETS_ROOT=$secrets_root"
  "OBS_E2E_BEARER_FILE=$secrets_root/bearer"
  "OBS_E2E_ARGV_SAFETY_CURSOR_FILE=$secrets_root/cursor"
  "OBS_E2E_ARGV_SAFETY_IDEMPOTENCY_KEY_FILE=$secrets_root/idempotency"
  "OBS_E2E_ARGV_SAFETY_LOCATOR_FILE=$secrets_root/locator"
  "OBS_E2E_GATEWAY_CONTAINER=$gateway_id"
)
setsid /usr/bin/env \
  -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY \
  -u DOCKER_CERT_PATH -u DOCKER_CONFIG \
  "${probe_environment[@]}" \
  "$probe" >"$stdout_file" 2>"$stderr_file" &
probe_pid=$!

for _ in {1..100}; do
  if capture_process_identity "$probe_pid"; then
    probe_identity_captured=1
    break
  fi
  [[ -e /proc/$probe_pid ]] || break
  sleep 0.01
done
if [[ "$probe_identity_captured" -ne 1 ]]; then
  echo "OBS_E2E_PROBE_SAFETY_PROCESS_CAPTURE_FAILED" >&2
  exit 1
fi
verify_process_identity "$probe_pid"

"$PYTHON_CLI" - \
  "$probe_pid" "$secrets_root/bearer" "$secrets_root/cursor" \
  "$secrets_root/idempotency" "$secrets_root/locator" <<'PY'
from pathlib import Path
import sys
import time

probe_pid = int(sys.argv[1])
forbidden = [Path(path).read_bytes().rstrip(b"\n") for path in sys.argv[2:]]
if any(not value for value in forbidden):
    raise SystemExit("OBS_E2E_PROBE_SAFETY_CANARY_INVALID")

seen = False
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    if not Path(f"/proc/{probe_pid}").exists():
        break
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw_stat = (entry / "stat").read_text(encoding="ascii")
            fields = raw_stat.split(") ", 1)[1].split()
            if int(fields[2]) != probe_pid or int(fields[3]) != probe_pid:
                continue
            raw_cmdline = (entry / "cmdline").read_bytes()
        except (OSError, IndexError, ValueError):
            continue
        parts = [item for item in raw_cmdline.split(b"\0") if item]
        if not parts or parts[0] != b"/usr/bin/curl":
            continue
        seen = True
        if len(parts) < 2 or parts[1] != b"-q":
            raise SystemExit("OBS_E2E_PROBE_SAFETY_CURL_Q_NOT_FIRST")
        if any(value in raw_cmdline for value in forbidden):
            raise SystemExit("OBS_E2E_PROBE_SAFETY_ARGV_SECRET_LEAKED")
        if b"://" in raw_cmdline or b"Authorization:" in raw_cmdline:
            raise SystemExit("OBS_E2E_PROBE_SAFETY_ARGV_REQUEST_LEAKED")
    time.sleep(0.002)

if not seen:
    raise SystemExit("OBS_E2E_PROBE_SAFETY_CURL_NOT_OBSERVED")
PY

set +e
wait "$probe_pid"
probe_status=$?
set -e
forget_process "$probe_pid"
probe_identity_captured=0
probe_pid=""
if [[ "$probe_status" -ne 0 ]]; then
  echo "OBS_E2E_PROBE_SAFETY_PROBE_FAILED status=$probe_status" >&2
  exit 1
fi

grep -Fq 'OBS_E2E_PROBE_SAFETY_PATH_OK probes=7' "$stdout_file"
[[ ! -s "$fake_bash_log" ]]
[[ ! -e "$shell_marker" ]]
[[ ! -e "$shell_leak" ]]
grep -Fq 'argv_canaries=True' "$request_log"
if grep -Fq 'injected=True' "$request_log"; then
  echo "OBS_E2E_PROBE_SAFETY_REQUEST_INVALID" >&2
  exit 1
fi
if [[ -s "$fake_curl_log" ]]; then
  echo "OBS_E2E_PROBE_SAFETY_PATH_CURL_EXECUTED" >&2
  exit 1
fi
if [[ -e "$work_dir/curlrc-owned" ]]; then
  echo "OBS_E2E_PROBE_SAFETY_CURLRC_LOADED" >&2
  exit 1
fi

"$PYTHON_CLI" - \
  "$secrets_root/bearer" "$secrets_root/cursor" \
  "$secrets_root/idempotency" "$secrets_root/locator" \
  "$stdout_file" "$stderr_file" "$request_log" <<'PY'
from pathlib import Path
import sys

values = [Path(path).read_bytes().rstrip(b"\n") for path in sys.argv[1:5]]
outputs = b"".join(Path(path).read_bytes() for path in sys.argv[5:])
if any(value in outputs for value in values):
    raise SystemExit("OBS_E2E_PROBE_SAFETY_OUTPUT_SECRET_LEAKED")
PY

grep -Fxq 'readonly CURL_CLI="/usr/bin/curl"' "$probe"
grep -Fq '"$CURL_CLI" -q --config "$config" --config "$request_config"' "$probe"
grep -Fq '"$CURL_CLI" -q --config "$curl_config" --config "$stream_config"' "$probe"
grep -Fq 'proto = "=http,https"' "$probe"
grep -Fq 'proto-redir = "=http,https"' "$probe"
grep -Fq 'max-redirs = 0' "$probe"
grep -Fq '2>"$curl_error"' "$probe"
if grep -Fq -- '--location' "$probe" ||
  grep -Fq -- 'args+=(-L' "$probe"; then
  echo "OBS_E2E_PROBE_SAFETY_REDIRECT_ENABLED" >&2
  exit 1
fi
if find /tmp -maxdepth 1 -type d -name 'obs70-e2e-??????' -print -quit |
  grep -q .; then
  echo "OBS_E2E_PROBE_SAFETY_TEMP_RESIDUE" >&2
  exit 1
fi

echo "OBS_E2E_PROBE_SAFETY_OK curl_q=first argv=secret-safe xtrace=ignored-at-entry curlrc=ignored redirects=disabled absolute_curl=trusted python=isolated loader-overrides=denied secret_root=bound process_identity=drift-denied temp=clean shell=fixed-two-stage bash-env=ignored path-bash=denied"
