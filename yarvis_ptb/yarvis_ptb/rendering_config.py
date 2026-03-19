from pydantic import BaseModel, model_validator

from yarvis_ptb.settings.main import HISTORY_LENGTH_LONG_TURNS


class RenderingConfig(BaseModel):
    """Controls how DB messages are converted into system prompt + MessageParams.

    Pure data transformation — no tools, no side effects.
    """

    prompt_name: str = "anton_private"
    """Which system prompt to use (key into SYSTEM_PROMPTS)."""

    load_memory: bool = True
    """Load root workspace files into the system prompt.

    True = load all root files (HUMAN, BEHAVIOR, TOOLS, MEMORY, HUMAN_STATUS).
    False = don't load any workspace files.
    """

    list_skills: bool = True
    """Show available skills listing and enable read_skill tool."""

    max_history_length_turns: int = HISTORY_LENGTH_LONG_TURNS
    """How many DB message turns to fetch for context."""

    tool_result_truncation_after_n_turns: int | None = None
    """Truncate large (>=10KB) tool results in turns older than N from the end."""

    @model_validator(mode="before")
    @classmethod
    def _migrate_old_fields(cls, data):
        """Backward compat: rename old field names from archive agents in DB."""
        if not isinstance(data, dict):
            return data
        # autoload_memory_logic: "auto" | ["skill1"] | [] → bool
        if "autoload_memory_logic" in data:
            old = data.pop("autoload_memory_logic")
            data.setdefault("load_memory", bool(old))
        # list_all_memories → list_skills
        if "list_all_memories" in data:
            data.setdefault("list_skills", data.pop("list_all_memories"))
        return data
