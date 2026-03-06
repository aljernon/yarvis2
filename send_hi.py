import asyncio
import datetime
import os
from contextlib import asynccontextmanager

import typer
from telegram import Bot
from telegram.ext import (
    Application,
)

from yarvis_ptb.complex_chat import (
    DEFAULT_AGENT_CONFIG,
    Invocation,
    _process_multi_message_claude_invocation_no_lock,
)
from yarvis_ptb.debug_chat import RENDERED_MESSAGES_QUEUE
from yarvis_ptb.settings import ID_USER_MAP
from yarvis_ptb.settings.anton import USER_ANTON
from yarvis_ptb.storage import DbMessage, Invocation, connect
from yarvis_ptb.yarvis_ptb.logging import setup_logging
from yarvis_ptb.yarvis_ptb.settings.main import load_env

app = typer.Typer()


@asynccontextmanager
async def get_bot():
    with connect() as conn:
        with conn.cursor() as curr:
            application = (
                Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
            )
            application.bot_data["conn"] = conn
            application.bot_data["curr"] = curr
            bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])
            yield application, bot


@app.command()
def run(config: str | None = None):
    asyncio.run(main(config_name=config))


async def main(config_name: str | None):
    chat_id = ID_USER_MAP[USER_ANTON]

    load_env()

    setup_logging()

    created_at = datetime.datetime.now(tz=datetime.timezone.utc)

    agent_config = DEFAULT_AGENT_CONFIG

    for _ in range(1):
        RENDERED_MESSAGES_QUEUE.clear()
        async with get_bot() as (app, bot):
            await _process_multi_message_claude_invocation_no_lock(
                app.bot_data["curr"],
                app,
                bot,
                chat_id,
                Invocation(invocation_type="reply"),
                initial_db_message=DbMessage(
                    created_at=created_at,
                    chat_id=chat_id,
                    user_id=chat_id,
                    message="Please compute 2+2 in python",
                ),
                agent_config=agent_config,
                skip_db=True,
                forced_now_date=created_at,
            )
        print("DEBUG MESSAGES: ----->")
        for x in RENDERED_MESSAGES_QUEUE:
            print("=" * 80)
            print(x)


if __name__ == "__main__":
    app()
