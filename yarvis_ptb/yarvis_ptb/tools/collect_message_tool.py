"""Non-Telegram send_message variants.

Both share the canonical send_message spec (see message_tool.py) so the
Claude-visible tool array is identical to the main chat's — that's what lets
subagents reuse the main chat's prompt cache. The wrappers differ only in
their _execute side effects.
"""

from yarvis_ptb.tools.message_tool import build_send_message_spec
from yarvis_ptb.tools.tool_spec import LocalTool, ToolResult, ToolSpec


class CollectMessageTool(LocalTool):
    """send_message variant that collects messages without sending to Telegram."""

    def spec(self) -> ToolSpec:
        return build_send_message_spec()

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
