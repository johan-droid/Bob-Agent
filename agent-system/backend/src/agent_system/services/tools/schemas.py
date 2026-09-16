"""Runtime tool-argument validation (v3.1 §17).

The JSON schema a tool publishes to the model is the schema enforced before
execution. Nothing reaches a handler until its arguments validate:

    model tool call -> parse -> tool exists? -> arguments schema validation
                     -> permission evaluation -> execution

Supported JSON-Schema subset (exactly what the capability library authors
write, deliberately small and testable rather than a partial re-implementation
of the full spec):

``type`` · ``required`` · ``properties`` · ``additionalProperties`` (``false``
or a nested schema) · ``enum`` · ``const`` · ``pattern`` · ``minLength`` ·
``maxLength`` · ``minimum`` · ``maximum`` · ``items`` · ``minItems`` ·
``maxItems`` · ``minProperties`` · ``maxProperties`` · ``oneOf`` (as a
documented union of the above).

Anything outside that subset is treated as unconstrained, never as an error:
unknown *keywords* are ignored, unknown *arguments* are rejected only when the
tool says ``additionalProperties: false``.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Reject absurd payloads before we even walk the schema (bytes of JSON).
MAX_ARGUMENT_CHARS = 100_000


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return True  # unknown type name: do not invent a failure


def _check_type(path: str, value: Any, spec: dict[str, Any], errors: list[str]) -> None:
    expected = spec.get("type")
    if expected is None:
        return
    names = [expected] if isinstance(expected, str) else list(expected)
    if not any(_type_ok(value, str(name)) for name in names):
        joined = " | ".join(str(n) for n in names)
        errors.append(f"{path}: expected {joined}, got {type(value).__name__}")


def _check_one_of(path: str, value: Any, spec: dict[str, Any], errors: list[str]) -> None:
    options = spec.get("oneOf")
    if not isinstance(options, list) or not options:
        return
    for option in options:
        if not isinstance(option, dict):
            continue
        if not _validate(path, value, option):
            return
    errors.append(f"{path}: value matches none of the allowed forms")


def _check_object(
    path: str, value: dict[str, Any], spec: dict[str, Any], errors: list[str]
) -> None:
    properties = spec.get("properties") or {}
    required = spec.get("required") or []
    for name in required:
        if name not in value:
            errors.append(f"{path}: missing required argument '{name}'")
    min_props = spec.get("minProperties")
    if isinstance(min_props, int) and len(value) < min_props:
        errors.append(f"{path}: expected at least {min_props} properties")
    max_props = spec.get("maxProperties")
    if isinstance(max_props, int) and len(value) > max_props:
        errors.append(f"{path}: expected at most {max_props} properties")
    additional = spec.get("additionalProperties", True)
    for name, item in value.items():
        child = f"{path}.{name}" if path else name
        if isinstance(properties, dict) and name in properties:
            sub = properties[name]
            if isinstance(sub, dict):
                if not _validate(child, item, sub):
                    errors.extend(_errors(child, item, sub))
            continue
        if additional is False:
            known = ", ".join(sorted(properties)) if isinstance(properties, dict) else ""
            suffix = f" (accepted: {known})" if known else ""
            errors.append(f"{child}: unknown argument{suffix}")
        elif isinstance(additional, dict) and not _validate(child, item, additional):
            errors.extend(_errors(child, item, additional))


def _check_array(path: str, value: list[Any], spec: dict[str, Any], errors: list[str]) -> None:
    min_items = spec.get("minItems")
    if isinstance(min_items, int) and len(value) < min_items:
        errors.append(f"{path}: expected at least {min_items} items")
    max_items = spec.get("maxItems")
    if isinstance(max_items, int) and len(value) > max_items:
        errors.append(f"{path}: expected at most {max_items} items")
    items = spec.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            if not _validate(child, item, items):
                errors.extend(_errors(child, item, items))


def _check_scalars(path: str, value: Any, spec: dict[str, Any], errors: list[str]) -> None:
    enum = spec.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path}: expected one of {enum!r}, got {value!r}")
    const = spec.get("const")
    if const is not None and value != const:
        errors.append(f"{path}: expected {const!r}")
    if isinstance(value, str):
        min_len = spec.get("minLength")
        if isinstance(min_len, int) and len(value) < min_len:
            errors.append(f"{path}: shorter than minLength {min_len}")
        max_len = spec.get("maxLength")
        if isinstance(max_len, int) and len(value) > max_len:
            errors.append(f"{path}: longer than maxLength {max_len}")
        pattern = spec.get("pattern")
        if isinstance(pattern, str):
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                matched = True  # a broken pattern is an authoring bug, not a caller error
            if not matched:
                errors.append(f"{path}: does not match pattern {pattern!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = spec.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: below minimum {minimum}")
        maximum = spec.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: above maximum {maximum}")


def _errors(path: str, value: Any, spec: dict[str, Any]) -> list[str]:
    found: list[str] = []
    _check_type(path, value, spec, found)
    if isinstance(value, dict):
        _check_object(path, value, spec, found)
    elif isinstance(value, list):
        _check_array(path, value, spec, found)
    _check_scalars(path, value, spec, found)
    _check_one_of(path, value, spec, found)
    return found


def _validate(path: str, value: Any, spec: dict[str, Any]) -> bool:
    return not _errors(path, value, spec)


def validate_arguments(
    tool_name: str,
    schema: dict[str, Any],
    args: dict[str, Any],
    *,
    max_chars: int = MAX_ARGUMENT_CHARS,
) -> list[str]:
    """Return a list of human/model readable errors ([] means valid).

    Never raises: a tool with a malformed schema degrades to "accept the
    arguments" rather than making the capability unreachable.
    """
    try:
        encoded = json.dumps(args, default=str)
    except (TypeError, ValueError):
        return ["arguments are not JSON-serializable"]
    errors: list[str] = []
    if len(encoded) > max_chars:
        errors.append(f"arguments exceed the {max_chars} character limit ({len(encoded)})")
        return errors
    if not isinstance(schema, dict):
        return errors
    try:
        errors = _errors("arguments", args, schema)
    except RecursionError:
        return ["arguments are nested too deeply to validate"]
    return errors


def unknown_arguments(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    """Names in ``args`` that the schema does not declare (empty when open)."""
    if schema.get("additionalProperties", True) is not False:
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return list(args)
    return [name for name in args if name not in properties]


__all__ = ["MAX_ARGUMENT_CHARS", "unknown_arguments", "validate_arguments"]
