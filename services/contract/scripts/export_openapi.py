from __future__ import annotations

import json
from pathlib import Path

from contract.api.app import create_app
from contract.config import Settings


def main() -> None:
    target = Path(__file__).parents[1] / "openapi" / "contract-agent-openapi-v1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    schema = create_app(Settings(internal_auth_enabled=False)).openapi()
    target.write_text(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(target)


if __name__ == "__main__":
    main()
