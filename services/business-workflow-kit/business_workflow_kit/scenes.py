"""Fail-closed scene admission; callers receive copies of immutable definitions."""
from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
import json
import re
from typing import Any, Iterable

_ID = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_KEY = re.compile(r"[a-zA-Z][a-zA-Z0-9]{0,63}\Z")


class SceneRegistry:
    def __init__(self, scenes: Iterable[dict[str, Any]]):
        self._scenes: dict[str, dict[str, Any]] = {}
        self._workflows: dict[str, tuple[str, dict[str, Any]]] = {}
        self._modes: dict[str, str] = {}
        for source in scenes:
            scene = deepcopy(source)
            if scene.get("protocolVersion") != 1:
                raise ValueError("Unsupported scene protocol version")
            scene_id, mode = scene["sceneId"], scene["assistantMode"]
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", scene_id):
                raise ValueError("Invalid scene ID")
            if not _ID.fullmatch(mode) or not mode.endswith("_ASSISTANT"):
                raise ValueError("Invalid assistant mode")
            if scene_id in self._scenes or mode in self._modes:
                raise ValueError("Duplicate scene or assistant mode")
            if not scene.get("workflows"):
                raise ValueError("Scene must register a workflow")
            for workflow in scene["workflows"]:
                code = workflow["workflowType"]
                if not _ID.fullmatch(code) or code in self._workflows:
                    raise ValueError("Invalid or duplicate workflow")
                if workflow.get("validationMode") not in {"legacy-adapter", "schema"}:
                    raise ValueError("Unknown validation mode")
                seen: set[str] = set()
                for field in workflow["fields"]:
                    key = field["key"]
                    if not _KEY.fullmatch(key) or key in seen:
                        raise ValueError("Invalid or duplicate field key")
                    seen.add(key)
                    if field.get("type", "text") not in {"text", "date", "number", "enum"}:
                        raise ValueError("Unknown field type")
                    if field.get("type") == "enum" and not field.get("options"):
                        raise ValueError("Enum field requires options")
                for field in workflow["fields"]:
                    if field.get("dependsOn") and field["dependsOn"] not in seen:
                        raise ValueError("Unknown dependency")
                self._workflows[code] = (scene_id, workflow)
            self._scenes[scene_id] = scene
            self._modes[mode] = scene_id
        for scene_id, workflow in self._workflows.values():
            for field in workflow["fields"]:
                ref = field.get("referenceWorkflow")
                if ref and (ref not in self._workflows or self._workflows[ref][0] != scene_id):
                    raise ValueError("Reference must target a workflow in the same scene")

    def scenes(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._scenes.values()))

    def workflows(self, assistant_mode: str) -> list[dict[str, Any]]:
        scene_id = self._modes.get(assistant_mode)
        return deepcopy(self._scenes[scene_id]["workflows"]) if scene_id else []

    def workflow(self, code: str) -> dict[str, Any]:
        if code not in self._workflows:
            raise ValueError("Unregistered workflow")
        return deepcopy(self._workflows[code][1])


def load_builtin_scenes() -> SceneRegistry:
    directory = files("business_workflow_kit").joinpath("scenes")
    return SceneRegistry(json.loads(p.read_text(encoding="utf-8"))
                         for p in sorted(directory.iterdir(), key=lambda p: p.name)
                         if p.name.endswith(".json"))
