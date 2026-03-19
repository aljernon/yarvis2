"""forget_above tool — marks a point in the assistant turn where prior content is dropped."""

from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

FORGET_ABOVE_TOOL_NAME = "forget_above"


class ForgetAboveTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=FORGET_ABOVE_TOOL_NAME,
            description=(
                "Drop all content above this tool call from conversation history. "
                "On the next invocation, everything in this assistant turn before "
                "this call (and all preceding tool-loop turns) will be removed. "
                "Use to compress history after long tool-use sequences."
            ),
            args=[
                ArgSpec(
                    name="summary",
                    type=str,
                    description="Optional note summarizing what was above, for your own reference.",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            "Acknowledged. Content above this call will be removed from history "
            "on the next invocation."
        )
