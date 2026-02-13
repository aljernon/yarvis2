import logging
from abc import abstractmethod
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from yarvis_ptb.tools.tool_spec import ClaudeTool, ToolResult
from yarvis_ptb.util import ensure

logger = logging.getLogger(__name__)


class MCPClientBase:
    def __init__(self):
        # Initialize session and client objects
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()

    @abstractmethod
    def get_tools_whitelist(self) -> frozenset[str] | None:
        # if not None, only subset of tools will be allowed
        pass

    @abstractmethod
    def get_server_params(self) -> StdioServerParameters: ...

    @asynccontextmanager
    async def context(self):
        await self.connect_to_server()
        try:
            yield self
        finally:
            await self.cleanup()

    async def connect_to_server(self):
        server_params = self.get_server_params()

        stdio_transport = await self.exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        self.stdio, self.write = stdio_transport
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(self.stdio, self.write)
        )
        assert self.session is not None

        await self.session.initialize()

        # List available tools
        self._all_mcp_tools = await ensure(self.session).list_tools()
        all_mcp_tool_names = frozenset(x.name for x in self._all_mcp_tools.tools)

        allowed_tools = self.get_tools_whitelist() or all_mcp_tool_names
        self._supported_tools = allowed_tools
        logger.info("Connected to server with tools: %s", all_mcp_tool_names)
        logger.info(f"Allowed tools: {self._supported_tools}")
        for tool_name in self._supported_tools:
            if tool_name not in all_mcp_tool_names:
                raise ValueError(
                    f"Tool {tool_name} is an allowed_tools but not supported by the server"
                )

    async def list_tools(self) -> list[ClaudeTool]:
        available_tools: list[ClaudeTool] = [
            {
                "name": tool.name,
                "description": ensure(tool.description),
                "input_schema": tool.inputSchema,
            }
            for tool in self._all_mcp_tools.tools
            if tool.name in self._supported_tools
        ]
        return available_tools

    async def call_tool(self, tool_name: str, tool_args: dict) -> ToolResult:
        response: CallToolResult = await ensure(self.session).call_tool(
            tool_name, tool_args
        )
        logger.info(f"(remote) Tool {tool_name} response: {response}")
        assert all(isinstance(x, TextContent) for x in response.content), response
        text = "\n".join(x.text for x in response.content)  # type: ignore
        return ToolResult(text=text, is_error=response.isError)

    async def cleanup(self):
        """Clean up resources"""
        await self.exit_stack.aclose()
