from __future__ import annotations

import argparse
import json
import os
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
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _multipart(payload: dict) -> tuple[bytes, str]:
    boundary = f"contract-smoke-{uuid.uuid4().hex}"
    parts = [
        (
            "file",
            "contract.pdf",
            "application/pdf",
            b"%PDF-1.4\ncontract review HTTP smoke\n%%EOF\n",
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
    args = parser.parse_args()
    token = os.environ["CONTRACT_INTERNAL_TOKEN"]
    run_id = uuid.uuid4().hex
    request_id = f"req-smoke-{run_id}"
    headers = {
        "X-Internal-Service": "continew-java",
        "X-Internal-Token": token,
        "X-User-Id": "1",
        "X-Tenant-Id": "1",
        "X-Request-Id": request_id,
        "Idempotency-Key": f"idem-smoke-{run_id}",
    }

    code, health = _call(f"{args.base_url}/health", headers={"X-Request-Id": request_id})
    assert code == 200 and health["data"]["status"] == "UP"

    payload = {
        "business_task_id": f"task-{run_id}",
        "contract_version_id": f"version-{run_id}",
        "perspective": "PARTY_B",
        "our_party_name": "某某单位",
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
    assert code == 201
    assert created["data"]["status"] == "CREATED"
    assert created["data"]["framework_task_id"] is None
    review_id = created["data"]["review_id"]

    code, status = _call(f"{args.base_url}/v1/contract-reviews/{review_id}", headers=headers)
    assert code == 200 and status["data"]["status"] == "CREATED"

    code, result = _call(f"{args.base_url}/v1/contract-reviews/{review_id}/result", headers=headers)
    assert code == 409 and result["error"]["code"] == "REVIEW_NOT_READY"

    code, cancelled = _call(
        f"{args.base_url}/v1/contract-reviews/{review_id}/cancel",
        method="POST",
        headers=headers,
        body=b"",
    )
    assert code == 200
    assert cancelled["data"]["status"] == "CANCELLED"
    assert cancelled["data"]["already_terminal"] is False

    code, repeated = _call(
        f"{args.base_url}/v1/contract-reviews/{review_id}/cancel",
        method="POST",
        headers=headers,
        body=b"",
    )
    assert code == 200 and repeated["data"]["already_terminal"] is True

    print("HEALTH=200/UP")
    print("CREATE=201/CREATED")
    print("STATUS=200/CREATED")
    print("RESULT=409/REVIEW_NOT_READY")
    print("CANCEL=200/CANCELLED")
    print("CANCEL_REPEAT=200/IDEMPOTENT")


if __name__ == "__main__":
    main()
