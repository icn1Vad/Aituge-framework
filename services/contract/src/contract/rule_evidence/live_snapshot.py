"""Read the database snapshot frozen by Java when the business task was created."""
from __future__ import annotations

import hashlib
import json

import httpx

from contract.rule_evidence.java_snapshot import JavaRuleLibrarySnapshot
from contract.rule_evidence.snapshot import LocalRuleSnapshot


class FrozenJavaSnapshot(LocalRuleSnapshot):
    def __init__(self, wire: JavaRuleLibrarySnapshot):
        self.rules = tuple(wire.rules)
        self.as_of_date = wire.as_of_date
        self.manifest = {"source_version": wire.source_version,
                         "snapshot_hash": wire.snapshot_hash, "record_count": len(wire.rules)}


class JavaRuleSnapshotClient:
    def __init__(self, base_url, token, *, transport=None, max_bytes=32 * 1024 * 1024):
        if not base_url or not token:
            raise ValueError("Java rule snapshot connection is required")
        self.base_url, self.token = base_url.rstrip("/"), token
        self.transport, self.max_bytes = transport, max_bytes

    async def load(self, business_task_id, tenant_id):
        # Task id is taken from the trusted workflow, never a caller-supplied URL.
        if not str(business_task_id).isdigit() or not str(tenant_id).isdigit():
            raise ValueError("Invalid rule snapshot identity")
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            async with client.stream("GET", self.base_url + "/internal/contract-review-rule-snapshots/" + str(business_task_id),
                headers={"X-Internal-Service": "aituge-framework", "X-Internal-Token": self.token,
                         "X-Tenant-Id": str(tenant_id)}) as response:
                response.raise_for_status()
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > self.max_bytes:
                        raise ValueError("Rule snapshot exceeds size limit")
        data = json.loads(payload)
        # Verify the original JSON field ordering used by Java's ObjectMapper.
        wire = JavaRuleLibrarySnapshot.model_validate(data)
        if wire.tenant_id != str(tenant_id):
            raise ValueError("Rule snapshot tenant mismatch")
        source = [data["schema_version"], data["source_version"], data["tenant_id"], data["as_of_date"], data["rules"]]
        digest = "sha256:" + hashlib.sha256(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if digest != wire.snapshot_hash:
            raise ValueError("Rule snapshot hash mismatch")
        return FrozenJavaSnapshot(wire)
