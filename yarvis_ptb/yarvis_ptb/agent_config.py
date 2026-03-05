from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DEFAULT_TOOL_SUBSET: list[str] = ["python_repl", "bash_run", "editor"]


class AgentConfig(BaseModel):
    """Structured configuration for a subagent.

    Stored as JSON in the agent's meta column under the "agent_config" key.
    """

    description: str = ""
    model: str = "haiku"
    tool_subset: Literal["all"] | list[str] = Field(
        default_factory=lambda: list(DEFAULT_TOOL_SUBSET)
    )
    autoload_memories: list[str] = Field(default_factory=list)

    def to_meta(self) -> dict:
        """Serialize to a meta dict suitable for DB storage."""
        return {"agent_config": self.model_dump()}

    @classmethod
    def from_meta(cls, meta: dict) -> AgentConfig:
        """Deserialize from a DB meta dict with {"agent_config": {...}} format."""
        return cls.model_validate(meta["agent_config"])
