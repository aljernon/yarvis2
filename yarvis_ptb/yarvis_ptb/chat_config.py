from dataclasses import dataclass
from typing import Literal


@dataclass
class ChatConfig:
    prompt_name: str

    is_complex_chat: bool
    "Essentially meants access to settings.json and showing truncating constants in the context."

    memory_access: bool
    "Include memory files in the system prompt"

    tool_filter: Literal["none", "all", "basic"]

    chat_id: int | None = None
    "Additional filtering for the bot. If set, then the bot will only reply to this chat id."

    trigger_condition: Literal["root", "all", "mention"] = "root"
    """What are the messages the bot will reply to.

    - root: all messages from the root user
    - all: all messages from any user
    - mention: all messages from any user that mentions the bot

    If not "root" then chat_id must be specified.
    """

    max_history_length_turns_override: int | None = None

    model_name_override: str | None = None
    "Override the default model name defined in settings. If None, use the default."

    tool_only_messaging: bool = False
    """If True, the agent can ONLY send messages through the send_message tool.
    When enabled, requires tool_filter to be set to 'all'.
    When this mode is active, the agent's thinking will be formatted as a quote in the chat history."""

    @property
    def max_history_length_turns(self) -> int:
        from yarvis_ptb.settings.main import HISTORY_LENGTH_LONG_TURNS

        return self.max_history_length_turns_override or HISTORY_LENGTH_LONG_TURNS

    @property
    def model_name(self) -> str:
        from yarvis_ptb.settings.main import CLAUDE_MODEL_NAME

        return self.model_name_override or CLAUDE_MODEL_NAME
