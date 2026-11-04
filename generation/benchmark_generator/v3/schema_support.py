"""Schema validation with a dependency-free runtime fallback.

Development and tests use ``jsonschema`` when it is installed. CARLA workers
must also be able to import and validate frozen configs in the lean ``carla``
environment, so the fallback implements the JSON Schema features used by V3.
"""

from __future__ import annotations

import json
from importlib import metadata
import re
from typing import Any

from benchmark_generator.config import ConfigError


def jsonschema_backend() -> dict[str, Any]:
    try:
        import jsonschema  # type: ignore
    except ModuleNotFoundError:
        return {
            "backend": "benchmark_v3_stdlib_schema_fallback_0.1",
            "jsonschema_available": False,
            "runtime_safe_without_jsonschema": True,
        }
    return {
        "backend": "jsonschema.Draft202012Validator",
        "jsonschema_available": True,
        "jsonschema_version": metadata.version("jsonschema"),
        "runtime_safe_without_jsonschema": True,
    }


def validate_schema_instance(instance: Any, schema: dict[str, Any], label: str) -> dict[str, Any]:
    status = jsonschema_backend()
    if status["jsonschema_available"]:
        try:
            from jsonschema import Draft202012Validator  # type: ignore

            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(instance)
        except Exception as exc:
            raise ConfigError(f"{label} schema validation failed: {exc}") from exc
        return status
    try:
        _validate_node(instance, schema, schema, "$")
    except ValueError as exc:
        raise ConfigError(f"{label} fallback schema validation failed: {exc}") from exc
    return status


def _validate_node(instance: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    if "$ref" in schema:
        target = _resolve_local_ref(root, str(schema["$ref"]))
        _validate_node(instance, target, root, path)
        return
    if "const" in schema and instance != schema["const"]:
        raise ValueError(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise ValueError(f"{path}: value {instance!r} is not in enum")
    if "type" in schema and not _matches_type(instance, schema["type"]):
        raise ValueError(f"{path}: expected type {schema['type']!r}, got {type(instance).__name__}")

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in instance]
        if missing:
            raise ValueError(f"{path}: missing required properties {missing}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(instance) - set(properties))
            if extras:
                raise ValueError(f"{path}: unexpected properties {extras}")
        for name, value in instance.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                _validate_node(value, child_schema, root, f"{path}.{name}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < int(schema["minItems"]):
            raise ValueError(f"{path}: expected at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
            raise ValueError(f"{path}: expected at most {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            canonical = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in instance]
            if len(canonical) != len(set(canonical)):
                raise ValueError(f"{path}: array items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _validate_node(item, item_schema, root, f"{path}[{index}]")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            raise ValueError(f"{path}: string is shorter than {schema['minLength']}")
        if "pattern" in schema and re.search(str(schema["pattern"]), instance) is None:
            raise ValueError(f"{path}: string does not match {schema['pattern']!r}")

    if _is_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ValueError(f"{path}: value is below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            raise ValueError(f"{path}: value is above maximum {schema['maximum']}")


def _resolve_local_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"unsupported non-local schema reference: {reference}")
    target: Any = root
    for raw in reference[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or key not in target:
            raise ValueError(f"unresolved schema reference: {reference}")
        target = target[key]
    if not isinstance(target, dict):
        raise ValueError(f"schema reference is not an object: {reference}")
    return target


def _matches_type(instance: Any, expected: Any) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    return any(_matches_single_type(instance, str(name)) for name in names)


def _matches_single_type(instance: Any, name: str) -> bool:
    if name == "null":
        return instance is None
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if name == "string":
        return isinstance(instance, str)
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return _is_number(instance)
    raise ValueError(f"unsupported schema type: {name}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
