# Aituge Framework

Minimal TUGE framework for single-agent execution, tool injection, local RAG,
agent scheduling, and multi-agent discussion tests.

## Quick Start

Use Python 3.11.

```bash
poetry install
redis-server
poetry run uvicorn backend.local_code_chat_app:create_app --factory --host 0.0.0.0 --port 8894
```

Then open:

```text
http://127.0.0.1:8894/
```

The app initializes SQLite automatically at:

```text
backend/single-agent/tmp/sqlite/local.db
```

Default agent profiles are seeded on startup.

## Mount a Service Capability

The framework can load one trusted service-owned registration entry at startup.
For the local Proof service:

```bash
export AITUGE_CAPABILITY_ENTRY=/home/ningrx25/data/aituge-appliance/services/proof/capabilities/register.py
export PROOF_SERVICE_BASE_URL=http://127.0.0.1:18100
poetry run uvicorn backend.local_code_chat_app:create_app --factory --host 0.0.0.0 --port 8894
```

The entry registers `proof.qa.chat`, `proof-qa-agent`, its primary skill, and
the `proof_search` HTTP tool. The framework still executes the task through the
existing TaskManager, Scheduler, and SingleAgent path. In a container, mount the
Proof `capabilities` directory read-only and point `AITUGE_CAPABILITY_ENTRY` at
the mounted `register.py` file.

## Private Runtime Config

Model keys and paid tool keys are not committed to GitHub. Export them from a
configured machine into a private JSON file:

```bash
./scripts/tuge_private_config.sh export
```

This creates:

```text
localdata/private_config/tuge_runtime_config.private.json
```

Share that file privately with a collaborator, then import it on their machine:

```bash
./scripts/tuge_private_config.sh import /path/to/tuge_runtime_config.private.json
```

The private config currently includes these SQLite tables:

- `tuge_llm_model`
- `tuge_tool_config`
- `tuge_agent_profile`

It intentionally excludes chat history, messages, discussion logs, and local
runtime artifacts.

## Useful Commands

```bash
poetry run pytest -q
./scripts/tuge_private_config.sh inspect localdata/private_config/tuge_runtime_config.private.json
```

If Poetry is unavailable, create a Python 3.11 virtual environment and install
the dependencies from `pyproject.toml`.
