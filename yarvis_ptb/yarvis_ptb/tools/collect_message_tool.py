"""Non-Telegram send_message variants.

CollectMessageTool — for subagents: own spec with "return info to caller" wording.
NoOpSendMessageTool — for CLI/dashboard replay: keeps the real send_message spec
but doesn't send to Telegram.
"""

from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec


class CollectMessageTool(LocalTool):
    """send_message variant that collects messages without sending to Telegram."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="send_message",
            description=(
                "Return information to the caller. Use this to send your response. "
                "You can call this multiple times to return multiple pieces of information."
            ),
            args=[
                ArgSpec(
                    name="message",
                    type=str,
                    description="The message text to return",
                    is_required=True,
                ),
                ArgSpec(
                    name="final",
                    type=bool,
                    description="Set final=true if this is your last action and you have nothing more to do.",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        return ToolResult.success(
            "Message collected.",
            stop_after=bool(kwargs.get("final", False)),
        )


class NoOpSendMessageTool(LocalTool):
    """Keeps the real send_message spec but doesn't send to Telegram.

    Use for CLI tools and dashboard replay where you want the model to see
    the exact same tool definition as production.
    """

    def __init__(self, original_spec: ToolSpec):
        self._spec = original_spec

    def spec(self) -> ToolSpec:
        return self._spec

    async def _execute(self, **kwargs) -> ToolResult:
        return ToolResult.success(
            "Message sent successfully.",
            stop_after=bool(kwargs.get("final", False)),
        )
