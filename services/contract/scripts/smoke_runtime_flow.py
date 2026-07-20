from __future__ import annotations

import json
import os
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _call(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict]:
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    content = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode("ascii"))
        content.extend(value)
        content.extend(b"\nendobj\n")
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    content.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(content)


def _multipart(payload: dict, content: bytes) -> tuple[bytes, str]:
    boundary = f"contract-runtime-smoke-{uuid.uuid4().hex}"
    body = bytearray()
    for name, filename, content_type, value in (
        ("file", "contract.pdf", "application/pdf", content),
        (
            "request",
            None,
            "application/json",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        ),
    ):
        body.extend(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        body.extend(f"{disposition}\r\nContent-Type: {content_type}\r\n\r\n".encode())
        body.extend(value)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), boundary


def main() -> None:
    base_url = os.getenv(
        "CONTRACT_SMOKE_BASE_URL",
        "http://ai-contract:18200",
    ).rstrip("/")
    token = os.environ["CONTRACT_INTERNAL_TOKEN"]
    marker = uuid.uuid4().hex
    headers = {
        "X-Internal-Service": "continew-java",
        "X-Internal-Token": token,
        "X-User-Id": f"smoke-user-{marker}",
        "X-Tenant-Id": f"smoke-stage5b-{marker}",
        "X-Request-Id": f"smoke-request-{marker}",
        "Idempotency-Key": f"smoke-idempotency-{marker}",
    }
    payload = {
        "business_task_id": f"smoke-business-{marker}",
        "contract_version_id": f"smoke-version-{marker}",
        "perspective": "PARTY_B",
        "our_party_name": "Party B",
        "contract_type": "AUTO",
        "review_attitude": "NEUTRAL",
        "schema_version": "1.0",
    }
    body, boundary = _multipart(
        payload,
        _pdf_bytes("Party A supplies services. Party B pays after acceptance."),
    )
    code, created = _call(
        f"{base_url}/v1/contract-reviews",
        method="POST",
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        body=body,
    )
    assert code == 201, created
    data = created["data"]
    assert data["status"] == "RUNNING", data
    assert data["framework_attempt_no"] == 1
    assert data["framework_task_id"] and data["framework_run_id"]

    status = None
    for _ in range(15):
        code, response = _call(
            f"{base_url}/v1/contract-reviews/{data['review_id']}",
            headers=headers,
        )
        assert code == 200, response
        status = response["data"]
        if status["status"] != "RUNNING" or status["current_stage"] != "PARSING":
            break
        time.sleep(1)
    assert status is not None
    assert status["framework_attempt_no"] == 1
    assert status["framework_task_id"] == data["framework_task_id"]
    assert status["framework_run_id"] == data["framework_run_id"]
    assert status["current_stage"] in {
        "PARTY_RESOLUTION",
        "IR_EXTRACTION",
        "RIGHTS_OBLIGATIONS",
        "RISK_REVIEW",
        "EVIDENCE_VERIFICATION",
        "FINALIZING",
    }
    print("CONTRACT_RUNTIME_FLOW=OK")
    print(f"REVIEW_STATUS={status['status']}")
    print(f"CURRENT_STAGE={status['current_stage']}")
    print("FRAMEWORK_MAPPING=COMPLETE")


if __name__ == "__main__":
    main()
