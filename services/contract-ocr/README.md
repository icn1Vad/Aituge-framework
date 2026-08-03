# Contract OCR

`contract-ocr` is an isolated Python runtime for the contract scan-preprocessing path.

It deliberately does **not** accept contract files or participate in the review flow yet. This bootstrap
image only proves that the pinned PaddleOCR and PaddlePaddle runtime can be built and imported without
changing Java, Framework, OnlyOffice, or the existing text-PDF path.

When the OCR flow is approved, this service will own only the following work:

1. classify PDF pages as native-text, scanned, or mixed;
2. run PP-StructureV3 for OCR, layout, table, and coordinate extraction;
3. return a derived DOCX plus a structured OCR manifest to the Java contract service.

The Java service will remain the owner of contract versions and file storage. The original PDF must never
be overwritten by the OCR-derived DOCX.

## Current bootstrap endpoints

- `GET /health` — image health and installed package versions.
- `GET /v1/internal/ocr/runtime` — verifies the Paddle packages can be imported. It does not download or
  initialize OCR model weights.

The service has no host port mapping. It is reachable only on the test Docker `agent_internal` network as
`http://contract-ocr:18300`.
