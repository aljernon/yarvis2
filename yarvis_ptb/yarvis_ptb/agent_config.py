from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from yarvis_ptb.rendering_config import RenderingConfig
from yarvis_ptb.sampling import SamplingConfig

DEFAULT_SUBAGENT_TOOL_SUBSET: list[str] = ["python_repl", "bash_run", "editor"]


class AgentConfig(BaseModel):
    """Universal agent configuration. Composes rendering + sampling.

    Used for both top-level agents and subagents.
    """

    rendering: RenderingConfig = Field(default_factory=RenderingConfig)

    sampling: SamplingConfig = Field(default_factory=SamplingConfig)

    @property
    def requires_memory_tools(self) -> bool:
        """Agent has skill listing -> needs read_skill tools."""
        return self.rendering.list_skills

    @property
    def requires_tool_output_tool(self) -> bool:
        """Rendering truncates old results -> agent needs get_tool_output tool."""
        return self.rendering.tool_result_truncation_after_n_turns is not None

    @property
    def requires_messaging_tool(self) -> bool:
        """Output mode is tool-based -> agent needs send_message tool."""
        return self.sampling.output_mode == "tool_message"


class AgentMeta(BaseModel):
    """Typed wrapper for the agents.meta JSONB column.

    All fields are optional for backward compatibility with existing records.
    """

    agent_config: AgentConfig = Field(default_factory=AgentConfig)
    type: str | None = None  # "dau_session", "auto_reflect"
    status: Literal["frozen"] | None = None
    summary: str | None = None
    last_prompt_tokens: int | None = None

    @property
    def is_frozen(self) -> bool:
        return self.status == "frozen"
