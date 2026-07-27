# Contract service

`services/contract` is the isolated Contract Python service for contract-review phase 1.
It owns technical file copies, parsing generations, Framework execution mappings, and
validated result snapshots. It does not own Java permissions, business files, or human
review decisions.

The frozen cross-service contract is maintained outside this module in the single
approved Java-Python-Framework phase-1 specification. This README only documents local
development commands.

## Run tests

```bash
python -m pytest -q
```

## Run locally

```bash
python -m uvicorn contract.api.app:app --host 127.0.0.1 --port 18200
```

`CONTRACT_MOCK_MODE=true` selects the protocol-faithful in-memory adapter.
`CONTRACT_MOCK_MODE=false` selects the PostgreSQL-backed runtime service and the
TaskManager v1 HTTP adapter. The runtime already preserves Framework Task/Run
idempotency, Attempt recovery, and cancellation races. The runtime Docker image defaults
to the real PostgreSQL and Framework path; Mock mode must be enabled explicitly.

The `contract-review` Capability mounts the frozen `contract.review.run` task, a
15-stage Pipeline, neutral Agent, 13 Skill packages, four internal tools, and the
required Contract Result Sink. Contract IR extraction is internally implemented as five
validated semantic fragments with pipeline parallelism limited to three. Their outputs
are deterministically merged into the single frozen `extract_contract_ir` Stage, so the
Contract Python status protocol and `schema_version=1.0` remain unchanged.

The runtime LLM used by this capability must allow at least 20,000 output tokens. A
real 93-block DOCX contract demonstrated that the combined rights, obligations, and
prohibitions fragment can be truncated with the Framework model default of 8,000 output
tokens even though the fragment boundaries are otherwise sufficient. This is a
deployment model setting, not a Java-Python protocol field.

`scripts/smoke_runtime_capability.py` verifies the mounted runtime surface, while
`scripts/smoke_runtime_flow.py` creates a real review and checks that its Framework
Task/Run mapping advances beyond parsing.
