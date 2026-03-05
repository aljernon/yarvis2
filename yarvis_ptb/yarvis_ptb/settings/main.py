import pathlib

import pytz

from yarvis_ptb.chat_config import ChatConfig

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
DEFAULT_TIMEZONE = pytz.timezone(DEFAULT_TIMEZONE_STR)



HISTORY_LENGTH_TURNS = 60
HISTORY_LENGTH_LONG_TURNS = 400
HISTORY_LENGTH_LONG_TOKENS = 80_000
HISTORY_LENGTH_LONG_SHRINKING_FACTOR = 0.5
LARGE_MESSAGE_SIZE_THRESHOLD = (
    0.3  # If message takes >30% of total size, consider it large
)
# Id as we record in the database
BOT_USER_ID = -1
TOOL_CALL_USER_ID = -3
SYSTEM_USER_ID = -2

KNOWN_USER_PRIVATE_CHAT_CONFIGS: dict[int, ChatConfig] = {}

# chat name -> config. Only works if root in the the channel and the message
# from the root.
CONFIGURED_CHATS: dict[str, ChatConfig] = {}


def load_env():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", verbose=True)
