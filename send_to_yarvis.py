#!/usr/bin/env python3
"""Send a message to Yarvis bot on Telegram as the user (via Telethon).

Usage:
    SETTINGS_NAME=anton conda run -n clam python send_to_yarvis.py "is sms up?"
"""

import asyncio
import sys

from yarvis_ptb.settings.main import load_env

load_env()

from yarvis_ptb.telegram_client import telegram_session

BOT_USERNAME = "ya42352_bot"


async def main(message: str) -> None:
    async with telegram_session() as client:
        await client.send_message(BOT_USERNAME, message)
        print(f"Sent to @{BOT_USERNAME}: {message}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <message>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
