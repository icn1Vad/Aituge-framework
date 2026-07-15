"""Register the media_military master-library Gateway in a TUGE SQLite DB."""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()

    db_path = args.db.expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"SQLite database does not exist: {db_path}")
    base_url = args.base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise SystemExit("--base-url must use http or https")

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "select tenant_id from tuge_tool_config order by id limit 1"
        ).fetchone()
        tenant_id = str(row[0]) if row else "__default_tenant_id__"
        config_json = json.dumps(
            {
                "base_url": base_url,
                "timeout_seconds": args.timeout_seconds,
            }
        )
        connection.execute(
            """
            insert into tuge_tool_config (
                id,
                tenant_id,
                tool_name,
                provider,
                enabled,
                config_json,
                encrypted_secrets_json
            ) values (?, ?, ?, ?, ?, ?, ?)
            on conflict(tenant_id, tool_name, provider) do update set
                enabled = excluded.enabled,
                config_json = excluded.config_json
            """,
            (
                uuid.uuid4().hex,
                tenant_id,
                "media_master_library",
                "media_military_http",
                1,
                config_json,
                "",
            ),
        )

    print(
        "Registered media_master_library/media_military_http "
        f"for tenant {tenant_id} at {base_url}."
    )


if __name__ == "__main__":
    main()
