"""forget_above tool — marks a point in the assistant turn where prior content is dropped."""

from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

FORGET_ABOVE_TOOL_NAME = "forget_above"


class ForgetAboveTool(LocalTool):
    def __init__(self):
        self.prior_tool_calls = 0

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=FORGET_ABOVE_TOOL_NAME,
            description=(
                "Trim conversation history: on the next invocation, everything above "
                "this point will be removed — all prior conversation turns AND all "
                "tool calls/results above this point in the current bot turn. This call "
                "will appear to be the start of the conversation. Everything AFTER this "
                "call (later tool calls, subagent results) is preserved. "
                "Only useful AFTER you've already made tool calls in THIS turn. "
                "NEVER call this as your first action — there is nothing above to forget. "
                "Common pattern: forget_above, then delegate remaining work to a subagent. "
                "No summary needed — the subagent gets its own context, and its result "
                "will be returned into this conversation (below the forget point, so it's kept)."
            ),
            args=[
                ArgSpec(
                    name="summary",
                    type=str,
                    description=(
                        "Optional note summarizing what was above, for your own reference "
                        "in subsequent tool calls within this turn. Not needed if you are "
                        "about to delegate to a subagent."
                    ),
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        if self.prior_tool_calls == 0:
            return ToolResult.error(
                "forget_above has no effect as the first tool call — "
                "there is nothing above to forget. "
                "Use it after tool calls you want trimmed from history."
            )
        return ToolResult(
            "Acknowledged. Content above this call will be removed from history "
            "on the next invocation."
        )
