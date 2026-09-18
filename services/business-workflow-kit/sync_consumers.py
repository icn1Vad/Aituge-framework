"""Mechanical generation of identical registry snapshots; --check never writes."""
from pathlib import Path
import argparse
import hashlib
import json
from business_workflow_kit import load_builtin_scenes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--deployment-root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args()
    scenes = load_builtin_scenes().scenes()
    encoded = (json.dumps(scenes, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    targets = [
        args.deployment_root / "frontframe/src/features/business-workflows/generated/scenes.json",
        args.deployment_root / "Javabackend/continew-business/src/main/resources/workflow-kit/scenes.json",
    ]
    for target in targets:
        if args.check:
            if not target.is_file() or target.read_bytes() != encoded:
                raise SystemExit(f"Generated scene snapshot differs: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
    print(json.dumps({"protocolVersion": 1, "sceneCount": len(scenes),
                      "sha256": hashlib.sha256(encoded).hexdigest(), "checked": args.check}))


if __name__ == "__main__":
    main()
