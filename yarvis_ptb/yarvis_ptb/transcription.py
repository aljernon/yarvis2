import asyncio
import logging
import os

import telethon
import telethon.tl.types.messages

from yarvis_ptb.settings import load_env

logger = logging.getLogger(__name__)


async def _wait_until_last_message_has_voice(
    client: telethon.TelegramClient, chat_id: int
) -> telethon.tl.types.Message:
    for attempt in range(10):
        saved_messages = await client.get_entity(chat_id)
        message = None
        async for message in client.iter_messages(saved_messages):
            break
        assert message is not None
        assert message.chat.id == chat_id, f"{message.chat.id=} {chat_id=}"
        if message.voice or message.sender.username != "anton_shtoli":
            return message
        logger.info(
            f"Last message is not voice, waiting ({attempt=}) {message.stringify()}"
        )
        await asyncio.sleep(1)
        await _wait_until_last_message_has_voice(client, chat_id)
    raise Exception("No voice message found")


async def transcribe_last_message(chat_id: int) -> str:
    async with telethon.TelegramClient(
        "session_name", os.environ["TELEGRAM_ID"], os.environ["TELEGRAM_HASH"]
    ) as client:
        message = await _wait_until_last_message_has_voice(client, chat_id)
        logger.info(f"Getting transcription {message.id=}")
        while True:
            result: telethon.tl.types.messages.TranscribedAudio = await client(
                telethon.functions.messages.TranscribeAudioRequest(
                    peer=chat_id,
                    msg_id=message.id,
                )
            )
            if not result.pending:
                logger.info("Got transcription")
                return result.text
            logger.info("Waiting for transcription")
            await asyncio.sleep(1)


if __name__ == "__main__":
    load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("chat_id", type=int)
    args = parser.parse_args()
    transcription = asyncio.run(transcribe_last_message(args.chat_id))
    print("Transcritpion:", transcription)
