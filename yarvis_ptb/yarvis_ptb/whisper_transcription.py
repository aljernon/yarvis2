import logging
import os
import tempfile

import openai
from telegram import Bot, Voice

from yarvis_ptb.settings import load_env

logger = logging.getLogger(__name__)


async def transcribe_voice_message(bot: Bot, voice: Voice) -> str:
    """Transcribe voice message using OpenAI Whisper API."""
    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    try:
        # Download the voice file using python-telegram-bot
        voice_file = await bot.get_file(voice.file_id)

        # Create temporary file for the voice data
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_file:
            await voice_file.download_to_drive(temp_file.name)
            voice_file_path = temp_file.name

        try:
            # Transcribe using OpenAI Whisper
            with open(voice_file_path, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1", file=audio_file, response_format="text"
                )

            logger.info(f"Transcription completed: {transcript[:50]}...")
            return transcript.strip()

        finally:
            # Clean up the temporary file
            if os.path.exists(voice_file_path):
                os.unlink(voice_file_path)

    except Exception as e:
        logger.error(f"Error transcribing voice message: {e}")
        raise


if __name__ == "__main__":
    load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # This would need to be tested with actual voice message data
    print("Whisper transcription service ready")
