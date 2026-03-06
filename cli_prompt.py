"""CLI entry point: send a prompt into the current agent state, print results, don't save to DB.

The send_message tool is intercepted so responses print to terminal instead of Telegram.
"""

import asyncio
import datetime
import json
import os
import sys

import typer

from yarvis_ptb import tool_sampler
from yarvis_ptb.agent_config import AgentMeta
from yarvis_ptb.complex_chat import DEFAULT_AGENT_CONFIG
from yarvis_ptb.prompting import (
    build_claude_input,
    render_claude_response_short,
)
from yarvis_ptb.ptb_util import InterruptionScope
from yarvis_ptb.sampling import NoOpHooks
from yarvis_ptb.settings import DEFAULT_TIMEZONE, ID_USER_MAP
from yarvis_ptb.settings.anton import USER_ANTON
from yarvis_ptb.settings.main import load_env
from yarvis_ptb.storage import (
    DbMessage,
    Invocation,
    connect,
    get_agent_by_slug,
    get_messages,
    get_schedules,
)
from yarvis_ptb.tool_sampler import _DummyJobQueue, get_tools_for_agent_config
from yarvis_ptb.tools.tool_spec import LocalTool, ToolResult
from yarvis_ptb.yarvis_ptb.logging import setup_logging

app = typer.Typer()


class _CliSendMessageTool(LocalTool):
    """Intercepts send_message: prints to terminal instead of sending to Telegram."""

    def __init__(self):
        self._original_spec = None

    def set_original_spec(self, spec):
        self._original_spec = spec

    def spec(self):
        return self._original_spec

    async def _execute(self, **kwargs) -> ToolResult:
        message = kwargs["message"]
        print(f"\n{'='*60}\n[send_message]\n{message}\n{'='*60}\n")
        return ToolResult.success(
            "Message sent successfully.",
            stop_after=bool(kwargs.get("final", False)),
        )


@app.command()
def run(
    prompt: str = typer.Argument(..., help="Prompt to send to the agent"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Verbose output with tool details"
    ),
    agent: str = typer.Option(
        None,
        "--agent",
        "-a",
        help="Agent slug to chat with (loads agent's config and history)",
    ),
):
    asyncio.run(main(prompt=prompt, verbose=verbose, agent=agent))


async def main(prompt: str, verbose: bool, agent: str | None = None):
    load_env()
    setup_logging()

    chat_id = ID_USER_MAP[USER_ANTON]
    now = datetime.datetime.now(DEFAULT_TIMEZONE)

    with connect() as conn:
        with conn.cursor() as curr:
            # Resolve agent config
            agent_id = None
            if agent is not None:
                result = get_agent_by_slug(curr, chat_id, agent)
                if result is None:
                    print(f"Agent '{agent}' not found.", file=sys.stderr)
                    raise typer.Exit(1)
                agent_id, raw_meta = result
                agent_meta = AgentMeta.model_validate(raw_meta)
                agent_config = agent_meta.agent_config
                print(f"[agent: {agent} (id={agent_id})]", file=sys.stderr)
            else:
                agent_config = DEFAULT_AGENT_CONFIG
            rendering_config = agent_config.rendering
            sampling_config = agent_config.sampling

            # Load history from DB
            db_messages = get_messages(
                curr,
                chat_id=chat_id,
                limit=rendering_config.max_history_length_turns,
                agent_id=agent_id,
            )
            initial_db_message = DbMessage(
                created_at=now,
                chat_id=chat_id,
                user_id=chat_id,
                message=prompt,
            )
            db_messages = [*db_messages, initial_db_message][
                -rendering_config.max_history_length_turns :
            ]

            scheduled_invocations = get_schedules(curr, chat_id)

            # Build system prompt + history
            invocation = Invocation(invocation_type="reply")
            system, message_params = build_claude_input(
                db_messages,
                rendering_config,
                invocation=invocation,
                scheduled_invocations=scheduled_invocations,
                forced_now_date=now,
            )

            scope = InterruptionScope(chat_id=chat_id, message_id=None)

            from telegram import Bot

            bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])

            # Build tools, then intercept send_message to print to terminal
            all_local_tool_objects = get_tools_for_agent_config(
                agent_config, curr, chat_id, bot
            )
            tool_map = {}
            for t in all_local_tool_objects:
                if t.name == "send_message":
                    cli_tool = _CliSendMessageTool()
                    cli_tool.set_original_spec(t.spec())
                    tool_map[t.name] = cli_tool
                else:
                    tool_map[t.name] = t

            claude_tools = [
                tool_map[t.name].spec().to_claude_tool() for t in all_local_tool_objects
            ]

            from contextlib import AsyncExitStack

            async with AsyncExitStack() as stack:
                for t in tool_map.values():
                    await stack.enter_async_context(t.context())

                model_name = sampling_config.resolve_model_name()
                (
                    result_params,
                    claude_calls,
                ) = await tool_sampler._process_query_with_tools(
                    system=system,
                    messages=message_params,
                    claude_tools=claude_tools,
                    all_local_tool_objects=tool_map,
                    on_update=NoOpHooks().on_update,
                    scope=scope,
                    model_name=model_name,
                    job_queue=_DummyJobQueue(),
                    max_tokens=sampling_config.max_tokens,
                    thinking=sampling_config.thinking,
                )

            if not result_params:
                print("(no response)")
                return

            if verbose:
                print(json.dumps(result_params, indent=2, default=str))
            else:
                print(render_claude_response_short(result_params))

            if claude_calls:
                total_prompt = sum(c.num_prompt_tokens for c in claude_calls)
                total_cached = sum(c.num_cached_tokens for c in claude_calls)
                total_output = sum(c.num_output_tokens for c in claude_calls)
                cost = tool_sampler.estimate_cost(claude_calls, model_name)
                cost_str = f", ${cost:.4f}" if cost is not None else ""
                print(
                    f"\n[{len(claude_calls)} API call(s), {total_prompt} prompt tokens, {total_cached} cached, {total_output} output{cost_str}]",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    app()
