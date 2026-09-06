"""Opt-in, private local diagnostics. Never send model text to public callbacks/logs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid
import logging
from datetime import datetime, timezone
from typing import Any


def validation_issues(error: Any, stage: str) -> list[dict[str, Any]]:
    """Keep every error location, but not Pydantic's embedded input/exception objects."""
    if hasattr(error, "errors"):
        return [{"stage": stage, "path": list(item["loc"]),
                 "type": item["type"], "message": item["msg"]}
                for item in error.errors(include_input=False, include_url=False, include_context=False)]
    return [{"stage": stage, "path": [], "type": type(error).__name__, "message": str(error)}]


def write_private_diagnostic(kind: str, identity: dict, payload: dict) -> str | None:
    try:
        return _write_private_diagnostic(kind, identity, payload)
    except (OSError, ValueError, TypeError) as exc:
        # A diagnostics disk failure must not overwrite the original review error.
        # No paths, credentials, model output or contract text enter this log.
        logging.getLogger(__name__).error("PRIVATE_REVIEW_DIAGNOSTIC_WRITE_FAILED type=%s", type(exc).__name__)
        return None


def _write_private_diagnostic(kind: str, identity: dict, payload: dict) -> str | None:
    """One immutable file per attempt/batch. Disabled unless a protected directory is configured.

    Linux deployment uses a private Docker volume (0700 directories / 0600 files).
    Raw contract excerpts are intentionally restricted to local administrators. Never
    mount this directory in the frontend, commit it, or include its payload in a callback.
    """
    configured = os.environ.get("CONTRACT_REVIEW_DIAGNOSTIC_DIR")
    if not configured:
        return None
    root = Path(configured)
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("Diagnostic directory must be absolute and not a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    record_id = "review-diag-" + uuid.uuid4().hex
    record = {"diagnostic_id": record_id, "schema_version": 1, "kind": kind,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "identity": identity, "payload": payload}
    text = json.dumps(record, ensure_ascii=False, default=str)
    # Defence in depth: only known runtime secrets and credential-shaped strings;
    # full raw model content is otherwise retained for a local, reproducible replay.
    for key, value in os.environ.items():
        if any(word in key.upper() for word in ("TOKEN", "PASSWORD", "API_KEY", "SECRET")) and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~-]+", "Bearer [REDACTED]", text)
    path = root / (record_id + ".json")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    return record_id


def structural_changes(before: Any, after: Any, path: str = "") -> list[str]:
    """Locations only; before/after payloads live in the protected record."""
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        return [change for key in sorted(set(before) | set(after))
                for change in structural_changes(before.get(key), after.get(key), f"{path}/{key}")]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [change for index, (left, right) in enumerate(zip(before, after))
                for change in structural_changes(left, right, f"{path}/{index}")]
    return [path or "/"]
