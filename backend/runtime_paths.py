"""Shared runtime root for the API, Worker, and local code tools."""
import os
from pathlib import Path


def runtime_root() -> Path:
    # Keep the existing override; source.sh supplies the persistent workspace path.
    default = Path(__file__).resolve().parents[2] / "tmp"
    return Path(os.environ.get("AITUGE_TMP_ROOT", default)).expanduser().resolve()
