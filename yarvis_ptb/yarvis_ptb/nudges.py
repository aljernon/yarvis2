"""Post-reply nudges — sequential self-checks run after each bot reply.

Each Nudge defines a prompt, a condition for when it should fire, and a
condition for when its result should be persisted visibly in conversation
history (vs hidden as an auto-message).
"""

import datetime
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack

from telegram import Bot
from telegram.ext import Application

from yarvis_ptb import tool_sampler
from yarvis_ptb.agent_config import AgentConfig
from yarvis_ptb.message_search import save_message_and_update_index
from yarvis_ptb.prompting import build_claude_input
from yarvis_ptb.ptb_util import InterruptionScope
from yarvis_ptb.sampling import NoOpHooks
from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE, SYSTEM_USER_ID
from yarvis_ptb.settings.main import ROOT_AGENT_SLUG
from yarvis_ptb.storage import DbMessage, Invocation, get_messages, get_schedules
from yarvis_ptb.tools.collect_message_tool import NoOpSendMessageTool
from yarvis_ptb.turns import system_turn_meta

logger = logging.getLogger(__name__)

NON_RESEARCH_TOOLS = {
    "send_message",
    "schedule",
    "todo_read",
    "todo_write",
    "set_variable",
    "forget_above",
}


class Nudge:
    """A post-reply self-check that runs under specific conditions."""

    def __init__(
        self,
        name: str,
        prompt: str,
        should_run: Callable[[set[str]], bool],
        should_persist: Callable[[set[str]], bool],
        noop_send_message: bool = True,
    ):
        self.name = name
        self.prompt = prompt
        self.should_run = should_run
        self.should_persist = should_persist
        self.noop_send_message = noop_send_message


NUDGES: list[Nudge] = [
    Nudge(
        name="forgot-send",
        prompt=(
            "Self-check: I didn't call send_message this turn. "
            "The caller (human or agent) can't see any messages unless send_message is used. "
            "Was that intentional, or did I forget? "
            "If I forgot, let's call send_message now. "
            "Do not continue or finish any interrupted messages — only check if send_message was missed."
        ),
        should_run=lambda tools: "send_message" not in tools,
        should_persist=lambda nudge_tools: "send_message" in nudge_tools,
        noop_send_message=False,
    ),
    Nudge(
        name="research-check",
        prompt=(
            "Self-check on my last reply. Identify every person, place, event, "
            "or topic I referenced by name.\n\n"
            "For EACH reference, complete ALL steps:\n"
            "1. ls logseq/pages/ | grep -i <name> — check for dedicated page\n"
            "2. If step 1 found a page: head -50 the page to pull key facts into context "
            "(skip if already read this conversation)\n"
            "3. grep -rC2 <name> logseq/ workspace/ — find mentions with context\n"
            "4. If step 1 found NO dedicated page file: I lack deep knowledge on "
            "this topic. I MUST run read_skill brave-search followed by python_repl "
            "to search the web. This is not optional — finding scattered mentions "
            "in step 3 does not make up for lacking a dedicated knowledge page.\n\n"
            "Also available: message_subagent with archive/ agents for past conversations.\n"
            "If nothing named, do nothing. NEVER send_message. "
            "Do not continue or finish any interrupted messages — only do research."
        ),
        should_run=lambda tools: (
            "send_message" in tools and tools <= NON_RESEARCH_TOOLS
        ),
        should_persist=lambda nudge_tools: bool(nudge_tools - {"send_message"}),
        noop_send_message=True,
    ),
]


