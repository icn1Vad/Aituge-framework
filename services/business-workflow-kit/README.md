# Business Workflow Kit (migration foundation)

This service contains the versioned scene catalog and deterministic audit core
adapted from the running `baoxiao`/Huatai source snapshot (2026-09-18). It is
not yet a deployed reimbursement workflow. The Huatai manifest remains a
customer-specific example and must not be exposed as a Zhongliang business
process without a reviewed scene and customer dictionaries.

The portable pieces here are `SceneRegistry`, `EvidenceCatalog`, `AuditRunner`,
exact-decimal installment checks, and `review_reimbursement_facts` for duplicate
invoice and same-day lodging review. They do not read files, call a model,
persist drafts, reserve quota, approve payments, or submit orders. In
particular, a missing historical amount yields `PENDING`, never a synthetic
zero or a passing decision.

The older Huatai DeepSeek/PDFium vision path is deliberately not copied. New
attachment upload, parsing and the local extraction model stay in the current
platform attachment service. A future reimbursement tool must obtain
versioned, authorized attachment facts from that service and bind evidence IDs
server-side before invoking this audit core. The `invoice_history_complete` input
must come from a verified ledger adapter, not an agent argument. Do not accept
document positions, amounts or approval results supplied by the model as
authoritative facts.

The `sync_consumers.py` generator now emits identical scene snapshots to the
frontend scene registry and Java admission registry. These registries validate
and display only; the generated files do not enable a scene, persist a draft,
or add a route. The Python package is included in both Framework Docker build
paths, but no workflow capability is mounted by the platform Compose yet.

Local checks:

```powershell
$env:PYTHONPATH='E:\中粮\aituge-main\services\business-workflow-kit'
python -m pytest --confcutdir=services/business-workflow-kit/tests -q services/business-workflow-kit/tests
```
