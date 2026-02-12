import collections
import logging
import os
from asyncio import BoundedSemaphore
from typing import Any, Awaitable

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import (
    Application,
    CommandHandler,
    JobQueue,
    MessageHandler,
    SimpleUpdateProcessor,
    filters,
)

from clam_ptb.clam_ptb.handlers import (
    HANDLER_REGISTRY,
    app_start_callback,
    callback_minute,
    get_anthropic_client,
    handle_message,
    handle_voice_message,
    handler_interrupt,
)


async def log_all_updates(update: Update, context):
    """Log all updates for debugging"""
    if update.message:
        msg = update.message
        logger.info(
            f"UPDATE RECEIVED - Message details: text={bool(msg.text)}, "
            f"photo={bool(msg.photo)}, "
            f"document={bool(msg.document)}, "
            f"voice={bool(msg.voice)}, "
            f"audio={bool(msg.audio)}, "
            f"video={bool(msg.video)}, "
            f"animation={bool(msg.animation)}, "
            f"sticker={bool(msg.sticker)}, "
            f"video_note={bool(msg.video_note)}, "
            f"from_user={msg.from_user.id if msg.from_user else None}"
        )
        # Log full update for debugging
        logger.info(f"Full update: {update.to_dict()}")


from clam_ptb.clam_ptb.logging import setup_logging
from clam_ptb.clam_ptb.ptb_util import (
    set_last_message_id,
)
from clam_ptb.clam_ptb.settings import (
    BOT_FULL_NAME,
    FULL_LOG_CHAT_ID,
    ROOT_USER_ID,
    load_env,
)
from clam_ptb.clam_ptb.storage import connect
from clam_ptb.clam_ptb.util import ensure

# Set up logging
logger = logging.getLogger(__name__)


CHATS_WITH_COMMANDS = [
    ROOT_USER_ID,
    FULL_LOG_CHAT_ID,
    -4666161502,  # clam_simple_nomem
    -4767217828,  # clam_simple
]


class SimpleInterruptableUpdateProcessor(SimpleUpdateProcessor):
    __slots__ = ("_real_default_semaphore", "_real_semaphore_per_chat")

    def __init__(self):
        super().__init__(256)
        self._real_default_semaphore = BoundedSemaphore(1)
        self._real_semaphore_per_chat = collections.defaultdict(
            lambda: BoundedSemaphore(1)
        )

    async def do_process_update(
        self,
        update: object,
        coroutine: "Awaitable[Any]",
    ) -> None:
        handler_name = handler_interrupt.__name__.removeprefix("handler_")

        if isinstance(update, Update) and update.message and update.message.text:
            maybe_cleaned_command = update.message.text.removesuffix(BOT_FULL_NAME)
        else:
            maybe_cleaned_command = None

        if (
            maybe_cleaned_command == f"/{handler_name}"
            or maybe_cleaned_command == "/restart"
        ):
            # Commands to restart the bot or interrupt current coroutine are executed instantly.
            await coroutine
            return

        if (
            isinstance(update, Update)
            and update.message
            and update.message.text
            and not update.message.text.startswith("/")
            and update.effective_user
            and update.effective_user.id == ROOT_USER_ID
            and update.message.chat_id in CHATS_WITH_COMMANDS
        ):
            # This is a message from the root user in a complex chat.
            # Do complex interruption logic.
            await self.do_process_iterruptable_message(update, coroutine)

        else:
            async with self._real_default_semaphore:
                await coroutine

    async def do_process_iterruptable_message(
        self, update: Update, coroutine: "Awaitable[Any]"
    ) -> None:
        chat_id = ensure(ensure(update.message).chat_id)
        message_id = ensure(update.message).message_id
        logger.info(f"Waiting for semaphore for {chat_id=} {message_id=}")
        with set_last_message_id(chat_id, message_id):
            # Technically, there is no guarantee that the sempahor will wake up in the correct order, but it does in CPython.
            async with self._real_semaphore_per_chat[chat_id]:
                logger.info(f"GOT semaphore for {chat_id=} {message_id=}")
                try:
                    await coroutine
                except Exception as e:
                    logger.error(
                        f"Error while processing update in semaphore for {chat_id=} {message_id=}: {e}",
                        exc_info=True,
                    )
                finally:
                    logger.info(
                        f"FINISHED job for semaphore for {chat_id=} {message_id=}"
                    )


async def set_commands(application: Application):
    commands = [
        BotCommand(handler.name, handler.description) for handler in HANDLER_REGISTRY
    ]
    for chat_id in CHATS_WITH_COMMANDS:
        await application.bot.set_my_commands(
            commands, scope=BotCommandScopeChat(chat_id=chat_id)
        )


def main():
    # Get environment variables
    TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    # When proxying requests from another web server, e.g., location logging, we
    # may need to use a custom internal port.
    PORT = int(
        os.environ.get("CUSTOM_TELEGRAM_BOT_PORT", os.environ.get("PORT", "8443"))
    )

    logger.info(f"Starting the bot {PORT=}")

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(SimpleInterruptableUpdateProcessor())
        .build()
    )

    get_anthropic_client()  # Check for errors.

    with connect() as conn:
        application.bot_data["conn"] = conn
        job_queue = JobQueue()

        # Add logging handler first (group=-1 means it runs before other handlers)
        application.add_handler(MessageHandler(filters.ALL, log_all_updates), group=-1)  # type: ignore

        for handler in HANDLER_REGISTRY:
            application.add_handler(CommandHandler(handler.name, handler.handler))  # type: ignore
        application.add_handler(
            MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice_message)
        )  # type: ignore
        application.add_handler(
            MessageHandler(
                (
                    filters.TEXT
                    | filters.PHOTO
                    | filters.Document.ALL
                    | filters.AUDIO
                    | filters.VIDEO
                )
                & ~filters.COMMAND,
                handle_message,
            )  # type: ignore
        )

        job_queue.start()  # type: ignore

        assert application.job_queue is not None
        application.job_queue.run_once(set_commands, when=1)  # type: ignore
        application.job_queue.run_once(callback_minute, when=1)
        application.job_queue.run_once(app_start_callback, when=1)
        application.job_queue.run_repeating(callback_minute, interval=60)

        # Start the webhook
        webhook_url = (
            f"https://claude-telegram-c4fccbf117d9.herokuapp.com/{TELEGRAM_BOT_TOKEN}"
        )
        logger.info(f"Setting webhook to: {webhook_url}")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=webhook_url,
        )


if __name__ == "__main__":
    load_env()
    setup_logging()

    main()