def _extract_tool_names(message_params: list[dict]) -> set[str]:
    return {
        block["name"]
        for mp in message_params
        if mp.get("role") == "assistant" and isinstance(mp.get("content"), list)
        for block in mp["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }


async def run_nudges(
    curr,
    bot: Bot,
    chat_id: int,
    agent_config: AgentConfig,
    all_tools: list,
    application: Application,
    original_tool_names: set[str],
) -> None:
    """Run all applicable nudges in sequence."""
    for nudge in NUDGES:
        if not nudge.should_run(original_tool_names):
            continue

        logger.info(f"Nudge [{nudge.name}]: starting")
        rendering_config = agent_config.rendering
        now = datetime.datetime.now(DEFAULT_TIMEZONE)

        db_messages = get_messages(
            curr, chat_id=chat_id, limit=rendering_config.max_history_length_turns
        )

        nudge_db_msg = DbMessage(
            created_at=now,
            chat_id=chat_id,
            user_id=SYSTEM_USER_ID,
            message=nudge.prompt,
            meta=system_turn_meta("reflection"),
        )
        db_messages = [*db_messages, nudge_db_msg][
            -rendering_config.max_history_length_turns :
        ]

        scheduled_invocations = get_schedules(curr, chat_id)
        invocation = Invocation(invocation_type="reply")
        system, message_params, _ = build_claude_input(
            db_messages,
            rendering_config,
            invocation=invocation,
            scheduled_invocations=scheduled_invocations,
            forced_now_date=now,
            agent_slug=ROOT_AGENT_SLUG,
        )

        scope = InterruptionScope(chat_id=chat_id, message_id=None)

        tool_map = {
            t.name: NoOpSendMessageTool(t.spec())
            if nudge.noop_send_message and t.name == "send_message"
            else t
            for t in all_tools
        }

        claude_tools = [tool_map[t.name].spec().to_claude_tool() for t in all_tools]

        async with AsyncExitStack() as stack:
            for t in tool_map.values():
                await stack.enter_async_context(t.context())

            model_name = agent_config.sampling.resolve_model_name()
            (
                result_params,
                nudge_claude_calls,
                _interrupted,
            ) = await tool_sampler._process_query_with_tools(
                system=system,
                messages=message_params,
                claude_tools=claude_tools,
                all_local_tool_objects=tool_map,
                on_update=NoOpHooks().on_update,
                scope=scope,
                model_name=model_name,
                job_queue=application.job_queue,  # type: ignore[arg-type]
                max_tokens=agent_config.sampling.max_tokens,
                thinking=agent_config.sampling.thinking,
                effort=agent_config.sampling.effort,
            )

        if not result_params:
            result_params = []
        nudge_tool_names = _extract_tool_names(result_params)
        persist = nudge.should_persist(nudge_tool_names)

        if not persist:
            nudge_db_msg.is_hidden_auto_message = True
        save_message_and_update_index(curr, nudge_db_msg)

        if result_params:
            bot_meta: dict = {"message_params": result_params}
            if nudge_claude_calls:
                pricing = tool_sampler.MODEL_PRICING.get(model_name)
                if pricing is None:
                    logger.warning(
                        f"No MODEL_PRICING entry for {model_name!r}; "
                        f"saving nudge usage with cost=0 for {len(nudge_claude_calls)} calls"
                    )
                nudge_cost = tool_sampler.estimate_cost(nudge_claude_calls, model_name)
                bot_meta["usage"] = {
                    "model": model_name,
                    "calls": [c.to_usage_dict(pricing) for c in nudge_claude_calls],
                    "estimated_cost_usd": nudge_cost,
                    "cost_breakdown_usd": tool_sampler.cost_breakdown(
                        nudge_claude_calls, model_name
                    ),
                }
            bot_msg = DbMessage(
                chat_id=chat_id,
                created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                user_id=BOT_USER_ID,
                message="USE_CONTENT_FROM_META",
                meta=bot_meta,
                is_hidden_auto_message=not persist,
            )
            save_message_and_update_index(curr, bot_msg)

        if persist:
            logger.info(f"Nudge [{nudge.name}]: acted, messages kept visible")
        else:
            logger.info(f"Nudge [{nudge.name}]: no action, messages hidden")
