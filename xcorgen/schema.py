"""Stdlib validator for the subset of JSON Schema 2020-12 used by spec/scenario.schema.json.

The engine and CLI stay dependency-free. The server generates Pydantic and Zod
types from the same schema file, so this validator only needs to agree with
them on the keywords the schema actually uses (unknown keywords are rejected
when the schema is loaded, so a new keyword cannot be silently ignored).
"""
import json
import math
import os
import re
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "spec", "scenario.schema.json")

SUPPORTED = {"$schema", "$id", "$defs", "$ref", "title", "description", "default", "type", "enum", "const",
             "properties", "required", "additionalProperties", "propertyNames", "minProperties", "items",
             "minItems", "maxItems", "uniqueItems", "minimum", "maximum", "exclusiveMinimum", "minLength",
             "maxLength", "pattern", "format", "anyOf", "oneOf"}
TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
}


def _check_keywords(node, path="#"):
    if isinstance(node, dict):
        unknown = set(node) - SUPPORTED
        if unknown and not path.endswith("/properties") and not path.endswith("/$defs"):
            raise ValueError(f"Unsupported schema keywords at {path}: {sorted(unknown)}")
        for k, v in node.items():
            _check_keywords(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _check_keywords(v, f"{path}/{i}")


_SCHEMA = None


def load_schema():
    global _SCHEMA
    if _SCHEMA is None:
        with open(SCHEMA_PATH) as f:
            schema = json.load(f)
        _check_keywords(schema)
        _SCHEMA = schema
    return _SCHEMA


def _format_ok(fmt, value):
    try:
        if fmt == "date-time":
            if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$", value):
                return False
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif fmt == "date":
            date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _resolve(root, ref):
    if not ref.startswith("#/"):
        raise ValueError(f"Only local references are supported: {ref}")
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(value, schema=None, root=None, path="$"):
    """Return a list of (json_path, message). An empty list means the value conforms."""
    root = root or schema or load_schema()
    schema = schema if schema is not None else root
    errors = []
    if "$ref" in schema:
        errors += validate(value, _resolve(root, schema["$ref"]), root, path)
    if "const" in schema and value != schema["const"]:
        errors.append((path, f"must equal {json.dumps(schema['const'])}"))
    if "enum" in schema and value not in schema["enum"]:
        errors.append((path, f"must be one of {', '.join(json.dumps(v) for v in schema['enum'])}"))
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(TYPES[t](value) for t in types):
            errors.append((path, f"must be of type {' or '.join(types)}"))
            return errors
    if "anyOf" in schema:
        branches = [validate(value, s, root, path) for s in schema["anyOf"]]
        if all(branches):
            errors += min(branches, key=len)
    if "oneOf" in schema:
        matches = [s for s in schema["oneOf"] if not validate(value, s, root, path)]
        if len(matches) != 1:
            errors.append((path, "must match exactly one allowed form"))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append((f"{path}.{key}", "is required"))
        if len(value) < schema.get("minProperties", 0):
            errors.append((path, f"needs at least {schema['minProperties']} entries"))
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if "propertyNames" in schema:
                errors += [(f"{path}.{key}", f"key {m}") for _, m in validate(key, schema["propertyNames"], root, path)]
            if key in props:
                errors += validate(item, props[key], root, f"{path}.{key}")
            elif extra is False:
                errors.append((f"{path}.{key}", "is not a recognized field"))
            elif isinstance(extra, dict):
                errors += validate(item, extra, root, f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append((path, f"needs at least {schema['minItems']} items"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append((path, f"allows at most {schema['maxItems']} items"))
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(seen) != len(set(seen)):
                errors.append((path, "must not contain duplicates"))
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], root, f"{path}[{i}]")
    if TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append((path, f"must be at least {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append((path, f"must be at most {schema['maximum']}"))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append((path, f"must be greater than {schema['exclusiveMinimum']}"))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append((path, f"must be at least {schema['minLength']} characters"))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append((path, f"must be at most {schema['maxLength']} characters"))
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append((path, "has an invalid format"))
        if "format" in schema and not _format_ok(schema["format"], value):
            errors.append((path, f"must be an RFC 3339 {schema['format']}"))
    return errors
