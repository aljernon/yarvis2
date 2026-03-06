"""No-op send_message tool for frozen agents.

Same interface as SendMessageTool, but doesn't actually send to Telegram.
The message content is captured in message_params as tool_use blocks,
which _extract_agent_messages() already knows how to extract.
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
