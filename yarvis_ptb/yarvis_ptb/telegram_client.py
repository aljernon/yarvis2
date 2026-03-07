"""Telegram client context manager for on-demand Telethon sessions.

Opens a Telethon connection only while needed, then cleanly disconnects.
Reuses the same session file as the rest of Yarvis (session_name2).

Usage:
    from yarvis_ptb.telegram_client import telegram_session, get_recent_messages

    async with telegram_session() as client:
        messages = await get_recent_messages(client, hours=24)
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import telethon
from telethon.tl.types import Channel, Chat, User

SESSION_PATH = os.environ.get("TELETHON_SESSION_PATH", "session_name2")


class _SharedClient:
    """Reentrant singleton: first enter connects, last exit disconnects."""

    _client: telethon.TelegramClient | None = None
    _refcount: int = 0
    _lock: asyncio.Lock | None = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    async def acquire(cls) -> telethon.TelegramClient:
        async with cls._get_lock():
            cls._refcount += 1
            if cls._client is None:
                client = telethon.TelegramClient(
                    SESSION_PATH,
                    int(os.environ["TELEGRAM_ID"]),
                    os.environ["TELEGRAM_HASH"],
                )
                await client.connect()
                cls._client = client
            return cls._client

    @classmethod
    async def release(cls) -> None:
        async with cls._get_lock():
            cls._refcount -= 1
            if cls._refcount == 0 and cls._client is not None:
                await cls._client.disconnect()
                cls._client = None


@asynccontextmanager
async def telegram_session() -> AsyncIterator[telethon.TelegramClient]:
    """Reentrant async context manager for a shared Telethon client.

    First caller connects, last caller disconnects. Safe for concurrent use.
    """
    client = await _SharedClient.acquire()
    try:
        yield client
    finally:
        await _SharedClient.release()


async def get_chats(client: telethon.TelegramClient, limit: int = 20) -> list[dict]:
    """Get recent Telegram chats/dialogs."""
    dialogs = await client.get_dialogs(limit=limit)
    result = []
    for dialog in dialogs:
        entity = dialog.entity
        if isinstance(entity, User):
            chat_type = "user"
            name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
        elif isinstance(entity, Channel):
            chat_type = "channel" if entity.broadcast else "supergroup"
            name = entity.title
        elif isinstance(entity, Chat):
            chat_type = "group"
            name = entity.title
        else:
            chat_type = "unknown"
            name = str(entity)
        result.append(
            {
                "chat_id": dialog.id,
                "name": name,
                "type": chat_type,
                "unread_count": dialog.unread_count,
                "last_message_date": dialog.date.isoformat() if dialog.date else None,
            }
        )
    return result


async def get_messages(
    client: telethon.TelegramClient,
    chat_id: int,
    limit: int = 50,
    min_date: datetime | None = None,
    search: str | None = None,
) -> list[dict]:
    """Get messages from a specific chat."""
    me = await client.get_me()
    my_id = getattr(me, "id", None)

    kwargs: dict = {"limit": limit}
    if min_date:
        kwargs["offset_date"] = min_date
    if search:
        kwargs["search"] = search

    raw = await client.get_messages(chat_id, **kwargs)
    msgs: list = raw if isinstance(raw, list) else [raw] if raw else []
    result = []
    for msg in msgs:
        if not msg.text and not getattr(msg, "message", None):
            continue
        sender_name = _sender_name(msg)
        direction = "outgoing" if msg.sender_id == my_id else "incoming"
        text = msg.text or ""
        if msg.media and not msg.text:
            text = f"[Media: {type(msg.media).__name__}]"
        result.append(
            {
                "timestamp": msg.date.isoformat() if msg.date else "",
                "from_name": sender_name,
                "direction": direction,
                "message": text,
                "conversation_partner": sender_name,
                "chat_id": chat_id,
            }
        )
    return result


async def get_recent_messages(
    client: telethon.TelegramClient,
    hours: int = 24,
    limit_chats: int = 15,
    limit_messages_per_chat: int = 20,
) -> list[dict]:
    """Scan recent chats and return messages from the last N hours."""
    me = await client.get_me()
    my_id = getattr(me, "id", None)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    dialogs = await client.get_dialogs(limit=limit_chats)
    all_messages = []
    for dialog in dialogs:
        is_group = isinstance(dialog.entity, (Chat, Channel))
        try:
            raw = await client.get_messages(dialog.id, limit=limit_messages_per_chat)
            msgs: list = raw if isinstance(raw, list) else [raw] if raw else []
        except Exception:
            continue
        for msg in msgs:
            if not msg.date or msg.date < cutoff:
                continue
            if not msg.text and not getattr(msg, "message", None):
                continue
            sender_name = _sender_name(msg)
            direction = "outgoing" if msg.sender_id == my_id else "incoming"
            text = msg.text or ""
            if msg.media and not msg.text:
                text = f"[Media: {type(msg.media).__name__}]"
            sender_phone = getattr(msg.sender, "phone", None) if msg.sender else None
            all_messages.append(
                {
                    "timestamp": msg.date.isoformat(),
                    "from_name": sender_name,
                    "direction": direction,
                    "message": text,
                    "conversation_partner": dialog.name or f"Chat {dialog.id}",
                    "chat_id": dialog.id,
                    "is_group": is_group,
                    "sender_phone": sender_phone,
                }
            )
    return all_messages


def _sender_name(msg) -> str:
    if not msg.sender:
        return "Unknown"
    if hasattr(msg.sender, "first_name"):
        name = f"{msg.sender.first_name or ''} {msg.sender.last_name or ''}".strip()
        return name or "Unknown"
    if hasattr(msg.sender, "title"):
        return msg.sender.title
    return str(msg.sender.id)
