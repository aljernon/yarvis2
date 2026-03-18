"""Standalone agent runner — run a Claude invocation under an agent_id.

Extracted from subagent_tool to be usable from both tool calls and scheduled
invocations without going through process_multi_message_claude_invocation.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from yarvis_ptb.agent_config import AgentConfig, AgentMeta
from yarvis_ptb.agent_slugs import generate_agent_slug
from yarvis_ptb.debug_chat import add_debug_message_to_queue
from yarvis_ptb.interruption_scope_internal import INTERRUPTABLES
from yarvis_ptb.prompting import build_claude_input
from yarvis_ptb.ptb_util import InterruptionScope
from yarvis_ptb.rendering_config import RenderingConfig
from yarvis_ptb.sampling import NoOpHooks, SamplingConfig, SamplingResult
from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE, SYSTEM_USER_ID
from yarvis_ptb.settings.main import SUBAGENT_MODEL_MAP
from yarvis_ptb.storage import (
    DbMessage,
    create_agent,
    get_agent_by_slug,
    get_messages,
    save_message,
    update_agent_meta,
)
from yarvis_ptb.tools.subagent_tool import YARVIS_SUBAGENT_SYSTEM_MESSAGE

logger = logging.getLogger(__name__)


@dataclass
class AgentRunResult:
    """Result of running an agent."""

    result: SamplingResult
    model_id: str
    agent_id: int
    slug: str


async def create_and_run_agent(
    curr,
    chat_id: int,
    bot,
    *,
    message: str,
    agent_config: AgentConfig | None = None,
) -> AgentRunResult:
    """Create a new yarvis subagent, inject setup messages, run the task, return result.

    Handles the full lifecycle: agent creation, system message injection,
    running the query, and saving to DB. Caller doesn't need to know
    subagent internals.
    """
    if agent_config is None:
        agent_config = AgentConfig(
            rendering=RenderingConfig(
                prompt_name="anton_private",
                autoload_memory_logic="auto",
                list_all_memories=True,
                tool_result_truncation_after_n_turns=0,
            ),
            sampling=SamplingConfig(
                model="opus",
                tool_subset="all",
                output_mode="tool_message",
                collect_messages=True,
            ),
        )

    slug = generate_agent_slug()
    agent_id = create_agent(
        curr, chat_id, meta=AgentMeta(agent_config=agent_config).model_dump(), slug=slug
    )
    logger.info(f"create_and_run_agent: created {slug} (id={agent_id})")

    # Inject subagent system message
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    save_message(
        curr,
        DbMessage(
            created_at=now,
            chat_id=chat_id,
            user_id=SYSTEM_USER_ID,
            message=YARVIS_SUBAGENT_SYSTEM_MESSAGE,
            agent_id=agent_id,
        ),
    )

    return await run_as_agent(
        curr,
        chat_id,
        bot,
        agent_slug=slug,
        message=message,
        agent_config=agent_config,
    )


async def run_as_agent(
    curr,
    chat_id: int,
    bot,
    *,
    agent_slug: str,
    message: str,
    agent_config: AgentConfig | None = None,
    save_to_db: bool = True,
) -> AgentRunResult:
    """Run a Claude invocation under an existing agent_id.

    Builds prompt from the agent's history, runs the query with full tools,
    and optionally saves the exchange to the agent's DB history.

    Returns AgentRunResult with the sampling result and metadata.
    """
    # 1. Load agent
    result = get_agent_by_slug(curr, chat_id, agent_slug)
    if result is None:
        raise ValueError(f"Agent '{agent_slug}' not found")
    agent_id, raw_meta = result
    agent_meta = AgentMeta.model_validate(raw_meta)
    if agent_config is None:
        agent_config = agent_meta.agent_config

    model_id = SUBAGENT_MODEL_MAP.get(agent_config.sampling.model)
    if model_id is None:
        raise ValueError(f"Unknown model '{agent_config.sampling.model}'")

    logger.info(f"run_as_agent: {agent_slug} (id={agent_id}) model={model_id}")

    # 2. Build prompt from agent's history
    db_msgs = get_messages(curr, chat_id, agent_id=agent_id)
    system, messages = build_claude_input(
        db_msgs,
        agent_config.rendering,
        agent_slug=agent_slug,
    )
    messages.append({"role": "user", "content": message})

    # 3. Run query with tools
    # Deferred import: circular dependency (tool_sampler imports subagent_tool)
    from yarvis_ptb.tool_sampler import (
        _DummyJobQueue,
        get_tools_for_agent_config,
        process_query,
    )

    parent_scope = None
    for s in reversed(INTERRUPTABLES):
        if s.chat_id == chat_id:
            parent_scope = s
            break
    if parent_scope is None:
        parent_scope = InterruptionScope(chat_id=chat_id, message_id=None)

    tools = get_tools_for_agent_config(
        agent_config, curr, chat_id, bot, agent_slug=agent_slug
    )

    sampling_result = await process_query(
        system=system,
        messages=messages,
        agent_config=agent_config,
        tools=tools,
        hooks=NoOpHooks(),
        job_queue=_DummyJobQueue(),
        scope=parent_scope,
    )

    # 4. Save to DB under agent_id
    if save_to_db:
        _save_agent_exchange(
            curr, chat_id, agent_id, agent_slug, message, sampling_result, model_id
        )

    # 5. Debug chat
    from yarvis_ptb.tool_sampler import estimate_cost

    cost = estimate_cost(sampling_result.claude_calls, model_id)
    cost_str = f", cost: ${cost:.3f}" if cost else ""
    add_debug_message_to_queue(
        f"**AGENT {agent_slug}** (model: {model_id}{cost_str}, msg: {message[:100]})"
    )
    if sampling_result.message_params:
        add_debug_message_to_queue(sampling_result.message_params)

    return AgentRunResult(
        result=sampling_result,
        model_id=model_id,
        agent_id=agent_id,
        slug=agent_slug,
    )


def extract_agent_messages(result: SamplingResult) -> list[str]:
    """Extract send_message texts from a sampling result."""
    if result.agent_messages:
        return result.agent_messages
    # Fallback: extract final assistant text
    for mp in reversed(result.message_params):
        if mp.get("role") == "assistant":
            for block in mp.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    return [block["text"]]
    return []


def _save_agent_exchange(
    curr,
    chat_id: int,
    agent_id: int,
    slug: str,
    message: str,
    result: SamplingResult,
    model_id: str,
) -> None:
    """Save user message + bot response to agent's DB history."""
    from yarvis_ptb.tool_sampler import MODEL_PRICING, cost_breakdown, estimate_cost

    now = datetime.datetime.now(DEFAULT_TIMEZONE)

    # User message
    save_message(
        curr,
        DbMessage(
            created_at=now,
            chat_id=chat_id,
            user_id=SYSTEM_USER_ID,
            message=message,
            agent_id=agent_id,
        ),
    )

    # Bot response
    if result.message_params:
        bot_meta: dict = {"message_params": result.message_params}
        if result.claude_calls:
            pricing = MODEL_PRICING.get(model_id)
            bot_meta["usage"] = {
                "model": model_id,
                "calls": [c.to_usage_dict(pricing) for c in result.claude_calls],
                "estimated_cost_usd": estimate_cost(result.claude_calls, model_id),
                "cost_breakdown_usd": cost_breakdown(result.claude_calls, model_id),
            }
        save_message(
            curr,
            DbMessage(
                created_at=now,
                chat_id=chat_id,
                user_id=BOT_USER_ID,
                message="USE_CONTENT_FROM_META",
                meta=bot_meta,
                agent_id=agent_id,
            ),
        )

    # Track prompt tokens for frozen detection
    if result.claude_calls:
        last_prompt_tokens = max(c.num_prompt_tokens for c in result.claude_calls)
        update_agent_meta(curr, agent_id, {"last_prompt_tokens": last_prompt_tokens})
