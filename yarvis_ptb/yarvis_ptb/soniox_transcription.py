import logging
import os

from soniox import SonioxClient
from soniox.types import CreateTranscriptionConfig

logger = logging.getLogger(__name__)


def transcribe_file_soniox(file_path: str) -> str:
    """Transcribe an audio file using Soniox API."""
    client = SonioxClient(api_key=os.environ["SONIOX_API_KEY"])
    try:
        config = CreateTranscriptionConfig(language_hints=["en", "ru"])
        transcription = client.stt.transcribe_and_wait(file=file_path, config=config)
        if transcription.status == "error":
            raise RuntimeError(f"Soniox: {transcription.error_message}")
        transcript = client.stt.get_transcript(transcription.id)
        logger.info(f"Soniox transcription completed: {transcript.text[:50]}...")
        return transcript.text.strip()
    finally:
        client.close()
