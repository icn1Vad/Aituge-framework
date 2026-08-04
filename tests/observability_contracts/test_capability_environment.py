from __future__ import annotations

import os
import subprocess

from .contract_loader import repository_root


SCRIPTS = repository_root() / "scripts" / "observability-contracts"
LOADER_OVERRIDE_VARIABLES = (
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONUSERBASE",
)


def _script_environment() -> dict[str, str]:
    env = os.environ.copy()
    for variable in LOADER_OVERRIDE_VARIABLES:
        env.pop(variable, None)
    return env


def _run(
    script: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    effective_env = _script_environment() if env is None else env
    return subprocess.run(
        [str(SCRIPTS / script), *args],
        check=False,
        capture_output=True,
        text=True,
        env=effective_env,
    )


def test_target_guard_uses_authoritative_synthetic_safety_matrix() -> None:
    result = _run("test-e2e-guard-safety.sh")
    assert result.returncode == 0, result.stderr
    assert "OBS_E2E_GUARD_SAFETY_OK" in result.stdout


def test_e2e_probe_refuses_by_default_without_network_access() -> None:
    env = _script_environment()
    env.pop("OBS_E2E_RUN", None)
    result = _run("e2e-observability-probe.sh", env=env)
    assert result.returncode != 0
    assert "OBS_E2E_DEFAULT_REFUSAL" in result.stderr


def test_probe_contains_no_deploy_or_database_mutation_command() -> None:
    probe = (SCRIPTS / "e2e-observability-probe.sh").read_text(encoding="utf-8")
    lowered = probe.lower()
    for forbidden in [
        "docker ",
        "docker-compose",
        "kubectl ",
        "psql ",
        "mysql ",
        "git push",
        "ssh ",
    ]:
        assert forbidden not in lowered


def test_e2e_requires_container_evidence_and_private_bearer_file() -> None:
    probe = (SCRIPTS / "e2e-observability-probe.sh").read_text(encoding="utf-8")
    guard = (SCRIPTS / "guard-test-target.sh").read_text(encoding="utf-8")
    reader = (SCRIPTS / "read-private-e2e-secret.py").read_text(encoding="utf-8")
    assert "OBS_E2E_GATEWAY_CONTAINER" in probe
    assert probe.count('noproxy = "*"') == 2
    assert (
        'secret_reader="$repo_root/scripts/observability-contracts/'
        'read-private-e2e-secret.py"'
    ) in probe
    assert '"$PYTHON_CLI" -I -B "$secret_reader"' in probe
    assert "os.O_NOFOLLOW" in reader
    assert "before = os.fstat(secret_fd)" in reader
    assert "stat.S_IMODE(before.st_mode) != 0o600" in reader
    assert probe.splitlines()[1] == "set +x"
    assert 'readonly CURL_CLI="/usr/bin/curl"' in probe
    assert (
        'readonly CURL_CLI_SHA256="'
        '459c937b69bd76620d6d01100a0793294349c89871f4474d398efdae49fcee09"' in probe
    )
    assert 'verify_fixed_binary "$CURL_CLI" "$CURL_CLI_SHA256" curl || exit 80' in probe
    assert "OBS_E2E_EXPORT_WRITE_CONFIRMATIONS_REQUIRED" in probe
    assert "EVENT_STREAM_REPLAY_GAP" in probe
    assert "EVENT_STREAM_RESET_REQUIRED" in probe
    assert "stat.S_IMODE(root_metadata.st_mode) != 0o700" in reader
    assert 'fail("OBS_E2E_SECRET_ANCESTOR_INVALID", label)' in reader
    assert "OBS_E2E_GUARD_TEST_MODE" in probe
    assert "OBS_E2E_GUARD_TEST_POLICY_FILE" in probe
    assert "OBS_E2E_GUARD_TEST_TRUST_ROOT" in probe
    request_body = probe.split("request() {", 1)[1].split("assert_header() {", 1)[0]
    stream_section = probe.split('stream_config="$work_dir/sse-live.request.conf"', 1)[
        1
    ].split('rm -f -- "$stream_config" "$stream_error"', 1)[0]
    stream_body = stream_section.split('} >"$stream_config"', 1)[0]
    request_call = (
        'status=$("$CURL_CLI" -q --config "$config" '
        '--config "$request_config" 2>"$curl_error")'
    )
    stream_call = (
        'stream_status=$("$CURL_CLI" -q --config "$curl_config" '
        '--config "$stream_config" 2>"$stream_error")'
    )
    assert 'noproxy = "*"' in request_body
    assert 'proto = "=http,https"' in request_body
    assert 'proto-redir = "=http,https"' in request_body
    assert "max-redirs = 0" in request_body
    assert 'noproxy = "*"' in stream_body
    assert 'proto = "=http,https"' in stream_body
    assert 'proto-redir = "=http,https"' in stream_body
    assert "max-redirs = 0" in stream_body
    assert request_call in request_body
    assert request_body.index("verify_target") < request_body.index(request_call)
    assert stream_call in stream_section
    assert stream_section.index("verify_target") < stream_section.index(stream_call)
    assert "OBS_E2E_FORBIDDEN_PORTS" not in probe
    assert "guard_docker inspect --format" in guard
    assert "^[0-9a-f]{64}$" in guard
    assert '[[ "$gateway_container" != "$actual_id" ]]' in guard
    assert "com.docker.compose.project" in guard
    assert "com.aituge.environment" in guard
    assert "SYSTEM_FORBIDDEN_PORTS_FILE" in guard
    assert "OBS_E2E_GUARD_POLICY_REQUIRED" in guard
    assert "OBS_E2E_GUARD_POLICY_EMPTY" in guard
    assert "OBS_E2E_GUARD_POLICY_INVALID" in guard
    assert "HostIp" in guard
    assert "HostPort" in guard
    assert "OBS_E2E_GUARD_ORIGIN_CONTAINER_MISMATCH" in guard
    assert "getent " not in guard
    assert "OBS_E2E_GUARD_ORIGIN_HOST_LITERAL_REQUIRED" in guard
    assert "lower_actual_name" in guard
