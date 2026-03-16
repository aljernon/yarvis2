import abc
import copy
import datetime
import logging
from dataclasses import dataclass

from anthropic.types import MessageParam

from yarvis_ptb.settings import (
    BOT_USER_ID,
    ROOT_AGENT_USER_ID,
    SYSTEM_USER_ID,
    USER_ID_MAP,
)
from yarvis_ptb.storage import IMAGE_B64_META_FIELD, DbMessage

logger = logging.getLogger(__name__)


@dataclass
class BaseTurn(abc.ABC):
    created_at: datetime.datetime
    marked_for_archive: bool = False

    @abc.abstractmethod
    def render(self) -> list[MessageParam]: ...

    @abc.abstractmethod
    def to_db_message(
        self, chat_id: int, *, agent_id: int | None = None
    ) -> DbMessage: ...


@dataclass
class SystemTurn(BaseTurn):
    message: str = ""

    def render(self) -> list[MessageParam]:
        text = f"<system>System message created at {self.created_at.isoformat()}: {self.message}</system>"
        role_messages: list[MessageParam] = [{"role": "user", "content": text}]
        if self.marked_for_archive:
            _apply_archive_prefix(role_messages)
        return role_messages

    def to_db_message(self, chat_id: int, *, agent_id: int | None = None) -> DbMessage:
        return DbMessage(
            created_at=self.created_at,
            chat_id=chat_id,
            user_id=SYSTEM_USER_ID,
            message=self.message,
            agent_id=agent_id,
        )


@dataclass
class BotTurn(BaseTurn):
    message_params: list[MessageParam] | None = None
    plain_text: str | None = None

    def __post_init__(self):
        assert (self.message_params is None) != (
            self.plain_text is None
        ), "BotTurn requires exactly one of message_params or plain_text"

    def render(self) -> list[MessageParam]:
        if self.message_params is not None:
            role_messages: list[MessageParam] = copy.deepcopy(self.message_params)
        else:
            role_messages = [
                MessageParam(role="assistant", content=self.plain_text or "")
            ]

        # Drop empty trailing assistant message
        if (
            role_messages
            and not role_messages[-1]["content"]
            and role_messages[-1]["role"] == "assistant"
        ):
            logger.debug(f"Empty message: {role_messages[-2:]}")
            role_messages.pop()

        if self.marked_for_archive:
            _apply_archive_prefix(role_messages)

        return role_messages

    def to_db_message(
        self,
        chat_id: int,
        *,
        agent_id: int | None = None,
        usage: dict | None = None,
    ) -> DbMessage:
        if self.message_params is not None:
            meta: dict = {"message_params": self.message_params}
            if usage:
                meta["usage"] = usage
            return DbMessage(
                created_at=self.created_at,
                chat_id=chat_id,
                user_id=BOT_USER_ID,
                message="USE_CONTENT_FROM_META",
                meta=meta,
                agent_id=agent_id,
            )
        assert self.plain_text is not None
        return DbMessage(
            created_at=self.created_at,
            chat_id=chat_id,
            user_id=BOT_USER_ID,
            message=self.plain_text,
            agent_id=agent_id,
        )


@dataclass
class UserTurn(BaseTurn):
    message: str = ""
    user_id: int = 0

    is_voice: bool = False
    image_b64: str | None = None
    reply_to: dict | None = None
    uploaded_file: dict | None = None

    def _resolve_sender(self) -> str:
        return USER_ID_MAP.get(
            self.user_id,
            "root agent"
            if self.user_id == ROOT_AGENT_USER_ID
            else f"unknown user ({self.user_id})",
        )

    def render(self) -> list[MessageParam]:
        content_chunks: list = []

        if self.image_b64:
            content_chunks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": self.image_b64,
                    },
                }
            )

        reply_prefix = ""
        reply_to = self.reply_to
        if reply_to is not None:
            text = reply_to["text"]
            display_text = text[:200] + "..." if len(text) > 200 else text
            reply_prefix = f"[Replying to {reply_to['from']} at {reply_to.get('date', '?')}: \"{display_text}\"]\n"

        sender = self._resolve_sender()
        is_voice_message = self.is_voice
        full_message = f"<system>Sent by {sender} at {self.created_at.isoformat()} {is_voice_message=}</system>\n{reply_prefix}{self.message}"
        content_chunks.append({"type": "text", "text": full_message})

        role_messages: list[MessageParam] = [
            {"role": "user", "content": content_chunks}
        ]

        if self.marked_for_archive:
            _apply_archive_prefix(role_messages)

        return role_messages

    def to_db_message(self, chat_id: int, *, agent_id: int | None = None) -> DbMessage:
        meta: dict = {"is_voice": self.is_voice}
        if self.image_b64:
            meta[IMAGE_B64_META_FIELD] = self.image_b64
        if self.reply_to:
            meta["reply_to"] = self.reply_to
        if self.uploaded_file:
            meta["uploaded_file"] = self.uploaded_file
        return DbMessage(
            created_at=self.created_at,
            chat_id=chat_id,
            user_id=self.user_id,
            message=self.message,
            meta=meta,
            agent_id=agent_id,
        )


Turn = SystemTurn | BotTurn | UserTurn


def db_message_to_turn(msg: DbMessage) -> Turn:
    if msg.user_id == BOT_USER_ID:
        message_params = None
        plain_text = None
        if msg.meta and "message_params" in msg.meta:
            message_params = msg.meta["message_params"]
        else:
            plain_text = msg.message
        return BotTurn(
            created_at=msg.created_at,
            message_params=message_params,
            plain_text=plain_text,
            marked_for_archive=msg.marked_for_archive,
        )
    if msg.user_id == SYSTEM_USER_ID:
        return SystemTurn(
            created_at=msg.created_at,
            message=msg.message,
            marked_for_archive=msg.marked_for_archive,
        )
    meta = msg.meta or {}
    return UserTurn(
        created_at=msg.created_at,
        message=msg.message,
        user_id=msg.user_id,
        is_voice=meta.get("is_voice", False),
        image_b64=meta.get(IMAGE_B64_META_FIELD),
        reply_to=meta.get("reply_to"),
        uploaded_file=meta.get("uploaded_file"),
        marked_for_archive=msg.marked_for_archive,
    )


def _apply_archive_prefix(role_messages: list[MessageParam]) -> None:
    for rm in role_messages:
        content = rm["content"]
        if isinstance(content, str):
            rm["content"] = "[MARKED_FOR_DELETION] " + content
        else:
            assert isinstance(content, list)
            if content and content[0]["type"] == "text":
                content[0]["text"] = "[MARKED_FOR_DELETION] " + content[0]["text"]
