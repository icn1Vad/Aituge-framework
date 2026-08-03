#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
readonly REPO_ROOT="$(repo_root_from_probe)"
readonly STACK_DIR="${REPO_ROOT}/infrastructure/observability-test"
readonly OBS_COMPOSE="${STACK_DIR}/compose.observability-test.yml"
page6_verify_docker_daemon || probe_fail "COMPOSE_DOCKER_TRUST_REJECTED"
for key in COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR COMPOSE_ENV_FILES; do
    [[ ! -v $key ]] || probe_fail "COMPOSE_ENVIRONMENT_OVERRIDE_REJECTED"
done
create_probe_dir
probe_local_cleanup() {
    cleanup_probe_dir
}
probe_install_cleanup_traps probe_local_cleanup

if env -u CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT     -u CONTRACT_REVIEW_DEV_TEST_SECRETS_DIR     -u CONTRACT_REVIEW_DEV_GRAFANA_ADMIN_USER     "$PAGE6_DOCKER_BIN" --context "$PAGE6_DOCKER_CONTEXT" compose --env-file /dev/null -f "$OBS_COMPOSE" config     >/dev/null 2>&1; then
    probe_fail "COMPOSE_MISSING_OBSERVABILITY_INPUT_ACCEPTED"
fi

readonly SECRET_FIXTURE="${PROBE_DIR}/secrets"
readonly LOG_FIXTURE="${PROBE_DIR}/capture"
mkdir -m 700 -- "$SECRET_FIXTURE" "$LOG_FIXTURE"
for leaf in deepseek_api_key dashscope_api_key grafana_admin_password; do
    : > "${SECRET_FIXTURE}/${leaf}"
    chmod 600 -- "${SECRET_FIXTURE}/${leaf}"
done
[[ "$TEST_SECRETS_DIR" == \
    "/home/aituge/contract-review-code-dev-test-private/secrets" ]] ||
    probe_fail "COMPOSE_SECRET_EXACT_ROOT_CHANGED"
validate_test_secret_fixture_directory "$SECRET_FIXTURE" >/dev/null
if validate_test_secret_directory "$SECRET_FIXTURE" >/dev/null 2>&1; then
    probe_fail "COMPOSE_SECRET_FIXTURE_ACCEPTED_AS_DEPLOYMENT_ROOT"
fi
ln -- "${SECRET_FIXTURE}/deepseek_api_key" \
    "${SECRET_FIXTURE}/deepseek_api_key.hardlink"
if validate_test_secret_fixture_directory "$SECRET_FIXTURE" >/dev/null 2>&1; then
    probe_fail "COMPOSE_SECRET_HARDLINK_ACCEPTED"
fi
unlink -- "${SECRET_FIXTURE}/deepseek_api_key.hardlink"
validate_test_secret_fixture_directory "$SECRET_FIXTURE" >/dev/null
validate_private_directory     "$LOG_FIXTURE" "$TEST_OBSERVABILITY_PARENT"     "COMPOSE_LOG_FIXTURE_INVALID" >/dev/null

readonly RENDERED_JSON="${PROBE_DIR}/observability.json"
page6_docker compose --env-file /dev/null -f "$OBS_COMPOSE" config --no-interpolate --format json > "$RENDERED_JSON"

python3 - "$RENDERED_JSON" "${STACK_DIR}/grafana/dashboards/test-observability.json" <<'PY'
import json
import pathlib
import sys

config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))

expected_images = {
    "loki": "${CONTRACT_REVIEW_DEV_LOKI_IMAGE:?must be approved grafana/loki:3.7.2@sha256 digest}",
    "alloy": "${CONTRACT_REVIEW_DEV_ALLOY_IMAGE:?must be approved grafana/alloy:v1.18.0@sha256 digest}",
    "tempo": "${CONTRACT_REVIEW_DEV_TEMPO_IMAGE:?must be approved grafana/tempo:2.10.5@sha256 digest}",
    "otel-collector": "otel/opentelemetry-collector-contrib:0.153.0@sha256:666fb40ee1391aa9f0eddb06ea143ffe215d731ff02cf13ce4c2f44f2a3bf89a",
    "prometheus": "prom/prometheus:v3.13.0@sha256:b96d6068885d3045ae74e149890aa574948f6fcaa6005b82171c6b2be7947998",
    "alertmanager": "${CONTRACT_REVIEW_DEV_ALERTMANAGER_IMAGE:?must be approved prom/alertmanager:v0.32.1@sha256 digest}",
    "grafana-secret-init": "${CONTRACT_REVIEW_DEV_GRAFANA_IMAGE:?must be approved grafana/grafana:13.1.0@sha256 digest}",
    "grafana": "${CONTRACT_REVIEW_DEV_GRAFANA_IMAGE:?must be approved grafana/grafana:13.1.0@sha256 digest}",
}
if config.get("name") != "contract-review-code-dev-observability":
    raise SystemExit("COMPOSE_PROJECT_NAME_INVALID")
