# Business Workflow Kit

This service contains the versioned scene catalog and deterministic audit core
adapted from the running `baoxiao`/Huatai source snapshot (2026-09-18), together
with the platform-file adapters mounted by the Framework API. The Huatai manifest remains a
customer-specific example and must not be exposed as a Zhongliang business
process without a reviewed scene and customer dictionaries.

The portable core pieces here are `SceneRegistry`, `EvidenceCatalog`, `AuditRunner`,
exact-decimal installment checks, and `review_reimbursement_facts` for duplicate
invoice and same-day lodging review. These core functions do not read files, call a model,
persist drafts, reserve quota, approve payments, or submit orders. In
particular, a missing historical amount yields `PENDING`, never a synthetic
zero or a passing decision.

New business uploads reuse the platform attachment service. `workflow_audit_api`
consumes an authorized platform file ID, reads parsed text/chunks, and calls the
model for classified business facts with server-bound evidence IDs. Plain-text
extraction cannot establish visual signatures or stamps. `travel_file_api` adapts
parsed travel PDFs; scanned documents still require a working MinerU service.
Legacy image extraction is retained for compatibility, not the default upload path.
The `invoice_history_complete` input
must come from a verified ledger adapter, not an agent argument. Do not accept
document positions, amounts or approval results supplied by the model as
authoritative facts.

The `sync_consumers.py` generator now emits identical scene snapshots to the
frontend scene registry and Java admission registry. Java owns drafts, versions,
form/attachment relationships, exact-amount checks, quota reservations and local
completion. Frontend hosts the scene form and audit panels. No external approval,
booking or payment is executed by local completion.

The inherited Huatai completion policy allows `PENDING` advisory checks and blocks
unresolved `RISK` checks. A completable record is therefore not necessarily an
all-pass audit. The UI must retain pending findings and describe this distinction.
Tightening that business policy requires a separate reviewed decision.

The platform integration has been browser-tested with explicitly synthetic
contract/invoice/acceptance text fixtures. This does not establish real invoice
authenticity, signature validity, OCR accuracy or production acceptance.

Local checks:

```powershell
$env:PYTHONPATH='E:\中粮\aituge-main\services\business-workflow-kit'
python -m pytest --confcutdir=services/business-workflow-kit/tests -q services/business-workflow-kit/tests
```
