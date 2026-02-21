import asyncio
import dataclasses
import datetime
import functools
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from tempfile import NamedTemporaryFile, gettempprefix
from typing import Awaitable

import anthropic
import telegram
import telegramify_markdown
import tenacity
from telegram import Bot, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import CallbackContext
from typing_extensions import Callable

from yarvis_ptb.settings import (
    BOT_FULL_NAME,
    BOT_REAL_USER_ID,
    FULL_LOG_CHAT_ID,
    ROOT_USER_ID,
    USER_ID_MAP,
)
from yarvis_ptb.settings.main import CONFIGURED_CHATS
from yarvis_ptb.util import ensure, to_truncated_str

logger = logging.getLogger(__name__)


def build_bot_from_env() -> telegram.Bot:
    return Bot(os.environ["TELEGRAM_BOT_TOKEN"])


@dataclasses.dataclass
class InterruptionScope:
    chat_id: int
    message_id: int | None
    _is_interrupted: bool = False

    def interrupt(self) -> None:
        self._is_interrupted = True

    @property
    def is_interrupted(self) -> bool:
        from yarvis_ptb.interruption_scope_internal import CHAT2LAST_MESSAGE_ID

        if (
            self.message_id is not None
            and (last_message_id := CHAT2LAST_MESSAGE_ID.get(self.chat_id)) is not None
        ):
            # This is reply to user message, rather than scheduled generation. So it could be interrupted with a new message.
            if last_message_id != self.message_id:
                return True
        return self._is_interrupted


@contextmanager
def build_interruptable_scope(chat_id: int, message_id: int | None = None):
    from yarvis_ptb.interruption_scope_internal import INTERRUPTABLES

    scope = InterruptionScope(chat_id=chat_id, message_id=message_id)
    INTERRUPTABLES.append(scope)
    try:
        yield scope
    finally:
        INTERRUPTABLES.remove(scope)


@contextmanager
def set_last_message_id(chat_id: int, message_id: int):
    from yarvis_ptb.interruption_scope_internal import CHAT2LAST_MESSAGE_ID

    CHAT2LAST_MESSAGE_ID[chat_id] = message_id
    logger.info(f"Set last message id for chat {chat_id} to {message_id}")
    try:
        yield
    finally:
        if CHAT2LAST_MESSAGE_ID.get(chat_id) == message_id:
            logger.info(
                f"Removing info last message id for chat {chat_id} from {message_id}"
            )
            del CHAT2LAST_MESSAGE_ID[chat_id]


def get_scopes() -> list[InterruptionScope]:
    from yarvis_ptb.interruption_scope_internal import INTERRUPTABLES

    return INTERRUPTABLES


def interrupt_all() -> list[str]:
    from yarvis_ptb.interruption_scope_internal import INTERRUPTABLES

    scopes = []
    for scope in INTERRUPTABLES:
        scopes.append(scope.chat_id)
        logger.info(f"interrupting {scope.chat_id}")
        scope.interrupt()
    return scopes


def interrupt_current(chat_id: int) -> bool:
    from yarvis_ptb.interruption_scope_internal import INTERRUPTABLES

    scopes = [scope for scope in INTERRUPTABLES if scope.chat_id == chat_id]
    for scope in scopes:
        logger.info(f"interrupting {scope.chat_id}")
        scope.interrupt()
    return len(scopes) > 0


async def reply_file(
    bot: Bot,
    chat_id: int,
    text: str,
    prefix: str | None = None,
    suffix: str | None = None,
) -> telegram.Message:
    if prefix is None:
        prefix = "yarvis_message"
    if suffix is None:
        suffix = ".txt"

    date = datetime.datetime.now().date()
    with NamedTemporaryFile(
        mode="w", prefix=f"{prefix}_{date}_{gettempprefix()}", suffix=suffix
    ) as stream:
        stream.write(text)
        stream.flush()
        return await bot.send_document(chat_id=chat_id, document=stream.name)  # type: ignore


