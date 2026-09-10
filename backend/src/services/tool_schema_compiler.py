"""Compile and lint provider-safe JSON schemas from the app's typed contracts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


TOOL_SCHEMA_VERSION = "trip-deepseek-strict-v1"
STRICT_NULL_NUMBER_SENTINEL = -999999999
_STRICT_ALLOWED = {
    "type", "description", "properties", "required", "additionalProperties", "items", "enum", "anyOf",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "default", "pattern", "format",
}
_STRICT_TYPES = {"object", "string", "number", "integer", "boolean", "array"}
EMPTY_TOOL_ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


class ToolSchemaError(ValueError):
    pass


class ToolSchemaCompiler:
    def compile(self, schema: dict[str, Any]) -> dict[str, Any]:
        compiled = self._compile_node(deepcopy(schema))
        self.lint(compiled)
        return compiled

    def schema_hash(self, schema: dict[str, Any]) -> str:
        encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def lint(self, schema: dict[str, Any]) -> None:
        errors: list[str] = []

        def visit(node: Any, path: str) -> None:
            if not isinstance(node, dict):
                return
            unsupported = sorted(set(node) - _STRICT_ALLOWED)
            if unsupported:
                errors.append(f"{path}: unsupported strict keywords {unsupported}")
            node_type = node.get("type")
            if node_type is not None and node_type not in _STRICT_TYPES:
                errors.append(f"{path}: unsupported strict type {node_type!r}")
            if any(item is None for item in node.get("enum") or []):
                errors.append(f"{path}: strict enum cannot contain null")
            if node_type == "object":
                properties = node.get("properties")
                if not isinstance(properties, dict):
                    errors.append(f"{path}: object requires properties")
                elif not properties:
                    errors.append(f"{path}: strict object cannot have empty properties")
                else:
                    required = node.get("required")
                    if sorted(required or []) != sorted(properties):
                        errors.append(f"{path}: every object property must be required")
                    if node.get("additionalProperties") is not False:
                        errors.append(f"{path}: object must set additionalProperties=false")
                    for key, value in properties.items():
                        visit(value, f"{path}.properties.{key}")
            if node_type == "array":
                visit(node.get("items"), f"{path}.items")
            for index, value in enumerate(node.get("anyOf") or []):
                if isinstance(value, dict) and value.get("type") not in _STRICT_TYPES:
                    errors.append(f"{path}.anyOf[{index}]: strict anyOf branch requires a supported type")
                visit(value, f"{path}.anyOf[{index}]")

        visit(schema, "$")
        if errors:
            raise ToolSchemaError("; ".join(errors))

    def _compile_node(self, node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if node.get("type") == "null":
            raise ToolSchemaError("DeepSeek strict mode does not support null schemas")
        compiled = {key: value for key, value in node.items() if key in _STRICT_ALLOWED}
        if "anyOf" in compiled:
            nullable = self._nullable_variant(compiled["anyOf"])
            if nullable is not None:
                compiled = self._compile_nullable_variant(nullable, str(compiled.get("description") or ""))
            else:
                compiled["anyOf"] = [self._compile_node(value) for value in compiled["anyOf"]]
        if compiled.get("type") == "object":
            properties = compiled.get("properties") if isinstance(compiled.get("properties"), dict) else {}
            compiled["properties"] = {key: self._compile_node(value) for key, value in properties.items()}
            compiled["required"] = list(properties.keys())
            compiled["additionalProperties"] = False
        if compiled.get("type") == "array":
            compiled["items"] = self._compile_node(compiled.get("items") or {})
        return compiled

    @staticmethod
    def _nullable_variant(variants: Any) -> dict[str, Any] | None:
        if not isinstance(variants, list) or len(variants) != 2:
            return None
        non_null = [item for item in variants if isinstance(item, dict) and item.get("type") != "null"]
        nulls = [item for item in variants if isinstance(item, dict) and item.get("type") == "null"]
        return non_null[0] if len(non_null) == 1 and len(nulls) == 1 else None

    def _compile_nullable_variant(self, variant: dict[str, Any], description: str) -> dict[str, Any]:
        compiled = self._compile_node(variant)
        schema_type = str(compiled.get("type") or "")
        if schema_type == "string":
            sentinel_note = "Use an empty string when this optional value is unavailable."
        elif schema_type in {"number", "integer"}:
            sentinel_note = f"Use {STRICT_NULL_NUMBER_SENTINEL} when this optional value is unavailable."
        else:
            raise ToolSchemaError(f"DeepSeek strict nullable {schema_type!r} has no safe sentinel")
        compiled["description"] = " ".join(item for item in (description.strip(), sentinel_note) if item)
        return compiled


class ToolSchemaValidator:
    """Small deterministic validator for the JSON-Schema subset exposed to tools."""

    def validate(self, value: Any, schema: dict[str, Any], path: str = "$") -> list[dict[str, str]]:
        if not isinstance(schema, dict):
            return []
        any_of = schema.get("anyOf")
        if isinstance(any_of, list) and any_of:
            variants = list(any_of)
            if isinstance(value, dict) and "op" in value:
                matching = [
                    variant
                    for variant in variants
                    if isinstance(variant, dict)
                    and value.get("op") in (((variant.get("properties") or {}).get("op") or {}).get("enum") or [])
                ]
                if matching:
                    variants = matching
            attempts = [self.validate(value, variant, path) for variant in variants]
            if any(not issues for issues in attempts):
                return []
            return min(attempts, key=len) if attempts else [self._issue(path, "does not match anyOf", "one supported variant")]
        if "enum" in schema and value not in schema.get("enum", []):
            return [self._issue(path, f"unexpected value {value!r}", f"one of {schema.get('enum')!r}")]
        expected_type = schema.get("type")
        if expected_type and not self._matches_type(value, str(expected_type)):
            return [self._issue(path, f"expected {expected_type}", str(expected_type))]
        issues: list[dict[str, str]] = []
        if expected_type == "object" and isinstance(value, dict):
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            for key in schema.get("required") or []:
                if key not in value:
                    issues.append(self._issue(f"{path}.{key}", "required property is missing", str(key)))
            if schema.get("additionalProperties") is False:
                for key in value:
                    if key not in properties:
                        issues.append(self._issue(f"{path}.{key}", "additional property is not allowed", "defined property"))
            for key, item in value.items():
                if key in properties:
                    issues.extend(self.validate(item, properties[key], f"{path}.{key}"))
        if expected_type == "array" and isinstance(value, list):
            for index, item in enumerate(value):
                issues.extend(self.validate(item, schema.get("items") or {}, f"{path}[{index}]"))
        if expected_type in {"number", "integer"} and isinstance(value, (int, float)) and not isinstance(value, bool):
            if schema.get("minimum") is not None and value < schema["minimum"]:
                issues.append(self._issue(path, f"value is below minimum {schema['minimum']}", f">= {schema['minimum']}"))
            if schema.get("maximum") is not None and value > schema["maximum"]:
                issues.append(self._issue(path, f"value is above maximum {schema['maximum']}", f"<= {schema['maximum']}"))
        return issues

    @staticmethod
    def _matches_type(value: Any, expected_type: str) -> bool:
        return {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }.get(expected_type, True)

    @staticmethod
    def _issue(path: str, message: str, expected: str) -> dict[str, str]:
        return {"path": path, "message": message, "expected": expected}


def tool_validation_schema(function: dict[str, Any]) -> dict[str, Any]:
    """Return a closed local schema even when provider syntax omits no-arg parameters."""
    parameters = function.get("parameters") if isinstance(function.get("parameters"), dict) else None
    return parameters or deepcopy(EMPTY_TOOL_ARGUMENTS_SCHEMA)


def compile_deepseek_tools(tools: list[dict[str, Any]], *, strict: bool) -> tuple[list[dict[str, Any]], dict[str, str]]:
    compiler = ToolSchemaCompiler()
    result: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    for tool in tools:
        item = deepcopy(tool)
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        parameters = function.get("parameters") if isinstance(function.get("parameters"), dict) else {"type": "object", "properties": {}}
        if strict:
            function["strict"] = True
            if parameters.get("type") == "object" and not (parameters.get("properties") or {}):
                parameters = {}
                function.pop("parameters", None)
            else:
                parameters = compiler.compile(parameters)
                function["parameters"] = parameters
        else:
            function["parameters"] = parameters
        name = str(function.get("name") or "")
        hashes[name] = compiler.schema_hash(parameters)
        item["function"] = function
        result.append(item)
    return result, hashes
