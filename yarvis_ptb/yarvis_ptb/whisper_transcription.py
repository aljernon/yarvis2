import logging
import os

import openai

from yarvis_ptb.settings import load_env

logger = logging.getLogger(__name__)


def transcribe_file_whisper(file_path: str) -> str:
    """Transcribe an audio file using OpenAI Whisper API."""
    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    with open(file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1", file=audio_file, response_format="text"
        )
    logger.info(f"Whisper transcription completed: {transcript[:50]}...")
    return transcript.strip()


if __name__ == "__main__":
    load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # This would need to be tested with actual voice message data
    print("Whisper transcription service ready")
