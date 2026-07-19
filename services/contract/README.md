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

The public HTTP endpoints still use the protocol-faithful in-memory adapter while the
runtime orchestration is under construction. The module already contains PostgreSQL
migrations and repositories, content-addressed technical file storage, deterministic
PDF/DOCX parsing, Parse Generation drafts, and the typed structural Contract IR.
Framework dispatch and result callbacks are added in later implementation stages
without changing the frozen public HTTP contract.
