# Contract OCR

`contract-ocr` is an isolated Python runtime for the contract scan-preprocessing path.

It is an internal preprocessing service. It classifies every PDF page before a contract reaches the
existing review parser. Native-text PDFs remain on that parser path; scanned or mixed PDFs are converted
to DOCX by PP-StructureV3.

When the OCR flow is approved, this service will own only the following work:

1. classify PDF pages as native-text, scanned, or mixed;
2. run PP-StructureV3 for OCR, layout, table, and coordinate extraction;
3. return a derived DOCX plus a structured OCR manifest to the Java contract service in one ZIP archive.

The Java service will remain the owner of contract versions and file storage. The original PDF must never
be overwritten by the OCR-derived DOCX.

## Current bootstrap endpoints

- `GET /health` — image health and installed package versions.
- `GET /v1/internal/ocr/runtime` — verifies the Paddle packages can be imported. It does not download or
  initialize OCR model weights.
- `POST /v1/internal/ocr/inspect` — multipart field `file`; returns `NATIVE_TEXT`, `OCR_REQUIRED`, or
  `MIXED` and the affected page numbers.
- `POST /v1/internal/ocr/convert` — multipart field `file`; accepts only scanned or mixed PDFs and returns
  `application/zip` containing `contract.docx` and `manifest.json`.

The service has no host port mapping. It is reachable only on the platform's
`ai-internal` network as `http://contract-ocr:18300`. The production image
preloads the fixed PPStructureV3 model set into `/models`, so health checks and
first conversion do not need outbound model downloads.
