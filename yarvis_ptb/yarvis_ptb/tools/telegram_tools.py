import datetime
import json
import logging
import os
import textwrap
from typing import List

import telethon
from telethon.tl.types import Channel, Chat, User

from yarvis_ptb.settings.main import (
    DEFAULT_TIMEZONE,
    DEFAULT_TIMEZONE_STR,
    load_env,
)
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class TelegramSingletonClient:
    """Base class for Telegram tools."""

    _num_entrances: int = 0
    _maybe_client: telethon.TelegramClient | None = None

    @classmethod
    async def init(cls) -> telethon.TelegramClient:
        cls._num_entrances += 1
        logger.info(f"init {cls=} {cls._num_entrances=} {cls._maybe_client=}")
        if cls._num_entrances == 1:
            cls._maybe_client = telethon.TelegramClient(
                "session_name2",
                int(os.environ["TELEGRAM_ID"]),
                os.environ["TELEGRAM_HASH"],
            )
            await cls._maybe_client.connect()
        assert cls._maybe_client is not None
        return cls._maybe_client

    @classmethod
    async def close(cls) -> None:
        cls._num_entrances -= 1
        if cls._num_entrances == 0:
            assert cls._maybe_client is not None
            cls._maybe_client.disconnect()
            cls._maybe_client = None


class TelegramToolBase(LocalTool):
    """Base class for Telegram tools."""

    def __init__(self):
        super().__init__()
        self.client = None
        self._client_initialized = False

    async def _ensure_client(self):
        """Ensure the Telegram client is connected."""
        if not self._client_initialized:
            self.client = await TelegramSingletonClient.init()
            self._client_initialized = True

    async def init(self):
        # Don't connect to Telegram on init - wait until tool is actually called
        pass

    async def close(self):
        if self._client_initialized:
            self.client = None
            self._client_initialized = False
            await TelegramSingletonClient.close()


class GetTelegramChatsTool(TelegramToolBase):
    """Tool to retrieve a list of recent Telegram chats."""

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
        await self._ensure_client()
        assert self.client is not None, "Client is not initialized"
        try:
            limit = kwargs.get("limit", 20)

            # Get all dialogs (chats)
            dialogs = await self.client.get_dialogs(limit=limit)

            result = []
            for dialog in dialogs:
                entity = dialog.entity

                # Determine the chat type
                if isinstance(entity, User):
                    chat_type = "user"
                    name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
                    if entity.username:
                        name += f" (@{entity.username})"
                elif isinstance(entity, Chat):
                    chat_type = "group"
                    name = entity.title
                elif isinstance(entity, Channel):
                    chat_type = "channel" if entity.broadcast else "supergroup"
                    name = entity.title
                else:
                    chat_type = "unknown"
                    name = str(entity)

                # Add to results
                chat_info = {
                    "chat_id": dialog.id,
                    "name": name,
                    "type": chat_type,
                    "unread_count": dialog.unread_count,
                    "last_message_date": dialog.date.isoformat()
                    if dialog.date
                    else None,
                }
                result.append(chat_info)

            return ToolResult.success(json.dumps(result, indent=2))

        except Exception as e:
            logger.exception(f"Error in get_telegram_chats: {e}")
            return ToolResult.error(f"Failed to retrieve Telegram chats: {str(e)}")


