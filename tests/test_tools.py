import json

import pytest

from harness.tools import Tool, definitions


def sample_tool() -> Tool:
    return Tool(
        name="add",
        description="Add two integers and return the sum.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        execute=lambda a, b: str(a + b),
    )


def test_definition_is_openai_function_format():
    tool = sample_tool()
    assert tool.definition() == {
        "type": "function",
        "function": {
            "name": "add",
            "description": "Add two integers and return the sum.",
            "parameters": tool.parameters,
        },
    }


def test_definitions_covers_the_whole_registry():
    registry = {"add": sample_tool()}
    assert definitions(registry) == [registry["add"].definition()]


def test_execute_round_trips_through_json_arguments():
    tool = sample_tool()
    arguments = '{"a": 2, "b": 3}'  # exactly the string a model emits
    assert tool.execute(**json.loads(arguments)) == "5"


def test_rejects_schema_whose_type_is_not_object():
    with pytest.raises(ValueError, match="type 'object'"):
        Tool(name="bad", description="x", parameters={"type": "string"}, execute=str)


def test_rejects_schema_without_a_properties_dict():
    with pytest.raises(ValueError, match="properties"):
        Tool(name="bad", description="x", parameters={"type": "object"}, execute=str)


def test_rejects_required_names_missing_from_properties():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}},
        "required": ["a", "b"],
    }
    with pytest.raises(ValueError, match="'b'"):
        Tool(name="bad", description="x", parameters=schema, execute=str)
