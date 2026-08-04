from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml


EXTERNAL_SPEC = "04-管理员观测接口草案.openapi.yaml"
INTERNAL_SPEC = "06-Java-Python内部观测接口草案.openapi.yaml"


class DuplicateKeyError(ValueError):
    """Raised when a YAML mapping contains the same key more than once."""


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(
                f"duplicate YAML key at line {key_node.start_mark.line + 1}: {key!r}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def docs_root() -> Path:
    if "OBS_CONTRACT_DOCS_ROOT" in os.environ:
        raise RuntimeError("OBS_CONTRACT_DOCS_OVERRIDE_DENIED")
    return repository_root() / "docs" / "observability" / "1.5"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        document = yaml.load(stream, Loader=UniqueKeyLoader)
    if not isinstance(document, dict):
        raise TypeError(f"OpenAPI root must be a mapping: {path}")
    return document


def load_external() -> dict[str, Any]:
    return load_yaml(docs_root() / EXTERNAL_SPEC)


def load_internal() -> dict[str, Any]:
    return load_yaml(docs_root() / INTERNAL_SPEC)


def iter_refs(value: Any, pointer: str = "#") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_pointer = (
                f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"
            )
            if key == "$ref" and isinstance(child, str):
                yield child_pointer, child
            else:
                yield from iter_refs(child, child_pointer)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_refs(child, f"{pointer}/{index}")


def resolve_local_ref(document: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise AssertionError(f"external ref is forbidden in frozen contracts: {ref}")
    current: Any = document
    for encoded in ref[2:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise AssertionError(f"unresolved $ref: {ref}")
        current = current[token]
    return current


def operations(document: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method.lower() in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "options",
                "head",
            }:
                yield path, method.lower(), operation


def parameter_name(
    document: dict[str, Any], parameter: dict[str, Any]
) -> tuple[str, str]:
    if "$ref" in parameter:
        parameter = resolve_local_ref(document, parameter["$ref"])
    return parameter["in"], parameter["name"]


def path_parameters(
    document: dict[str, Any],
    path: str,
    operation: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    inherited = document["paths"][path].get("parameters", [])
    for parameter in [*inherited, *operation.get("parameters", [])]:
        if "$ref" in parameter:
            parameter = resolve_local_ref(document, parameter["$ref"])
        if parameter.get("in") == "path":
            resolved[parameter["name"]] = parameter
    return resolved


def implementation_root(env_name: str, relative_path: str) -> Path | None:
    configured = os.environ.get(env_name)
    if not configured:
        return None
    return Path(configured).resolve() / relative_path
