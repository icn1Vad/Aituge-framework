# AI-framework appliance deployment

This deployment keeps Framework, Proof, Contract Python, and their data services isolated from
the existing appliance workloads.

Remote layout:

```text
/home/aituge/workspace/AI-framework/
├── backend/
├── aituge_model/
├── frontend/
├── pyproject.toml
├── poetry.lock
├── services/proof/
├── infrastructure/postgres/
├── deploy/appliance/
├── runtime/
└── backups/
```

Run all commands from the repository root:

```bash
cp deploy/appliance/.env.example deploy/appliance/.env
cp services/proof/.env.example deploy/appliance/proof.env
# Edit both files before starting the stack. On a versioned appliance release,
# set AI_FRAMEWORK_RUNTIME_ROOT to the absolute shared runtime directory.
docker network inspect proofspace-network >/dev/null 2>&1 || docker network create proofspace-network
docker network inspect agent-internal >/dev/null 2>&1 || docker network create agent-internal
docker build --target dependencies -f deploy/appliance/framework.Dockerfile -t ai-framework-core:base .
docker compose --env-file deploy/appliance/.env -f deploy/appliance/compose.yml config --quiet
docker compose --env-file deploy/appliance/.env -f deploy/appliance/compose.yml up -d --build
docker compose --env-file deploy/appliance/.env -f deploy/appliance/compose.yml ps
```

The stable dependency image is separated from the frequently changing Framework
source layer. After the first dependency build, ordinary source updates only
rebuild the lightweight runtime layer.

Runtime tokens are stored in `deploy/appliance/.env`; provider credentials are
mounted from `aituge_model/config/secrets` into Model Gateway only. Never commit
these values or credential files.

`MODEL_GATEWAY_TOKEN` authenticates Framework, Proof, and Contract requests to
the internal Model Gateway. All public and local model requests use
`http://model-gateway:18300`; provider credentials are not mounted into those
business containers.

`CONTRACT_INTERNAL_TOKEN` must equal ContiNew Java's
`BUSINESS_CONTRACT_AGENT_INTERNAL_TOKEN`. `FRAMEWORK_RESULT_SINK_INTERNAL_TOKEN`
and `CONTRACT_RESULT_SINK_INTERNAL_TOKEN` must contain the same independent
callback secret. Contract Python is reachable only as `http://ai-contract:18200`
on the internal Docker networks; it does not publish a host port.

`AI_FRAMEWORK_RUNTIME_ROOT` points to the persistent host data directory. Keep
that path unchanged between releases so PostgreSQL, Redis, uploaded documents,
and Framework conversation history survive an application upgrade.

`AITUGE_TMP_ROOT` points to the host directory used for generated-code workspaces
and chat image artifacts. Keep it outside the Git checkout; the Framework creates
`code-runs` and date-partitioned `chat-artifacts` directories below it.

## Multiple service capabilities

Framework accepts an explicit JSON list of trusted service entry files through
`AITUGE_CAPABILITY_ENTRIES`. The appliance mounts Proof and the deterministic
Smoke and Contract services independently:

```yaml
environment:
  AITUGE_CAPABILITY_ENTRIES: '["/opt/proof-capabilities/register.py","/opt/smoke-capabilities/register.py","/opt/contract-capabilities/register.py"]'
volumes:
  - ../../services/proof/capabilities:/opt/proof-capabilities:ro
  - ../../services/smoke/capabilities:/opt/smoke-capabilities:ro
  - ../../services/contract/capabilities:/opt/contract-capabilities:ro
```

To add another service, create `services/<name>/capabilities/register.py`, mount
that capability directory read-only, and append its in-container path to the JSON
array. The legacy `AITUGE_CAPABILITY_ENTRY` remains supported when the plural
variable is not configured.

```bash
git fetch origin proof
git switch proof
```

The host endpoints are Framework `:8894`, Proof `:18100`, Smoke `:18200`, and the
host-local Model Gateway diagnostic port `:18300`. Containers on the
existing `proofspace-network` can use `http://ai-framework:8894` and
`http://ai-proof:18100`. Java reaches Contract Python through the shared
`agent-internal` network. PostgreSQL and Redis remain host-local on ports 15432
and 16379.
