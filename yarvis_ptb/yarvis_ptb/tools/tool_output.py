import json
import logging

from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class GetToolOutputTool(LocalTool):
    def __init__(self, curr):
        self.curr = curr

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_tool_output",
            description="Retrieve the original content of a truncated tool result. Use this when you see a truncation notice in an old tool result.",
            args=[
                ArgSpec(
                    name="msg_id",
                    type=int,
                    description="The DB message ID from the truncation notice.",
                ),
                ArgSpec(
                    name="tool_index",
                    type=int,
                    description="The positional index of the tool_result block within that message's message_params.",
                ),
            ],
        )

    async def _execute(  # pyre-ignore[14]
        self, msg_id: int, tool_index: int, **kwargs
    ) -> ToolResult:
        self.curr.execute(
            "SELECT meta FROM messages WHERE id = %s",
            (msg_id,),
        )
        row = self.curr.fetchone()
        if row is None:
            return ToolResult.error(f"Message with id={msg_id} not found.")

        meta = row[0]
        if not meta or "message_params" not in meta:
            return ToolResult.error(
                f"Message id={msg_id} does not have message_params in meta."
            )

        message_params = meta["message_params"]

        # Find the tool_result at the given index across all message_params
        tool_result_count = 0
        for msg_param in message_params:
            content = msg_param.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if tool_result_count == tool_index:
                        result_content = block.get("content", "")
                        if isinstance(result_content, str):
                            return ToolResult.success(result_content)
                        elif isinstance(result_content, list):
                            text_parts = []
                            for item in result_content:
                                if (
                                    isinstance(item, dict)
                                    and item.get("type") == "text"
                                ):
                                    text_parts.append(item.get("text", ""))
                                else:
                                    text_parts.append(json.dumps(item))
                            return ToolResult.success("\n".join(text_parts))
                        else:
                            return ToolResult.success(json.dumps(result_content))
                    tool_result_count += 1

        return ToolResult.error(
            f"tool_index={tool_index} out of range. Message id={msg_id} has {tool_result_count} tool_result blocks."
        )
