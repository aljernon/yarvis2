import abc
import copy
import datetime
import logging
from dataclasses import dataclass
from typing import Literal

from anthropic.types import MessageParam

from yarvis_ptb.settings import (
    BOT_USER_ID,
    ROOT_AGENT_USER_ID,
    SYSTEM_USER_ID,
    USER_ID_MAP,
)
from yarvis_ptb.storage import IMAGE_B64_META_FIELD, DbMessage
from yarvis_ptb.tools.forget_above_tool import FORGET_ABOVE_TOOL_NAME

logger = logging.getLogger(__name__)


@dataclass
class BaseTurn(abc.ABC):
    created_at: datetime.datetime
    marked_for_archive: bool

    @abc.abstractmethod
    def render(self) -> list[MessageParam]: ...

    @abc.abstractmethod
    def to_db_message(
        self, chat_id: int, *, agent_id: int | None = None
    ) -> DbMessage: ...


@dataclass
class SystemTurn(BaseTurn):
    message: str
    turn_type: Literal["notification", "schedule", "reflection"] = "notification"

    def render(self) -> list[MessageParam]:
        ts = self.created_at.isoformat()
        if self.turn_type == "schedule":
            text = f'<meta type="schedule" at="{ts}"></meta>\n{self.message}'
        elif self.turn_type == "reflection":
            text = f'<meta type="reflection" at="{ts}"></meta>\n{self.message}'
        else:
            text = f'<meta type="notification" at="{ts}"></meta>\n<system>{self.message}</system>'
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
            meta={"turn_type": self.turn_type},
            agent_id=agent_id,
        )


@dataclass
class BotTurn(BaseTurn):
    message_params: list[MessageParam]

    def render(self) -> list[MessageParam]:
        role_messages: list[MessageParam] = copy.deepcopy(self.message_params)

        # Drop empty trailing assistant message
        if (
            role_messages
            and not role_messages[-1]["content"]
            and role_messages[-1]["role"] == "assistant"
        ):
            logger.debug(f"Empty message: {role_messages[-2:]}")
            role_messages.pop()

        role_messages = _apply_forget_above(role_messages)

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


@dataclass
class InputMessageTurn(BaseTurn):
    message: str
    user_id: int

    is_voice: bool = False
    image_b64: str | None = None
    reply_to: dict | None = None
    uploaded_file: dict | None = None

    def _resolve_sender_type(self) -> str:
        if self.user_id == ROOT_AGENT_USER_ID:
            return "agent"
        return "human"

    def _resolve_sender_name(self) -> str:
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

        sender_type = self._resolve_sender_type()
        sender_name = self._resolve_sender_name()
        ts = self.created_at.isoformat()
        voice_attr = ' is_voice="true"' if self.is_voice else ""
        meta_tag = f'<meta type="message" sender_type="{sender_type}" sender_name="{sender_name}" at="{ts}"{voice_attr}></meta>'
        full_message = f"{meta_tag}\n{reply_prefix}{self.message}"
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


Turn = SystemTurn | BotTurn | InputMessageTurn


def db_message_to_turn(msg: DbMessage) -> Turn:
    if msg.user_id == BOT_USER_ID:
        if msg.meta and "message_params" in msg.meta:
            message_params = msg.meta["message_params"]
        else:
            # Legacy plain-text bot message (pre-migration).
            # Wrap into message_params on the fly.
            message_params = [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": msg.message}],
                }
            ]
        return BotTurn(
            created_at=msg.created_at,
            message_params=message_params,
            marked_for_archive=msg.marked_for_archive,
        )
    if msg.user_id == SYSTEM_USER_ID:
        meta = msg.meta or {}
        return SystemTurn(
            created_at=msg.created_at,
            message=msg.message,
            marked_for_archive=msg.marked_for_archive,
            turn_type=meta.get("turn_type", "notification"),
        )
    meta = msg.meta or {}
    return InputMessageTurn(
        created_at=msg.created_at,
        message=msg.message,
        user_id=msg.user_id,
        is_voice=meta.get("is_voice", False),
        image_b64=meta.get(IMAGE_B64_META_FIELD),
        reply_to=meta.get("reply_to"),
        uploaded_file=meta.get("uploaded_file"),
        marked_for_archive=msg.marked_for_archive,
    )


def _apply_forget_above(role_messages: list[MessageParam]) -> list[MessageParam]:
    """Strip content before the last forget_above tool call.

    Scans for the last assistant turn containing a forget_above tool_use.
    Drops all turns before it, and within that assistant turn drops all
    blocks before the forget_above call. The corresponding user turn is
    filtered to only keep tool_results for remaining tool_use IDs.
    """
    # Find the last forget_above: (turn_index, block_index)
    last_hit: tuple[int, int] | None = None
    for turn_idx, turn in enumerate(role_messages):
        if turn["role"] != "assistant" or not isinstance(turn["content"], list):
            continue
        for block_idx, block in enumerate(turn["content"]):
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == FORGET_ABOVE_TOOL_NAME
            ):
                last_hit = (turn_idx, block_idx)

    if last_hit is None:
        return role_messages

    turn_idx, block_idx = last_hit

    # Trim the assistant turn: keep from block_idx onward
    assistant_turn = role_messages[turn_idx]
    kept_blocks = assistant_turn["content"][block_idx:]
    kept_tool_ids = {
        b["id"]
        for b in kept_blocks
        if isinstance(b, dict) and b.get("type") == "tool_use"
    }
    assistant_turn["content"] = kept_blocks

    # Filter the next user turn to only keep matching tool_results
    result: list[MessageParam] = [assistant_turn]
    for turn in role_messages[turn_idx + 1 :]:
        if (
            turn["role"] == "user"
            and isinstance(turn["content"], list)
            and not kept_tool_ids
        ):
            # No tool_use IDs left to match — keep the turn as-is
            result.append(turn)
        elif (
            turn["role"] == "user"
            and isinstance(turn["content"], list)
            and kept_tool_ids
        ):
            filtered = [
                b
                for b in turn["content"]
                if not (isinstance(b, dict) and b.get("type") == "tool_result")
                or b.get("tool_use_id") in kept_tool_ids
            ]
            if filtered:
                turn["content"] = filtered
                result.append(turn)
            kept_tool_ids = set()  # only filter the immediately following user turn
        else:
            result.append(turn)

    return result


def _apply_archive_prefix(role_messages: list[MessageParam]) -> None:
    for rm in role_messages:
        content = rm["content"]
        if isinstance(content, str):
            rm["content"] = "[MARKED_FOR_DELETION] " + content
        else:
            assert isinstance(content, list)
            if content and content[0]["type"] == "text":
                content[0]["text"] = "[MARKED_FOR_DELETION] " + content[0]["text"]
