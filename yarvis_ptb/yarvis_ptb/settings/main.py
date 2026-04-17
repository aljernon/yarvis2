import pathlib
from typing import Any

import pytz

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent.parent
LOCATION_PATH = PROJECT_ROOT / "locations.json"

DEFAULT_TIMEZONE_STR = "US/Pacific"
CLAUDE_MODEL_NAME = "claude-opus-4-7"

SUBAGENT_MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}
SUBAGENT_DEFAULT_MODEL = "haiku"
MAX_AGENT_CONTEXT_TOKENS = 50_000
DEFAULT_TIMEZONE = pytz.timezone(DEFAULT_TIMEZONE_STR)


HISTORY_LENGTH_TURNS = 60
HISTORY_LENGTH_LONG_TURNS = 400
HISTORY_LENGTH_LONG_TOKENS = 80_000
# Synthetic user_id values in the messages table.
# Real Telegram user IDs are positive integers (e.g. 96009555).
# These determine how db_message_to_turn() routes messages:
BOT_USER_ID = -1  # Claude's assistant-role responses (BotTurn, meta has message_params)
SYSTEM_USER_ID = -2  # System notifications, schedule markers, reflections (SystemTurn)
AGENT_TO_AGENT_USER_ID = (
    1  # Agent-to-agent messages (InputMessageTurn, sender_type="agent")
)
ROOT_AGENT_SLUG = "ROOT"  # also determines workspace todo filename: todos/{slug}.json

KNOWN_USER_PRIVATE_CHAT_CONFIGS: dict[int, Any] = {}

# chat name -> config. Only works if root in the the channel and the message
# from the root.
CONFIGURED_CHATS: dict[str, Any] = {}


def load_env():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", verbose=True)
