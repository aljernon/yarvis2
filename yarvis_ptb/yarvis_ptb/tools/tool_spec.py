import abc
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import List, Type, TypedDict, Union

from anthropic.types.tool_result_block_param import Content as ToolResultContent

logger = logging.getLogger(__name__)


class ClaudeTool(TypedDict):
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolResult:
    text: str
    images: list[dict] | None = None
    is_error: bool = False

    # Optional meta information to simplify rendeing of the tool result.
    # E.g., we can save a diff here for editor tool.
    meta_info: dict | None = None

    # If True, the sampling loop should stop after processing this result
    # (no further API call). Used by send_message(final=True).
    stop_after: bool = False

    def get_content(self) -> list[ToolResultContent]:
        content = []
        content.append({"type": "text", "text": self.text})
        images = self.images
        if images is not None:
            for img in images:
                content.append({"type": "image", "source": img})
        return content

    @classmethod
    def success(cls, text: str | dict | None, stop_after: bool = False):
        if not isinstance(text, str):
            text = json.dumps(text)
        return cls(text, is_error=False, stop_after=stop_after)

    @classmethod
    def error(cls, text: str | dict):
        if not isinstance(text, str):
            text = json.dumps(text)
        return cls(text, is_error=True)


@dataclass
class ArgSpec:
    name: str
    type: Union[Type[str], Type[int], Type[dict], Type[bool], Type[float]]
    description: str
    is_required: bool = True


@dataclass
class ToolSpec:
    name: str
    description: str
    # Either ArgSpec (simplified schema) or full JSON schema dic
    args: List[ArgSpec] | dict

    def to_input_schema(self) -> dict:
        if isinstance(self.args, dict):
            return self.args

        # Create JSON Schema for input parameters
        properties = {}
        required = []

        for arg in self.args:
            properties[arg.name] = {
                "type": get_json_schema_type(arg.type),
                "description": arg.description,
            }
            if arg.is_required:
                required.append(arg.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def to_claude_tool(self) -> ClaudeTool:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.to_input_schema(),
        }


class LocalTool(abc.ABC):
    @property
    def name(self) -> str:
        return self.spec().name

    async def init(self):
        # Any initialization code here.
        pass

    async def close(self):
        # Any cleanup code here.
        pass

    @asynccontextmanager
    async def context(self):
        await self.init()
        try:
            yield self
        finally:
            await self.close()

    @abc.abstractmethod
    def spec(self) -> ToolSpec:
        pass

    async def __call__(self, **kwargs) -> ToolResult:
        # Check if all required arguments are provided
        tool_spec = self.spec()
        required_args = tool_spec.to_input_schema().get("required", [])
        missing_args = [arg for arg in required_args if arg not in kwargs]

        if missing_args:
            return ToolResult.error(
                f"Missing required argument(s) for {self.name}: {', '.join(missing_args)}"
            )

        try:
            return await self._execute(**kwargs)
        except Exception as e:
            import traceback

            tb_str = "".join(traceback.format_exception(e))
            logger.error(f"During execution of {self.name} got exception:\n{tb_str}")
            # Include exception type and last frame in the error returned to the model
            tb_lines = traceback.format_tb(e.__traceback__)
            last_frame = tb_lines[-1].strip() if tb_lines else "no traceback"
            return ToolResult.error(
                f"During execution of {self.name} got {type(e).__name__}: {e}\n"
                f"  at: {last_frame}"
            )

    @abc.abstractmethod
    async def _execute(self, **kwargs) -> ToolResult:
        pass


def get_json_schema_type(t: Type) -> str:
    """Convert Python types to JSON Schema types."""
    type_mapping = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
    }
    return type_mapping.get(t, "string")


# Example usage:
if __name__ == "__main__":
    # Example tools
    tools = [
        ToolSpec(
            name="search_products",
            description="Search for products in the catalog based on a query string",
            args=[
                ArgSpec(
                    name="query",
                    type=str,
                    description="Search query string to find products",
                ),
                ArgSpec(
                    name="max_results",
                    type=int,
                    description="Maximum number of results to return",
                    is_required=False,
                ),
            ],
        ),
        ToolSpec(
            name="get_product_details",
            description="Get detailed information about a specific product",
            args=[
                ArgSpec(
                    name="product_id",
                    type=str,
                    description="Unique identifier for the product",
                ),
                ArgSpec(
                    name="include_reviews",
                    type=bool,
                    description="Whether to include customer reviews in the response",
                    is_required=False,
                ),
            ],
        ),
    ]

    # Convert to MCP Tool format
    mcp_tools = [x.to_claude_tool() for x in tools]

    for tool in mcp_tools:
        print(json.dumps(tool, indent=2))
