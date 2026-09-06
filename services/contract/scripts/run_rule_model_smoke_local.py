"""Launch the isolated test with existing runtime credentials; never print credentials."""
import json
import os
from pathlib import Path
import subprocess

repo = Path(__file__).resolve().parents[3]
workspace = repo.parent
root = workspace.parent / "proofspace-rule-data"
output = root / "execution-20260905"
output.mkdir(exist_ok=True)
details = json.loads(subprocess.check_output(["docker", "inspect", "proofspace-legal-e2e-framework-1"]))[0]
environment = dict(os.environ)
args = ["docker", "run", "--rm", "--network", "proofspace-legal-kg-e2e",
        "--volumes-from", "proofspace-legal-e2e-framework-1:ro",
        "--mount", f"type=bind,source={repo},target=/workspace,readonly",
        "--mount", f"type=bind,source={root / 'xingfa-existing-20260905-01'},target=/rule-data,readonly",
        "--mount", f"type=bind,source={output},target=/rule-output",
        "--mount", "type=bind,source=C:/Users/Admin/Desktop/base (1)/base,target=/samples,readonly",
        "--workdir", "/workspace"]
for entry in details["Config"]["Env"]:
    name, value = entry.split("=", 1)
    environment[name] = value
    args.extend(["-e", name])
environment["PYTHONPATH"] = "/workspace/services/contract/src:/workspace:/workspace/backend:/workspace/backend/single-agent"
args.extend(["-e", "PYTHONPATH", "--entrypoint", "python", details["Config"]["Image"],
             "services/contract/scripts/rule_library_model_smoke.py"])
raise SystemExit(subprocess.call(args, env=environment))
