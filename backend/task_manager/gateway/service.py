from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .models import GatewayResourceRef


class DataAccessGateway:
    """Validates resource references before TaskManager stores or executes a task.

    This first version is intentionally small: it accepts only resource references,
    not raw paths, raw SQL, credentials, or arbitrary connection data.
    """

    BLOCKED_REFERENCE_KEYS = {
        "path",
        "file_path",
        "sql",
        "connection_string",
        "password",
        "secret",
        "api_key",
    }

    def validate_resource_refs(
        self,
        *,
        user_id: str,
        tenant_id: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_refs = payload.get("resource_refs") or []
        try:
            refs = [GatewayResourceRef.model_validate(item) for item in raw_refs]
        except ValidationError as exc:
            raise ValueError(f"Invalid resource_refs: {exc.errors()}") from exc
        for ref in refs:
            forbidden = self.BLOCKED_REFERENCE_KEYS.intersection(ref.metadata)
            if forbidden:
                keys = ", ".join(sorted(forbidden))
                raise ValueError(f"Resource ref '{ref.id}' contains forbidden metadata keys: {keys}.")
        return [
            {
                **ref.model_dump(),
                "access_scope": {
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                },
            }
            for ref in refs
        ]
