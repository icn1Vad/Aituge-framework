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
idempotency, Attempt recovery, and cancellation races. Contract Capability registration
and the dedicated Result Sink are enabled in later implementation stages without
changing the frozen public HTTP contract.
