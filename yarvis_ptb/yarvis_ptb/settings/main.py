import pathlib
from typing import Any

import pytz

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent.parent
LOCATION_PATH = PROJECT_ROOT / "locations.json"

DEFAULT_TIMEZONE_STR = "US/Pacific"
CLAUDE_MODEL_NAME = "claude-opus-4-6"

SUBAGENT_MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6",
}
SUBAGENT_DEFAULT_MODEL = "haiku"
MAX_AGENT_CONTEXT_TOKENS = 50_000
DEFAULT_TIMEZONE = pytz.timezone(DEFAULT_TIMEZONE_STR)


HISTORY_LENGTH_TURNS = 60
HISTORY_LENGTH_LONG_TURNS = 400
HISTORY_LENGTH_LONG_TOKENS = 80_000
# Id as we record in the database
BOT_USER_ID = -1
TOOL_CALL_USER_ID = -3
SYSTEM_USER_ID = -2
ROOT_AGENT_USER_ID = 1

KNOWN_USER_PRIVATE_CHAT_CONFIGS: dict[int, Any] = {}

# chat name -> config. Only works if root in the the channel and the message
# from the root.
CONFIGURED_CHATS: dict[str, Any] = {}


def load_env():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", verbose=True)
