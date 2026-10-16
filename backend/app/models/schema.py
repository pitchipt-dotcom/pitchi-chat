"""Local JSON Schema checks at the HOST boundary; no network schema resolution."""
import json
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

from backend.app.models.base import ModelError


def strict_json(text):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result: raise ValueError("duplicate key")
            result[k] = v
        return result
    def constant(value): raise ValueError("nonfinite number")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def validator_for(schema):
    def scan(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and isinstance(item, str) and not item.startswith("#"):
                    raise ValueError("nonlocal reference")
                scan(item)
        elif isinstance(value, list):
            for item in value: scan(item)
    def no_network(uri): raise NoSuchResource(ref=uri)
    try:
        scan(schema)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, registry=Registry(retrieve=no_network))
    except Exception:
        raise ModelError("JSON Schemaを安全に検証できません。", classification="invalid_schema") from None


def validated_json(text, schema):
    validator = validator_for(schema)
    try:
        value = strict_json(text)
        validator.validate(value)
        return value
    except Exception:
        raise ModelError("モデル出力がJSON Schemaに適合しません。", classification="invalid_json_schema") from None


def normalized_format(fmt):
    if not fmt or fmt.get("type") == "text": return None, None
    if fmt.get("type") == "json_object":
        schema = fmt.get("schema") or {"type": "object"}
        fmt = {"type": "json_schema", "json_schema": {"name": "host_contract", "strict": True, "schema": schema}}
    elif fmt.get("type") == "json_schema":
        schema = fmt.get("json_schema", {}).get("schema")
    else:
        raise ModelError("JSON出力形式が未対応です。", classification="invalid_schema")
    validator_for(schema)
    return fmt, schema
