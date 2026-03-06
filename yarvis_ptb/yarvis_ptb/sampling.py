from __future__ import annotations

from typing import Literal, Protocol

from anthropic.types import MessageParam
from pydantic import BaseModel


class SamplingConfig(BaseModel):
    """Controls how the Claude API is called.

    Given rendered MessageParams, how do we produce new ones.
    """

    model: str = "opus"
    """Model short name or full ID. Short names resolved via SUBAGENT_MODEL_MAP."""

    tool_subset: Literal["all"] | list[str] = "all"
    """Which tools to make available. "all" = full power."""

    max_tokens: int = 16000
    """Max output tokens per API call."""

    thinking: Literal["adaptive", "none"] = "adaptive"
    """Thinking mode for the model."""

    output_mode: Literal["text", "tool_message"] = "text"
    """How the agent communicates its response.

    - "text": agent responds via regular text output
    - "tool_message": agent responds via send_message tool calls
    """

    def resolve_model_name(self) -> str:
        """Resolve short name to full model ID."""
        from yarvis_ptb.settings.main import SUBAGENT_MODEL_MAP

        return SUBAGENT_MODEL_MAP.get(self.model, self.model)


class SamplingHooks(Protocol):
    """Callbacks for the sampling loop.

    Implementations control delivery (Telegram, collecting, no-op).
    """

    async def on_update(self, accumulated_params: list[MessageParam]) -> None:
        """Called during streaming text deltas and after tool results."""
        ...

    @property
    def is_interrupted(self) -> bool:
        """Check if generation should be cancelled."""
        ...


class NoOpHooks:
    """Minimal hooks implementation for non-interactive use (subagents)."""

    async def on_update(self, accumulated_params: list[MessageParam]) -> None:
        pass

    @property
    def is_interrupted(self) -> bool:
        return False


class SamplingResult(BaseModel):
    """Result of a sampling run."""

    class Config:
        arbitrary_types_allowed = True

    message_params: list[dict] = []
    """Full turn history from this generation (assistant + tool_result turns)."""

    agent_messages: list[str] = []
    """Extracted agent output messages (from text or send_message tool calls)."""

    claude_calls: list = []
    """List of ClaudeCallInfo for token tracking."""

    subagent_usages: list[dict] = []
    """Cost dicts from any subagent invocations."""

    tool_init_time: float = 0.0
    """Time spent initializing tools."""
