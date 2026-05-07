import logging
import textwrap
from typing import List

from telegram import Bot

from yarvis_ptb.ptb_util import reply_maybe_markdown
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


# Shared spec across SendMessageTool / CollectMessageTool / NoOpSendMessageTool.
# Keeping the Claude-visible spec identical lets subagents (auto_reflect, etc.)
# share the main chat's prompt cache — different tool specs are different cache
# keys, so any drift here forces full cache_creation on every subagent run.
SEND_MESSAGE_DESCRIPTION = textwrap.dedent("""
    Send a message to the caller. The main (ROOT) agent sends the message directly to the user; subagents send the message back to the caller agent.

    IMPORTANT: This is the only way for messages to leave this agent's turn — the caller does NOT see your thinking or intermediate text by default.

    You can call this multiple times in a single turn to send multiple messages.

    Use the scheduling tool if you need to follow up later.
""").strip()

SEND_MESSAGE_ARGS = [
    ArgSpec(
        name="message",
        type=str,
        description="The message text to send to the caller",
        is_required=True,
    ),
    ArgSpec(
        name="final",
        type=bool,
        description="Set final=true if this is your last action and you have nothing more to do. This saves token cost so please use it.",
        is_required=False,
    ),
]


def build_send_message_spec() -> ToolSpec:
    return ToolSpec(
        name="send_message",
        description=SEND_MESSAGE_DESCRIPTION,
        args=list(SEND_MESSAGE_ARGS),
    )


class SendMessageTool(LocalTool):
    """Tool to send a message to the user."""

    def __init__(self, bot: Bot, chat_id: int, curr):
        self.bot = bot
        self.chat_id = chat_id
        self.curr = curr

    def spec(self) -> ToolSpec:
        return build_send_message_spec()

    async def _execute(self, **kwargs) -> ToolResult:
        message = kwargs["message"]

        try:
            # Send the message to the user
            await reply_maybe_markdown(
                self.bot,
                self.chat_id,
                message,
                disable_notification=False,
            )

            return ToolResult.success(
                "Message sent successfully.",
                stop_after=bool(kwargs.get("final", False)),
            )

        except Exception as e:
            logger.exception(f"Error sending message: {e}")
            return ToolResult.error(f"Failed to send message: {str(e)}")


def build_message_tools(bot: Bot, chat_id: int, curr) -> List[LocalTool]:
    """Return message-related tools."""
    return [SendMessageTool(bot, chat_id, curr)]