async def reply_maybe_markdown(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    message: telegram.Message | None = None,
    final_update: bool = False,
    disable_notification: bool = False,
) -> telegram.Message:
    text_md = telegramify_markdown.markdownify(text)
    # If we have a message_id, we updating an existing message. If the message
    # got too long, we will show truncated version until we get a call with
    # final_update=True. In that case if the full message is too long, we will
    # send a doc.
    if len(text_md) > 4000 and (final_update or message is None):
        # send as file
        date = datetime.datetime.now().date()
        with NamedTemporaryFile(
            mode="w", prefix=f"yarvis_message_{date}_{gettempprefix()}", suffix=".txt"
        ) as stream:
            stream.write(text)
            stream.flush()
            return await bot.send_document(chat_id=chat_id, document=stream.name)  # type: ignore
        if message is not None:
            await bot.edit_message_text(
                chat_id=chat_id, text="Message overflow", message_id=message.id
            )
    elif message is not None:
        # If we have a message_id, we can't send a doc # so we send a truncated message instead
        # edit_message_text
        outtext = to_truncated_str(text_md, 4000 + 10, truncate_front=False)
        try:
            edit_message_text_f = bot.edit_message_text
            if final_update:
                edit_message_text_f = tenacity.retry(
                    retry=tenacity.retry_if_exception_type(telegram.error.TimedOut),
                    stop=tenacity.stop_after_attempt(5),
                    wait=tenacity.wait_exponential(multiplier=2, min=1, max=10),
                )(bot.edit_message_text)
            return await edit_message_text_f(
                chat_id=chat_id,
                text=outtext,
                message_id=message.id,
                parse_mode=ParseMode.MARKDOWN_V2,  # type: ignore
            )
        except telegram.error.TimedOut:
            logger.exception("Timeout while editing message")
            if not final_update:
                return message
            else:
                # We've already re-tried with tenacity.'
                raise
        except telegram.error.BadRequest as e:
            if (
                "specified new message content and reply markup are exactly the same"
                in str(e)
            ):
                return message
            elif "Can't parse entities" in str(e):
                # whelp, we hope that the full message will have not problems.
                logger.exception("Can't parse entities")
                return message
            else:
                raise
    else:
        return await bot.send_message(
            chat_id=chat_id,
            text=text_md,
            parse_mode=ParseMode.MARKDOWN_V2,  # type: ignore
            disable_notification=disable_notification,
        )


@dataclass
class AuthInfo:
    user: telegram.User
    chat: telegram.Chat
    is_root_in_the_chat: bool
    group_chat_name: str | None
    # Either private chat, or group chat with only root user
    is_message_from_root: bool
    is_reply_to_bot: bool
    bot_mentioned: bool

    @property
    def group_chat_id(self) -> int | None:
        return ensure(self.chat.id) if self.chat.type == "group" else None

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def known_user(self) -> str | None:
        return USER_ID_MAP.get(self.user_id)

    @property
    def root_user_id(self) -> int:
        return ROOT_USER_ID

    @property
    def is_direct_root_user_complex_chat(self) -> bool:
        """Either private chat or group chat, but not debug chat."""
        return (
            self.is_root_user_complex_chat
            or self.is_message_from_root
            and self.group_chat_name in CONFIGURED_CHATS
        )

    @property
    def is_root_user_complex_chat(self) -> bool:
        return self.group_chat_id is None and self.user_id == self.root_user_id

    @property
    def is_root_user_debug_chat(self) -> bool:
        return self.group_chat_id == FULL_LOG_CHAT_ID


async def is_memeber_in_the_chat(chat: telegram.Chat, user_id: int) -> bool:
    try:
        await chat.get_member(user_id)
    except telegram.error.BadRequest:
        return False
    return True


def chat_id_from_another_side(update: Update) -> int:
    if update.effective_chat and update.effective_chat.type == "private":
        return BOT_REAL_USER_ID
    return ensure(update.effective_chat).id