class GetTelegramMessagesTool(TelegramToolBase):
    """Tool to retrieve messages from a specific Telegram chat."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_telegram_messages",
            description=textwrap.dedent("""
            Retrieves messages from a specific Telegram chat.

            Requires a chat_id which can be obtained from the get_telegram_chats tool.

            Returns a list of messages with:
            - Message ID
            - Sender information
            - Message text
            - Timestamp
            - Reply information if available

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
                    name="offset_id",
                    type=int,
                    description="Get messages starting from this ID. Can be used for pagination.",
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
        await self._ensure_client()
        assert self.client is not None, "Client is not initialized"
        try:
            chat_id = kwargs["chat_id"]
            limit = kwargs.get("limit", 20)
            offset_id = kwargs.get("offset_id")
            search = kwargs.get("search")
            min_date_str = kwargs.get("min_date")

            # Parse min_date if provided
            min_date = None
            if min_date_str:
                try:
                    min_date = datetime.datetime.fromisoformat(min_date_str)
                except ValueError as e:
                    return ToolResult.error(
                        f"Invalid date format for min_date. Please use ISO format (YYYY-MM-DDTHH:MM:SS): {str(e)}"
                    )

            if min_date is not None:
                min_date = min_date.astimezone(DEFAULT_TIMEZONE)

            # Get the entity (chat) by ID
            try:
                entity = await self.client.get_entity(chat_id)
            except Exception as e:
                return ToolResult.error(
                    f"Could not find chat with ID {chat_id}: {str(e)}"
                )

            # Get messages from the chat
            get_messages_kwargs = {"limit": limit}
            if offset_id is not None:  # Only add if not None
                get_messages_kwargs["offset_id"] = offset_id
            if search:
                get_messages_kwargs["search"] = search
            if min_date:
                get_messages_kwargs["offset_date"] = min_date

            messages = await self.client.get_messages(entity, **get_messages_kwargs)

            result = []
            for msg in messages:
                # Skip empty or service messages
                if not msg.text and not getattr(msg, "message", None):
                    continue

                # Get sender info
                if msg.sender:
                    if hasattr(msg.sender, "first_name"):
                        sender_name = f"{msg.sender.first_name or ''} {msg.sender.last_name or ''}".strip()
                        if hasattr(msg.sender, "username") and msg.sender.username:
                            sender_name += f" (@{msg.sender.username})"
                    else:
                        sender_name = str(msg.sender.id)
                    sender_id = msg.sender.id
                else:
                    sender_name = "Unknown"
                    sender_id = None

                # Get reply info if message is a reply
                reply_to = None
                if msg.reply_to:
                    reply_msg_id = msg.reply_to.reply_to_msg_id
                    try:
                        reply_msg = await self.client.get_messages(
                            entity, ids=reply_msg_id
                        )
                        if reply_msg:
                            reply_to = {
                                "message_id": reply_msg_id,
                                "text": reply_msg.text[:100] + "..."
                                if len(reply_msg.text) > 100
                                else reply_msg.text,
                            }
                    except Exception:
                        reply_to = {
                            "message_id": reply_msg_id,
                            "text": "[could not fetch reply]",
                        }

                # Create message object
                message_info = {
                    "message_id": msg.id,
                    "sender": {"id": sender_id, "name": sender_name},
                    "text": msg.text or getattr(msg, "message", ""),
                    "date": msg.date.isoformat() if msg.date else None,
                }

                if reply_to:
                    message_info["reply_to"] = reply_to

                result.append(message_info)

            return ToolResult.success(json.dumps(result, indent=2))

        except Exception as e:
            logger.exception(f"Error in get_telegram_messages: {e}")
            return ToolResult.error(f"Failed to retrieve Telegram messages: {str(e)}")


def get_telegram_tools() -> List[LocalTool]:
    """Return a list of Telegram-related tools."""
    return [GetTelegramChatsTool(), GetTelegramMessagesTool()]


async def test_telegram_tools():
    """Simple test function for Telegram tools."""
    # Initialize logging
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Telegram tools test")

    # Test get_telegram_chats
    chat_tool = GetTelegramChatsTool()
    async with chat_tool.context():
        logger.info("Testing get_telegram_chats...")
        chat_result = await chat_tool(limit=5)

        if not chat_result.is_error:
            chat_data = json.loads(chat_result.text)
            logger.info(f"Successfully retrieved {len(chat_data)} chats")
            for i, chat in enumerate(chat_data):
                logger.info(
                    f"Chat {i+1}: {chat['name']} (ID: {chat['chat_id']}, type: {chat['type']})"
                )
                # Save the first chat ID for testing get_telegram_messages
                if i == 0:
                    test_chat_id = chat["chat_id"]
        else:
            logger.error(f"Error retrieving chats: {chat_result.text}")
            test_chat_id = None

    # Test get_telegram_messages if we have a chat ID
    if test_chat_id:
        message_tool = GetTelegramMessagesTool()
        async with message_tool.context():
            logger.info(f"Testing get_telegram_messages for chat {test_chat_id}...")
            msg_result = await message_tool(chat_id=test_chat_id, limit=3)

            if not msg_result.is_error:
                msg_data = json.loads(msg_result.text)
                logger.info(f"Successfully retrieved {len(msg_data)} messages")
                for i, msg in enumerate(msg_data):
                    logger.info(
                        f"Message {i+1} from {msg['sender']['name']}: {msg['text'][:50]}..."
                    )

                # If we have at least one message, test the min_date parameter
                if msg_data:
                    # Get the date of the most recent message
                    recent_date = msg_data[0]["date"]
                    logger.info(f"Testing min_date parameter with date: {recent_date}")
                    # Test with the recent date - should return at least the most recent message
                    date_result = await message_tool(
                        chat_id=test_chat_id, limit=10, min_date=recent_date
                    )

                    if not date_result.is_error:
                        date_data = json.loads(date_result.text)
                        logger.info(
                            f"With min_date filter: Retrieved {len(date_data)} messages from {recent_date}"
                        )
                    else:
                        logger.error(f"Error testing min_date: {date_result.text}")
            else:
                logger.error(f"Error retrieving messages: {msg_result.text}")

            # Test with invalid date format
            logger.info("Testing with invalid date format...")
            invalid_result = await message_tool(
                chat_id=test_chat_id, min_date="not-a-date"
            )
            if invalid_result.is_error:
                logger.info("Correctly rejected invalid date format")
            else:
                logger.error("Failed to reject invalid date format")


if __name__ == "__main__":
    # Can be run with: PYTHONPATH=/app/repo/yarvis_ptb python -m yarvis_ptb.tools.telegram_tools
    import asyncio

    load_env()

    try:
        asyncio.run(test_telegram_tools())
    except Exception as e:
        logger.error(f"Test failed with error: {e}")
        raise
