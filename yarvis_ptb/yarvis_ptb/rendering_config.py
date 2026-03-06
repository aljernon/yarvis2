from typing import Literal

from pydantic import BaseModel

from yarvis_ptb.settings.main import HISTORY_LENGTH_LONG_TURNS


class RenderingConfig(BaseModel):
    """Controls how DB messages are converted into system prompt + MessageParams.

    Pure data transformation — no tools, no side effects.
    """

    prompt_name: str = "anton_private"
    """Which system prompt to use (key into SYSTEM_PROMPTS)."""

    autoload_memory_logic: list[str] | Literal["auto"] = "auto"
    """Which CKR skill files to preload into the system prompt.

    - ``"auto"`` — all files with ``autoload: true`` in frontmatter (default)
    - ``["logseq", "whoop"]`` — specific skills by folder name
    - ``[]`` — none
    """

    list_all_memories: bool = True
    """Show catalogue of all available CKR skills and enable read_memory tool."""

    max_history_length_turns: int = HISTORY_LENGTH_LONG_TURNS
    """How many DB message turns to fetch for context."""

    tool_result_truncation_after_n_turns: int | None = None
    """Truncate large (>=10KB) tool results in turns older than N from the end."""