async def maybe_get_auth(update: Update) -> AuthInfo | None:
    from yarvis_ptb.debug_chat import (
        build_bot_from_env,
        send_message_to_debug_chat_with_retries,
    )

    if not update.effective_user:
        logger.warning("No user in update. Refuse authorization")
        return None
    if not update.message:
        logger.warning("No message in update. Refuse authorization")
        return None
    user_id = update.effective_user.id
    logger.info(
        f"Message from User: {update.effective_user.first_name}, ID: {update.effective_user.id}, Chat ID: {ensure(update.effective_chat).id}"
    )
    root_id: int = ROOT_USER_ID
    bot_mentioned = False
    for entity, text in update.message.parse_entities().items():
        if entity.type == "mention" and text == BOT_FULL_NAME:
            bot_mentioned = True
    # user_id = USER_ID_MAP[user_id]
    if not user_id in USER_ID_MAP:
        logger.warning(f"User {user_id} not in user map {update=}")
        await send_message_to_debug_chat_with_retries(
            build_bot_from_env(), f"Suss access! @anton_shtoli {update.effective_user=}"
        )
        return None
    assert update.effective_chat
    is_reply_to_bot = bool(
        update.message
        and update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == BOT_REAL_USER_ID
    )
    if update.effective_chat.type != "private":
        logger.warning(
            f"Group chat detected: {update.effective_chat.type=} {update.effective_chat.title=}"
        )
        if update.effective_chat.type != "group":
            return None
        auth = AuthInfo(
            user=ensure(update.effective_user),
            chat=ensure(update.effective_chat),
            bot_mentioned=bot_mentioned,
            is_root_in_the_chat=(
                await is_memeber_in_the_chat(update.effective_chat, root_id)
            ),
            is_reply_to_bot=is_reply_to_bot,
            is_message_from_root=(user_id == root_id),
            group_chat_name=update.effective_chat.title,
        )
    else:
        auth = AuthInfo(
            user=ensure(update.effective_user),
            chat=ensure(update.effective_chat),
            bot_mentioned=bot_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            is_message_from_root=(user_id == root_id),
            is_root_in_the_chat=(user_id == root_id),
            group_chat_name=None,
        )
    logger.info(f"Auth record: {auth}")
    return auth


def auth_decorator(
    handler: Callable[[AuthInfo, Update, CallbackContext], Awaitable[None]],
) -> Callable[[Update, CallbackContext], Awaitable[None]]:
    @functools.wraps(handler)
    async def wrapper(update: Update, context: CallbackContext):
        if not (auth := await maybe_get_auth(update)):
            logger.warn(f"AUTH REJECT: no auth {auth}")
            return
        if not auth.is_root_in_the_chat:
            logger.warn(f"AUTH REJECT: no root in the chat {auth}")
            return
        return await handler(auth, update, context)

    return wrapper


def auth_decorator_complex_chat(
    handler: Callable[[Update, CallbackContext], Awaitable[None]],
) -> Callable[[Update, CallbackContext], Awaitable[None]]:
    @functools.wraps(handler)
    async def wrapper(update: Update, context: CallbackContext):
        if not (auth := await maybe_get_auth(update)):
            logger.warn(f"AUTH REJECT: no auth {auth}")
            return
        if not auth.is_root_in_the_chat:
            logger.warn(f"AUTH REJECT: no root in the chat {auth}")
            return
        if not auth.is_root_user_complex_chat and not auth.is_root_user_debug_chat:
            logger.warn(f"AUTH REJECT: not 'complex' chat")
            return
        return await handler(update, context)

    return wrapper


def auth_decorator_all_complex_chats(
    handler: Callable[[Update, CallbackContext], Awaitable[None]],
) -> Callable[[Update, CallbackContext], Awaitable[None]]:
    @functools.wraps(handler)
    async def wrapper(update: Update, context: CallbackContext):
        if not (auth := await maybe_get_auth(update)):
            logger.warn(f"AUTH REJECT: no auth {auth}")
            return
        if not auth.is_root_in_the_chat:
            logger.warn(f"AUTH REJECT: no root in the chat {auth}")
            return
        if (
            not auth.is_root_user_complex_chat
            and not auth.is_root_user_debug_chat
            and not (
                auth.is_message_from_root and auth.group_chat_name in CONFIGURED_CHATS
            )
        ):
            logger.warn("AUTH REJECT: not 'complex' chat")
            return
        return await handler(update, context)

    return wrapper


def get_anthropic_client() -> anthropic.Client:
    ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def get_async_anthropic_client() -> anthropic.AsyncAnthropic:
    ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
    return anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


def hard_restart():
    logger.error("GOING TO DO HARD RESTART")
    os.kill(os.getpid(), 9)
    raise RuntimeError("Hard restart")


@asynccontextmanager
async def typing_action(bot: Bot, chat_id: int, interval: float = 4.0):
    """Send 'typing' chat action repeatedly until the block exits.

    Telegram typing indicator expires after ~5 seconds, so we resend it
    every *interval* seconds.  The background task is always cancelled on
    exit (including exceptions), so the indicator stops promptly.
    """

    async def _keep_typing():
        try:
            while True:
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                except Exception:
                    logger.debug("Failed to send typing action", exc_info=True)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_keep_typing())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
