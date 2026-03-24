"""forget_above tool — marks a point in the assistant turn where prior content is dropped."""

from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

FORGET_ABOVE_TOOL_NAME = "forget_above"


class ForgetAboveTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=FORGET_ABOVE_TOOL_NAME,
            description=(
                "Trim conversation history: on the next invocation, all tool calls "
                "and results ABOVE this point in the current assistant turn will be "
                "removed, so that this tool call will appear to be first. "
                "Only useful AFTER you've already made tool calls in THIS turn that "
                "you want to drop from history (e.g. heavy research you no longer need). "
                "NEVER call this as your first action — it only trims content within "
                "the current assistant turn, not prior messages."
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
