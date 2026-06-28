from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
SINGLE_AGENT_DIR = BACKEND_DIR / "single-agent"

for path in reversed((ROOT_DIR, BACKEND_DIR, SINGLE_AGENT_DIR)):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
