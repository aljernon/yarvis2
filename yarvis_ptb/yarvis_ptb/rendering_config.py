from pydantic import BaseModel, Field

from yarvis_ptb.settings.main import HISTORY_LENGTH_LONG_TURNS


class RenderingConfig(BaseModel):
    """Controls how DB messages are converted into system prompt + MessageParams.

    Pure data transformation — no tools, no side effects.
    """

    prompt_name: str = "anton_private"
    """Which system prompt to use (key into SYSTEM_PROMPTS)."""

    include_memories: bool = True
    """Include Core Knowledge Repository files in the system prompt."""

    autoload_memories: list[str] = Field(default_factory=list)
    """Specific skill files to inject into the system prompt."""

    max_history_length_turns: int = HISTORY_LENGTH_LONG_TURNS
    """How many DB message turns to fetch for context."""

    tool_result_truncation_after_n_turns: int | None = None
    """Truncate large (>=10KB) tool results in turns older than N from the end."""
