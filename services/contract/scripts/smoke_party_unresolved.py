from __future__ import annotations

import argparse
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
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _text_pdf(text: str) -> bytes:
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
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
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


def _multipart(payload: dict) -> tuple[bytes, str]:
    boundary = f"contract-party-smoke-{uuid.uuid4().hex}"
    parts = [
        (
            "file",
            "party-mismatch.pdf",
            "application/pdf",
            _text_pdf(
                "Party A: Acme Company. Party B: Beta Company. "
                "Acme Company supplies services and Beta Company pays the fees."
            ),
        ),
        (
            "request",
            None,
            "application/json",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        ),
    ]
    body = bytearray()
    for name, filename, content_type, content in parts:
        body.extend(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body.extend(f"{disposition}\r\n".encode())
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), boundary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:19200")
    parser.add_argument("--timeout-seconds", type=int, default=360)
    args = parser.parse_args()

    token = os.environ["CONTRACT_INTERNAL_TOKEN"]
    marker = uuid.uuid4().hex
    headers = {
        "X-Internal-Service": "continew-java",
        "X-Internal-Token": token,
        "X-User-Id": f"e2e-user-{marker}",
        "X-Tenant-Id": f"e2e-tenant-{marker}",
        "X-Request-Id": f"e2e-request-{marker}",
        "Idempotency-Key": f"e2e-idempotency-{marker}",
    }
    payload = {
        "business_task_id": f"e2e-party-unresolved-{marker}",
        "contract_version_id": f"e2e-version-{marker}",
        "perspective": "PARTY_B",
        "our_party_name": "Gamma Company",
        "contract_type": "AUTO",
        "review_attitude": "NEUTRAL",
        "schema_version": "1.0",
    }
    body, boundary = _multipart(payload)
    create_headers = dict(headers)
    create_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    code, created = _call(
        f"{args.base_url}/v1/contract-reviews",
        method="POST",
        headers=create_headers,
        body=body,
    )
    assert code == 201, created
    review_id = created["data"]["review_id"]

    deadline = time.monotonic() + args.timeout_seconds
    while True:
        code, status = _call(
            f"{args.base_url}/v1/contract-reviews/{review_id}",
            headers=headers,
        )
        assert code == 200, status
        if status["data"]["status"] in {"FAILED", "SUCCEEDED", "CANCELLED"}:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Review {review_id} did not reach a terminal status")
        time.sleep(3)

    error = status["data"]["error"]
    assert status["data"]["status"] == "FAILED", status
    assert status["data"]["current_stage"] == "PARTY_RESOLUTION", status
    assert error["code"] == "PARTY_UNRESOLVED", status
    assert error["retryable"] is False, status
    assert error["user_action_required"] is True, status
    print(f"REVIEW_ID={review_id}")
    print("STATUS=FAILED/PARTY_RESOLUTION")
    print("ERROR=PARTY_UNRESOLVED/USER_ACTION_REQUIRED")


if __name__ == "__main__":
    main()
