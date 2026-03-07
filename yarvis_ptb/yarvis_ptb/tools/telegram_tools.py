import datetime
import json
import logging
import textwrap
from typing import List

from yarvis_ptb.settings.main import DEFAULT_TIMEZONE_STR
from yarvis_ptb.telegram_client import get_chats, get_messages, telegram_session
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class GetTelegramChatsTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_telegram_chats",
            description=textwrap.dedent("""
            Retrieves a list of recent Telegram chats (conversations) that Anton has.

            Returns information about each chat including:
            - Chat ID (for use with get_telegram_messages tool)
            - Chat title/name
            - Chat type (user, group, channel)
            - Last message timestamp
            - Unread count if available

            Use this tool to discover available chats before using get_telegram_messages to retrieve messages from a specific chat.
            """),
            args=[
                ArgSpec(
                    name="limit",
                    type=int,
                    description="Maximum number of chats to return (default 20)",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        try:
            limit = kwargs.get("limit", 20)
            async with telegram_session() as client:
                result = await get_chats(client, limit=limit)
            return ToolResult.success(json.dumps(result, indent=2))
        except Exception as e:
            logger.exception(f"Error in get_telegram_chats: {e}")
            return ToolResult.error(f"Failed to retrieve Telegram chats: {str(e)}")


class GetTelegramMessagesTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_telegram_messages",
            description=textwrap.dedent("""
            Retrieves messages from a specific Telegram chat.

            Requires a chat_id which can be obtained from the get_telegram_chats tool.

            Returns a list of messages with:
            - Sender information
            - Message text
            - Timestamp
            - Direction (incoming/outgoing)

            Use this after using get_telegram_chats to identify which chat you want to retrieve messages from.
            """),
            args=[
                ArgSpec(
                    name="chat_id",
                    type=int,
                    description="ID of the chat to get messages from. Use get_telegram_chats to find chat IDs.",
                    is_required=True,
                ),
                ArgSpec(
                    name="limit",
                    type=int,
                    description="Maximum number of messages to return (default 20)",
                    is_required=False,
                ),
                ArgSpec(
                    name="min_date",
                    type=str,
                    description=f"Return messages sent on or after this date (ISO format: YYYY-MM-DDTHH:MM:SS). Timezone: {DEFAULT_TIMEZONE_STR}",
                    is_required=False,
                ),
                ArgSpec(
                    name="search",
                    type=str,
                    description="Optional search term to filter messages",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        try:
            chat_id = kwargs["chat_id"]
            limit = kwargs.get("limit", 20)
            search = kwargs.get("search")
            min_date_str = kwargs.get("min_date")

            min_date = None
            if min_date_str:
                try:
                    min_date = datetime.datetime.fromisoformat(min_date_str)
                except ValueError as e:
                    return ToolResult.error(
                        f"Invalid date format for min_date. Please use ISO format (YYYY-MM-DDTHH:MM:SS): {str(e)}"
                    )

            async with telegram_session() as client:
                result = await get_messages(
                    client, chat_id, limit=limit, min_date=min_date, search=search
                )
            return ToolResult.success(json.dumps(result, indent=2))
        except Exception as e:
            logger.exception(f"Error in get_telegram_messages: {e}")
            return ToolResult.error(f"Failed to retrieve Telegram messages: {str(e)}")


def get_telegram_tools() -> List[LocalTool]:
    """Return a list of Telegram-related tools."""
    return [GetTelegramChatsTool(), GetTelegramMessagesTool()]
