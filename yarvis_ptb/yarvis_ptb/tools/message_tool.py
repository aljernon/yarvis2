import asyncio
import logging
import textwrap
from typing import List

from telegram import Bot

from yarvis_ptb.ptb_util import reply_maybe_markdown
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class SendMessageTool(LocalTool):
    """Tool to send a message to the user with optional reply timeout."""

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

            return ToolResult.success("Message sent successfully.")

        except Exception as e:
            logger.exception(f"Error sending message: {e}")
            return ToolResult.error(f"Failed to send message: {str(e)}")


def build_message_tools(bot: Bot, chat_id: int, curr) -> List[LocalTool]:
    """Return message-related tools."""
    return [SendMessageTool(bot, chat_id, curr)]


async def test_message_tool():
    """Simple test function for Message tools."""
    # Initialize logging
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Message tools test")

    # Mock objects for testing
    class MockBot:
        async def send_message(self, chat_id, text, **kwargs):
            print(f"[MOCK] Sending to {chat_id}: {text}")
            return None

    class MockCurr:
        def __init__(self):
            self.has_existing_timeout = False

        def execute(self, *args, **kwargs):
            print(f"[MOCK] SQL Execute: {args}")
            return 123

        def fetchone(self):
            return [42]  # Mock invocation ID

        def fetchall(self):
            return []  # Mock empty result for testing

    class MockDbScheduledInvocation:
        def __init__(self, meta=None):
            self.meta = meta or {}

    def get_scheduled_invocations_mock(curr, chat_id):
        """Mock function that replaces the real get_scheduled_invocations"""
        if curr.has_existing_timeout:
            # Return a mock invocation with reply_timeout type
            return [
                MockDbScheduledInvocation(meta={"invocation_type": "reply_timeout"})
            ]
        return []  # No existing invocations

    # Save original function to restore later
    import sys

    original_func = sys.modules["clam_ptb.storage"].get_scheduled_invocations
    # Replace with our mock
    sys.modules[
        "clam_ptb.storage"
    ].get_scheduled_invocations = get_scheduled_invocations_mock

    try:
        mock_bot = MockBot()
        mock_curr = MockCurr()
        test_chat_id = 12345

        # Test send_message
        message_tool = SendMessageTool(mock_bot, test_chat_id, mock_curr)

        # Test without timeout
        logger.info("Testing send_message without timeout...")
        result1 = await message_tool(message="Hello, this is a test message!")
        logger.info(f"Result: {result1.text}")

        # Test with timeout (first one)
        logger.info("Testing send_message with timeout (first)...")
        result2 = await message_tool(
            message="Please reply within 5 minutes", reply_timeout=5
        )
        logger.info(f"Result: {result2.text}")

        # Now set the mock to simulate an existing timeout
        mock_curr.has_existing_timeout = True

        # Test with timeout (second attempt)
        logger.info(
            "Testing send_message with timeout (second - should be rejected)..."
        )
        result3 = await message_tool(
            message="Please reply within 10 minutes", reply_timeout=10
        )
        logger.info(f"Result: {result3.text}")

        # Test without timeout again (should work)
        logger.info("Testing send_message without timeout again...")
        result4 = await message_tool(message="Just a regular message!")
        logger.info(f"Result: {result4.text}")

    finally:
        # Restore the original function
        sys.modules["clam_ptb.storage"].get_scheduled_invocations = original_func


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(test_message_tool())
    except Exception as e:
        logger.error(f"Test failed with error: {e}")
        raise
