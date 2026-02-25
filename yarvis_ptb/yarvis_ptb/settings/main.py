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


MAMONT_GROUP_CHAT_ID: int = -4771154054
FAMILY_GROUP_CHAT_ID: int = -705787714
PRIVATE_DEBUG_CHAT_ID: int = -4656233403

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

KNOWN_USER_PRIVATE_CHAT_CONFIGS: dict[int, ChatConfig] = {
    192932807: ChatConfig(  # mila
        prompt_name="mamont_private",
        is_complex_chat=False,
        tool_filter="basic",
        max_history_length_turns_override=HISTORY_LENGTH_TURNS,
        memory_access=False,
    ),
}

# chat name -> config. Only works if root in the the channel and the message
# from the root.
CONFIGURED_CHATS: dict[str, ChatConfig] = {
    "yarvis_simple": ChatConfig(
        prompt_name="default",
        is_complex_chat=True,
        tool_filter="basic",
        max_history_length_turns_override=20,
        memory_access=True,
    ),
    "yarvis_simple_nomem": ChatConfig(
        prompt_name="default",
        is_complex_chat=True,
        tool_filter="basic",
        max_history_length_turns_override=20,
        memory_access=False,
    ),
    "yarvis_simple_haiku": ChatConfig(
        prompt_name="default",
        is_complex_chat=True,
        tool_filter="basic",
        max_history_length_turns_override=20,
        memory_access=True,
        model_name_override="claude-3-haiku-20240307",
    ),
    "logseq": ChatConfig(
        prompt_name="logseq",
        is_complex_chat=True,
        tool_only_messaging=True,
        tool_filter="logseq",
        max_history_length_turns_override=20,
        memory_access=False,
    ),
    "Ф": ChatConfig(
        prompt_name="family",
        is_complex_chat=False,
        tool_filter="basic",
        trigger_condition="mention",
        chat_id=FAMILY_GROUP_CHAT_ID,
        max_history_length_turns_override=HISTORY_LENGTH_TURNS,
        memory_access=False,
    ),
    "california dreaming": ChatConfig(
        prompt_name="general_group_chat",
        is_complex_chat=False,
        tool_filter="basic",
        trigger_condition="mention",
        chat_id=-4867361443,
        max_history_length_turns_override=HISTORY_LENGTH_TURNS,
        memory_access=False,
    ),
    "Claude & Mamont": ChatConfig(
        prompt_name="mamont",
        is_complex_chat=False,
        chat_id=MAMONT_GROUP_CHAT_ID,
        trigger_condition="all",
        tool_filter="basic",
        max_history_length_turns_override=HISTORY_LENGTH_TURNS,
        memory_access=False,
    ),
}


def load_env():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", verbose=True)