services = config["services"]
if set(services) != set(expected_images):
    raise SystemExit("COMPOSE_SERVICE_SET_INVALID")
for name, image in expected_images.items():
    service = services[name]
    if service.get("image") != image:
        raise SystemExit("COMPOSE_IMAGE_PIN_INVALID")
    if service.get("pull_policy") != "never":
        raise SystemExit("COMPOSE_PULL_POLICY_INVALID")
    if service.get("labels", {}).get("com.aituge.environment") != "test":
        raise SystemExit("COMPOSE_TEST_LABEL_MISSING")
    logging = service.get("logging", {})
    if logging.get("driver") != "json-file":
        raise SystemExit("COMPOSE_LOG_DRIVER_INVALID")
    options = logging.get("options", {})
    if options != {"compress": "true", "max-file": "5", "max-size": "20m"}:
        raise SystemExit("COMPOSE_LOG_ROTATION_INVALID")

expected_resources = {
    "loki": ("1g", "1.0"),
    "alloy": ("256m", "0.25"),
    "tempo": ("1g", "1.0"),
    "otel-collector": ("512m", "0.5"),
    "prometheus": ("768m", "0.5"),
    "alertmanager": ("128m", "0.1"),
    "grafana-secret-init": ("64m", "0.1"),
    "grafana": ("512m", "0.5"),
}
for name, (mem_limit, cpus) in expected_resources.items():
    service = services[name]
    if service.get("mem_limit") != mem_limit or service.get("cpus") != cpus:
        raise SystemExit("COMPOSE_RESOURCE_LIMIT_INVALID")

prometheus_command = services["prometheus"].get("command", [])
if any(
    argument == "--web.enable-lifecycle"
    or argument.startswith("--web.enable-lifecycle=")
    for argument in prometheus_command
):
    raise SystemExit("COMPOSE_PROMETHEUS_LIFECYCLE_ENABLED")

for name, service in services.items():
    ports = service.get("ports", [])
    if name == "grafana":
        if ports != ["127.0.0.1:${CONTRACT_REVIEW_DEV_GRAFANA_PORT:-13000}:3000"]:
            raise SystemExit("COMPOSE_GRAFANA_EXPOSURE_INVALID")
    elif ports:
        raise SystemExit("COMPOSE_BACKEND_HOST_EXPOSURE")

networks = config["networks"]
expected_network_properties = {
    "observability-ingest": ("contract-review-code-dev-observability-ingest", True, "ingest"),
    "observability-backend": ("contract-review-code-dev-observability-backend", True, "backend"),
    "observability-loopback": ("contract-review-code-dev-observability-loopback", False, "loopback"),
}
if set(networks) != set(expected_network_properties):
    raise SystemExit("COMPOSE_NETWORK_SET_INVALID")
for name, (expected_name, expected_internal, expected_role) in expected_network_properties.items():
    network = networks[name]
    if (
        network.get("name") != expected_name
        or network.get("internal") is not expected_internal
        or network.get("labels", {}).get("com.aituge.environment") != "test"
        or network.get("labels", {}).get("com.aituge.stack") != "contract-review-code-dev-observability"
        or network.get("labels", {}).get("com.aituge.network.role") != expected_role
    ):
        raise SystemExit("COMPOSE_NETWORK_DEFINITION_INVALID")
