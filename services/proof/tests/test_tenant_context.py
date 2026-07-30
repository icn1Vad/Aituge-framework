from __future__ import annotations

from fastapi.testclient import TestClient

from proof.api.app import create_app
from proof.config import Settings
from proof.model_pack import current_model_pack_id
from proof.tenant import current_tenant_id, tenant_storage_key


class TenantEchoService:
    def health(self):
        return {"ok": True}

    def policy_metadata(self):
        return {"levels": [{"tenant_id": current_tenant_id()}]}


def test_v1_rejects_missing_tenant_header() -> None:
    assert "default_tenant_id" not in Settings.model_fields
    client = TestClient(create_app(Settings(_env_file=None), TenantEchoService()))

    response = client.get("/v1/policies/metadata")

    assert response.status_code == 400
    assert response.json()["error"] == "tenant_id_required"


def test_v1_uses_explicit_java_tenant_header() -> None:
    client = TestClient(create_app(Settings(_env_file=None), TenantEchoService()))

    response = client.get("/v1/policies/metadata", headers={"X-Tenant-ID": "2"})

    assert response.status_code == 200
    assert response.json()["data"] == {"levels": [{"tenant_id": "2"}]}
    assert tenant_storage_key("1") != tenant_storage_key("2")


def test_v1_rejects_java_null_tenant_value() -> None:
    client = TestClient(create_app(Settings(_env_file=None), TenantEchoService()))

    response = client.get(
        "/v1/policies/metadata",
        headers={"X-Tenant-ID": "null"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "tenant_id_required"


def test_health_does_not_require_tenant_context() -> None:
    settings = Settings(_env_file=None)
    client = TestClient(create_app(settings, TenantEchoService()))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["data"] == {"ok": True}


def test_v1_binds_explicit_model_pack_without_exposing_it_as_tool_input() -> None:
    class ContextEchoService:
        def policy_metadata(self):
            return {"levels": [{
                "tenant_id": current_tenant_id(),
                "model_pack_id": current_model_pack_id(),
            }]}

    client = TestClient(
        create_app(
            Settings(_env_file=None, model_pack_id="api-rerank"),
            ContextEchoService(),
        )
    )

    response = client.get(
        "/v1/policies/metadata",
        headers={
            "X-Tenant-ID": "2",
            "X-Model-Pack-ID": "local-rerank",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "levels": [{"tenant_id": "2", "model_pack_id": "local-rerank"}]
    }


def test_v1_rejects_unknown_model_pack_before_business_service_call() -> None:
    client = TestClient(
        create_app(
            Settings(_env_file=None, model_pack_id="api-rerank"),
            TenantEchoService(),
        )
    )

    response = client.get(
        "/v1/policies/metadata",
        headers={
            "X-Tenant-ID": "1",
            "X-Model-Pack-ID": "missing-pack",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_model_pack_id"


def test_v1_ai_mode_selects_model_pack_and_overrides_internal_pack_header() -> None:
    class ContextEchoService:
        def policy_metadata(self):
            return {"levels": [{"model_pack_id": current_model_pack_id()}]}

    client = TestClient(
        create_app(
            Settings(_env_file=None, model_pack_id="api-rerank"),
            ContextEchoService(),
        )
    )

    response = client.get(
        "/v1/policies/metadata",
        headers={
            "X-Tenant-ID": "2",
            "X-AI-Mode": "private",
            "X-Model-Pack-ID": "api-rerank",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "levels": [{"model_pack_id": "api-rerank-similarity"}]
    }


def test_v1_rejects_unknown_ai_mode() -> None:
    client = TestClient(create_app(Settings(_env_file=None), TenantEchoService()))

    response = client.get(
        "/v1/policies/metadata",
        headers={"X-Tenant-ID": "1", "X-AI-Mode": "secret"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_ai_mode"
