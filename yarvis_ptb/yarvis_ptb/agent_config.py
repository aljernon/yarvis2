from __future__ import annotations

from pydantic import BaseModel, Field

from yarvis_ptb.rendering_config import RenderingConfig
from yarvis_ptb.sampling import SamplingConfig

DEFAULT_SUBAGENT_TOOL_SUBSET: list[str] = ["python_repl", "bash_run", "editor"]


class AgentConfig(BaseModel):
    """Universal agent configuration. Composes rendering + sampling.

    Stored as JSON in the agent's meta column under the "agent_config" key.
    Used for both top-level agents and subagents.
    """

    description: str = ""

    rendering: RenderingConfig = Field(default_factory=RenderingConfig)

    sampling: SamplingConfig = Field(default_factory=SamplingConfig)

    @property
    def requires_memory_tools(self) -> bool:
        """Rendering includes memories -> agent needs read/write_memory tools."""
        return self.rendering.include_memories

    @property
    def requires_tool_output_tool(self) -> bool:
        """Rendering truncates old results -> agent needs get_tool_output tool."""
        return self.rendering.tool_result_truncation_after_n_turns is not None

    @property
    def requires_messaging_tool(self) -> bool:
        """Output mode is tool-based -> agent needs send_message tool."""
        return self.sampling.output_mode == "tool_message"

    def to_meta(self) -> dict:
        """Serialize to a meta dict suitable for DB storage."""
        return {"agent_config": self.model_dump()}

    @classmethod
    def from_meta(cls, meta: dict) -> AgentConfig:
        """Deserialize from a DB meta dict with {"agent_config": {...}} format."""
        return cls.model_validate(meta["agent_config"])
