#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_CONFIG_PATH="$ROOT_DIR/localdata/private_config/tuge_runtime_config.private.json"
COMMAND="${1:-}"
CONFIG_PATH="${2:-$DEFAULT_CONFIG_PATH}"

usage() {
  cat <<'EOF'
Usage:
  scripts/tuge_private_config.sh export [output.json]
  scripts/tuge_private_config.sh upload [output.json]
  scripts/tuge_private_config.sh import [input.json]
  scripts/tuge_private_config.sh download [input.json]
  scripts/tuge_private_config.sh inspect [input.json]

Notes:
  - export/upload writes a private local JSON bundle. It does not push secrets
    to GitHub.
  - import/download initializes SQLite and upserts model/tool/agent config.
  - The default path is localdata/private_config/tuge_runtime_config.private.json.
EOF
}

sqlite_path_from_env() {
  "$PYTHON_BIN" - "$ROOT_DIR" <<'PY'
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

root = Path(sys.argv[1])
default = root / "backend" / "single-agent" / "tmp" / "sqlite" / "local.db"
db_url = os.environ.get("SQLITE_URL")
if not db_url:
    print(default)
else:
    parsed = urlparse(db_url)
    if not parsed.scheme.startswith("sqlite"):
        raise SystemExit(f"Only SQLite config export/import is supported, got {parsed.scheme}")
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        path = f"//{parsed.netloc}{path}"
    elif path.startswith("//"):
        path = path[1:]
    if path in {"", "/:memory:"}:
        path = str(default)
    print(path)
PY
}

if [[ -z "$COMMAND" || "$COMMAND" == "-h" || "$COMMAND" == "--help" ]]; then
  usage
  exit 0
fi

PYTHON_BIN="${PYTHON:-python}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python not found: $PYTHON_BIN" >&2
  exit 1
fi

export PYTHONPATH="$ROOT_DIR/backend:$ROOT_DIR/backend/single-agent:${PYTHONPATH:-}"
DB_PATH="$(sqlite_path_from_env)"

export_config() {
  "$PYTHON_BIN" - "$ROOT_DIR" "$DB_PATH" "$CONFIG_PATH" <<'PY'
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
db_path = Path(sys.argv[2]).expanduser()
out_path = Path(sys.argv[3]).expanduser()
tables = ["tuge_llm_model", "tuge_tool_config", "tuge_agent_profile", "tuge_skill_package"]

if not db_path.exists():
    raise SystemExit(f"SQLite database does not exist: {db_path}")

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

payload = {
    "format": "tuge-runtime-config",
    "version": 1,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "source_db": str(db_path),
    "tables": {},
}

for table in tables:
    exists = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?",
        (table,),
    ).fetchone()
    if not exists:
        payload["tables"][table] = []
        continue
    rows = [dict(row) for row in conn.execute(f"select * from {table}")]
    payload["tables"][table] = rows

out_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
tmp_path.replace(out_path)

print(f"Exported private config to {out_path}")
for table, rows in payload["tables"].items():
    print(f"  {table}: {len(rows)} rows")
print("Keep this file private. It may contain encrypted API keys.")
PY
  chmod 600 "$CONFIG_PATH"
}

import_config() {
  "$PYTHON_BIN" - "$ROOT_DIR" "$DB_PATH" "$CONFIG_PATH" <<'PY'
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
db_path = Path(sys.argv[2]).expanduser()
config_path = Path(sys.argv[3]).expanduser()

if not config_path.exists():
    raise SystemExit(f"Private config file does not exist: {config_path}")

os.environ.setdefault("SQLITE_URL", f"sqlite+aiosqlite:///{db_path}")
sys.path.insert(0, str(root / "backend"))
sys.path.insert(0, str(root / "backend" / "single-agent"))

from db.db_context import init_db, reset_engine_for_test  # noqa: E402

reset_engine_for_test()
asyncio.run(init_db())

payload = json.loads(config_path.read_text(encoding="utf-8"))
if payload.get("format") != "tuge-runtime-config":
    raise SystemExit("Unsupported private config format.")

conn = sqlite3.connect(db_path)
tables = payload.get("tables") or {}

for table, rows in tables.items():
    if table not in {"tuge_llm_model", "tuge_tool_config", "tuge_agent_profile", "tuge_skill_package"}:
        print(f"Skipping unsupported table: {table}")
        continue
    rows = rows or []
    if not rows:
        print(f"{table}: 0 rows")
        continue
    table_columns = {
        row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()
    }
    for row in rows:
        columns = [column for column in row.keys() if column in table_columns]
        if not columns:
            continue
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)
        values = [row[column] for column in columns]
        conn.execute(
            f"insert or replace into {table} ({column_sql}) values ({placeholders})",
            values,
        )
    print(f"{table}: imported {len(rows)} rows")

conn.commit()
print(f"Imported private config into {db_path}")
PY
}

inspect_config() {
  "$PYTHON_BIN" - "$CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
payload = json.loads(path.read_text(encoding="utf-8"))
print(f"file: {path}")
print(f"format: {payload.get('format')} v{payload.get('version')}")
print(f"created_at: {payload.get('created_at')}")
for table, rows in (payload.get("tables") or {}).items():
    print(f"{table}: {len(rows or [])} rows")
PY
}

case "$COMMAND" in
  export|upload)
    export_config
    ;;
  import|download)
    import_config
    ;;
  inspect)
    inspect_config
    ;;
  *)
    usage
    exit 2
    ;;
esac
