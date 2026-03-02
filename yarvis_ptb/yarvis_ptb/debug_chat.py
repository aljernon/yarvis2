import asyncio
import logging
import time
from dataclasses import dataclass

import telegram
from anthropic.types import (
    MessageParam,
)
from telegram import Bot
from telegram.ext import Application

from yarvis_ptb.prompting import render_claude_response_verbose
from yarvis_ptb.ptb_util import build_bot_from_env, reply_file, reply_maybe_markdown
from yarvis_ptb.settings import FULL_LOG_CHAT_ID
from yarvis_ptb.util import RateController

logger = logging.getLogger(__name__)


@dataclass
class MessageAsFile:
    message: str
    file_prefix: str | None = None
    file_suffix: str | None = None


RENDERED_MESSAGES_QUEUE: "list[str | MessageAsFile]" = []

LOCK = asyncio.Lock()

RATE_CONTROLLER = RateController(wait_between_events_secs=3.0)


def add_debug_message_to_queue(
    message_params: list[MessageParam] | str | MessageAsFile,
    skip_first_n: int | None = None,
):
    global RENDERED_MESSAGES_QUEUE
    if isinstance(message_params, MessageAsFile):
        assert skip_first_n is None, f"{skip_first_n=}"
        RENDERED_MESSAGES_QUEUE.append(message_params)

    elif isinstance(message_params, str):
        assert skip_first_n is None, f"{skip_first_n=}"
        RENDERED_MESSAGES_QUEUE.append(message_params)
    else:
        RENDERED_MESSAGES_QUEUE.extend(
            render_claude_response_verbose(message_params, skip_first_n=skip_first_n)
        )


async def force_send_to_debug_chat(message: str) -> None:
    bot = build_bot_from_env()
    await send_message_to_debug_chat_with_retries(bot, message)


async def maybe_send_messages_to_debug_chat(application: Application):
    if LOCK.locked():
        logger.info("Fast exit from send_messages_to_debug_chat")
        return

    bot = application.bot
    assert isinstance(bot, Bot), f"{type(bot)=}"
    async with LOCK:
        logger.info(f"Got lock to send {len(RENDERED_MESSAGES_QUEUE)} messages")
        while RENDERED_MESSAGES_QUEUE:
            await RATE_CONTROLLER.wait_until_can_run()
            chunk = RENDERED_MESSAGES_QUEUE.pop(0)
            await send_message_to_debug_chat_with_retries(bot, chunk)


async def send_message_to_debug_chat_with_retries(
    bot: Bot, chunk: str | MessageAsFile
) -> None:
    timeout = 2.0
    while True:
        try:
            if isinstance(chunk, MessageAsFile):
                await reply_file(
                    bot,
                    FULL_LOG_CHAT_ID,
                    chunk.message,
                    prefix=chunk.file_prefix,
                    suffix=chunk.file_suffix,
                )
            else:
                if not chunk:
                    return
                await reply_maybe_markdown(bot, FULL_LOG_CHAT_ID, chunk)
        except (telegram.error.TimedOut, telegram.error.RetryAfter) as e:
            if isinstance(e, telegram.error.RetryAfter):
                timeout = e.retry_after
            logger.exception(
                f"Timed out while sending message to FULL_LOG_CHAT_ID. sleeping {timeout:.1f}s"
            )
            if timeout > 600:
                logger.error("Timed out for too long. Giving up.")
                break
            time.sleep(timeout)
            timeout *= 1.5
            continue
        break
