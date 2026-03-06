import logging
import textwrap
from typing import List

from telegram import Bot

from yarvis_ptb.ptb_util import reply_maybe_markdown
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class SendMessageTool(LocalTool):
    """Tool to send a message to the user."""

    def __init__(self, bot: Bot, chat_id: int, curr):
        self.bot = bot
        self.chat_id = chat_id
        self.curr = curr

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="send_message",
            description=textwrap.dedent("""
            Sends a message to the user through the Telegram chat.

            IMPORTANT: This is the only way to send messages to the user. The user does NOT see messages within the Assistant's thinking by default.

            You can send multiple messages in the same turn using this tool multiple times.

            Use scheduling tool if you need to follow up.
            """),
            args=[
                ArgSpec(
                    name="message",
                    type=str,
                    description="The message text to send to the user",
                    is_required=True,
                ),
                ArgSpec(
                    name="final",
                    type=bool,
                    description="Set final=true if this is your last action and you have nothing more to do. This saves token cost so please use it..",
                    is_required=False,
                ),
            ],
        )

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
