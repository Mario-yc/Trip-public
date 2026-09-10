import sqlite3
from typing import get_args

from src.api.schemas.itinerary_patches import ItineraryPatchOperationName
from src.services.tool_schema_compiler import (
    STRICT_NULL_NUMBER_SENTINEL,
    TOOL_SCHEMA_VERSION,
    ToolSchemaCompiler,
    ToolSchemaValidator,
    compile_deepseek_tools,
    tool_validation_schema,
)
from src.services.travel_tool_registry import PATCH_OPERATION_NAMES, PatchItineraryToolArgs, TravelToolRegistry


def test_deepseek_strict_compiler_lints_every_registered_tool_schema():
    registry = TravelToolRegistry(sqlite3.connect(":memory:"), None, {})

    tools, hashes = compile_deepseek_tools(registry.tool_definitions(), strict=True)

    assert TOOL_SCHEMA_VERSION == "trip-deepseek-strict-v1"
    assert set(hashes) == {tool["function"]["name"] for tool in tools}
    compiler = ToolSchemaCompiler()
    for tool in tools:
        function = tool["function"]
        assert function["strict"] is True
        if "parameters" in function:
            compiler.lint(function["parameters"])


def test_patch_operation_enum_comes_from_the_pydantic_contract():
    assert PATCH_OPERATION_NAMES == list(get_args(ItineraryPatchOperationName))
    assert "expand_meal_poi_candidates" in PATCH_OPERATION_NAMES


def test_patch_schema_uses_typed_operation_variants_and_accepts_minimal_strict_payload():
    registry = TravelToolRegistry(sqlite3.connect(":memory:"), None, {})
    raw_patch = next(tool for tool in registry.tool_definitions() if tool["function"]["name"] == "patch_itinerary")
    variants = raw_patch["function"]["parameters"]["properties"]["operations"]["items"]["anyOf"]
    exposed_names = {variant["properties"]["op"]["enum"][0] for variant in variants}
    pydantic_aliases = {
        field.alias or name for name, field in PatchItineraryToolArgs.model_fields.items()
    }

    assert exposed_names == set(PATCH_OPERATION_NAMES)
    assert set(raw_patch["function"]["parameters"]["properties"]) == pydantic_aliases

    [strict_patch], _ = compile_deepseek_tools([raw_patch], strict=True)
    schema = strict_patch["function"]["parameters"]
    issues = ToolSchemaValidator().validate(
        {"baseVersionId": "", "operations": [{"op": "replace_trip_title", "value": "北京高校两日游"}]},
        schema,
    )
    assert issues == []


def test_strict_compiler_replaces_nullable_types_with_provider_safe_sentinels():
    compiler = ToolSchemaCompiler()

    optional_string = compiler.compile({"anyOf": [{"type": "string"}, {"type": "null"}]})
    optional_number = compiler.compile({"anyOf": [{"type": "number"}, {"type": "null"}]})

    assert optional_string["type"] == "string"
    assert "empty string" in optional_string["description"]
    assert optional_number["type"] == "number"
    assert str(STRICT_NULL_NUMBER_SENTINEL) in optional_number["description"]
    assert "anyOf" not in optional_string
    assert "anyOf" not in optional_number


def test_no_arg_provider_tool_uses_closed_local_validation_schema():
    schema = tool_validation_schema({"name": "read_itinerary", "strict": True})

    assert ToolSchemaValidator().validate({}, schema) == []
    issues = ToolSchemaValidator().validate({"unexpected": True}, schema)
    assert any(issue["path"] == "$.unexpected" for issue in issues)


def test_patch_schema_preflight_rejects_operations_poi_alias_before_pydantic_execution():
    registry = TravelToolRegistry(sqlite3.connect(":memory:"), None, {})
    patch = next(tool for tool in registry.tool_definitions() if tool["function"]["name"] == "patch_itinerary")
    schema = patch["function"]["parameters"]

    issues = ToolSchemaValidator().validate(
        {"operations": [{"op": "replace_segment_poi", "segmentId": "seg_1", "poi": {"id": "invented"}}]},
        schema,
    )

    assert any(issue["path"] == "$.operations[0].poi" for issue in issues)
    assert any("additional property" in issue["message"] for issue in issues)