expected_networks = {
    "otel-collector": {"observability-ingest", "observability-backend"},
    "loki": {"observability-backend"},
    "alloy": {"observability-backend"},
    "tempo": {"observability-backend"},
    "prometheus": {"observability-backend"},
    "alertmanager": {"observability-backend"},
    "grafana-secret-init": set(),
    "grafana": {"observability-backend", "observability-loopback"},
}
for name, expected in expected_networks.items():
    if set(services[name].get("networks", {})) != expected:
        raise SystemExit("COMPOSE_NETWORK_MEMBERSHIP_INVALID")

secret_init = services["grafana-secret-init"]
if (
    secret_init.get("network_mode") != "none"
    or secret_init.get("user") != "0:0"
    or secret_init.get("read_only") is not True
    or secret_init.get("cap_drop") != ["ALL"]
    or set(secret_init.get("cap_add", [])) != {"CHOWN", "DAC_OVERRIDE", "FOWNER"}
    or secret_init.get("security_opt") != ["no-new-privileges:true"]
):
    raise SystemExit("COMPOSE_GRAFANA_SECRET_INIT_SANDBOX_INVALID")
secret_mounts = {
    item["target"]: item for item in secret_init.get("secrets", [])
}
if set(secret_mounts) != {"/run/secrets/grafana_admin_password"}:
    raise SystemExit("COMPOSE_GRAFANA_SECRET_SOURCE_INVALID")
init_volumes = {
    item["target"]: item for item in secret_init.get("volumes", [])
}
runtime_target = init_volumes.get("/run/grafana-secret", {})
if (
    runtime_target.get("source") != "grafana-secret-runtime"
    or runtime_target.get("read_only") is True
):
    raise SystemExit("COMPOSE_GRAFANA_SECRET_INIT_VOLUME_INVALID")
init_script = "\n".join(secret_init.get("command", []))
for required in (
    "chown 472:0", "chmod 0400", "stat -c", "/run/grafana-secret",
):
    if required not in init_script:
        raise SystemExit("COMPOSE_GRAFANA_SECRET_INIT_COMMAND_INVALID")

grafana = services["grafana"]
if (
    grafana.get("user") != "472:0"
    or grafana.get("environment", {}).get("GF_SECURITY_ADMIN_PASSWORD__FILE")
        != "/run/grafana-secret/grafana_admin_password"
):
    raise SystemExit("COMPOSE_GRAFANA_RUNTIME_USER_INVALID")
grafana_volumes = {
    item["target"]: item for item in grafana.get("volumes", [])
}
runtime_mount = grafana_volumes.get("/run/grafana-secret", {})
if (
    runtime_mount.get("source") != "grafana-secret-runtime"
    or runtime_mount.get("read_only") is not True
):
    raise SystemExit("COMPOSE_GRAFANA_SECRET_RUNTIME_MOUNT_INVALID")
if grafana.get("secrets"):
    raise SystemExit("COMPOSE_GRAFANA_DIRECT_SECRET_MOUNT_FORBIDDEN")
if grafana.get("depends_on", {}).get("grafana-secret-init", {}).get(
    "condition"
) != "service_completed_successfully":
    raise SystemExit("COMPOSE_GRAFANA_SECRET_DEPENDENCY_INVALID")

for volume in config.get("volumes", {}).values():
    if not volume.get("name", "").startswith("contract-review-code-dev-"):
        raise SystemExit("COMPOSE_VOLUME_NAME_INVALID")

# Image policy is evaluated only against parsed image references. Bind-source
# paths and release directory names are not image tags.
for service in services.values():
    image = service.get("image")
    if not isinstance(image, str) or not image:
        raise SystemExit("COMPOSE_IMAGE_REFERENCE_MISSING")
    tagged_name = image.split("@", 1)[0].rsplit("/", 1)[-1]
    if ":" not in tagged_name or tagged_name.rsplit(":", 1)[1] == "latest":
        raise SystemExit("COMPOSE_FORBIDDEN_IMAGE_TAG")

serialized = json.dumps(config, sort_keys=True)
for forbidden in (
    "/var/run/docker.sock",
    "/var/lib/docker/containers",
):
    if forbidden in serialized:
        raise SystemExit("COMPOSE_FORBIDDEN_RESOURCE")
PY

probe_pass "COMPOSE_VALIDATION_OK"
