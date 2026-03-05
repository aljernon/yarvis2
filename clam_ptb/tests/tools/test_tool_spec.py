import pytest

from clam_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec


class TestTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="test_tool",
            description="Test tool for testing argument validation",
            args=[
                ArgSpec(
                    name="required_arg",
                    type=str,
                    description="A required argument",
                    is_required=True,
                ),
                ArgSpec(
                    name="optional_arg",
                    type=str,
                    description="An optional argument",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        return ToolResult.success(f"Success! Args: {kwargs}")


class TestJsonSchemaTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="json_schema_tool",
            description="Test tool using JSON schema",
            args={
                "type": "object",
                "properties": {
                    "required_arg": {
                        "type": "string",
                        "description": "A required argument",
                    },
                    "optional_arg": {
                        "type": "string",
                        "description": "An optional argument",
                    },
                },
                "required": ["required_arg"],
            },
        )

    async def _execute(self, **kwargs) -> ToolResult:
        return ToolResult.success(f"Success! Args: {kwargs}")


@pytest.mark.asyncio
async def test_required_args_validation():
    """Test that LocalTool validates required arguments."""
    # Test with ArgSpec list
    test_tool = TestTool()

    # Missing required argument should return error
    result = await test_tool(optional_arg="optional value")
    assert result.is_error
    assert "Missing required argument" in result.text
    assert "required_arg" in result.text

    # Providing all required arguments should succeed
    result = await test_tool(required_arg="required value")
    assert not result.is_error
    assert "Success" in result.text

    # Providing both required and optional arguments should succeed
    result = await test_tool(
        required_arg="required value", optional_arg="optional value"
    )
    assert not result.is_error
    assert "Success" in result.text


@pytest.mark.asyncio
async def test_json_schema_required_args_validation():
    """Test that LocalTool validates required arguments with JSON schema."""
    # Test with JSON schema
    test_tool = TestJsonSchemaTool()

    # Missing required argument should return error
    result = await test_tool(optional_arg="optional value")
    assert result.is_error
    assert "Missing required argument" in result.text
    assert "required_arg" in result.text

    # Providing all required arguments should succeed
    result = await test_tool(required_arg="required value")
    assert not result.is_error
    assert "Success" in result.text

    # Providing both required and optional arguments should succeed
    result = await test_tool(
        required_arg="required value", optional_arg="optional value"
    )
    assert not result.is_error
    assert "Success" in result.text
