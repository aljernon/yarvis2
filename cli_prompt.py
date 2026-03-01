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
from yarvis_ptb.complex_chat import (
    COMPLEX_CHAT_PUT_CONTEXT_AT_THE_BEGINNING,
    COMPLEX_CHAT_PUT_CONTEXT_AT_THE_END,
    DEFAULT_COMPLEX_CHAT_CONFIG,
)
from yarvis_ptb.prompting import (
    build_claude_input,
    render_claude_response_short,
)
from yarvis_ptb.ptb_util import InterruptionScope, get_anthropic_client
from yarvis_ptb.settings import DEFAULT_TIMEZONE, ID_USER_MAP
from yarvis_ptb.settings.anton import USER_ANTON
from yarvis_ptb.settings.main import CONFIGURED_CHATS, load_env
from yarvis_ptb.storage import (
    DbMessage,
    Invocation,
    connect,
    get_messages,
    get_schedules,
)
from yarvis_ptb.tool_sampler import _DummyJobQueue, get_tools_for_config
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
    config: str | None = typer.Option(None, help="Chat config name"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Verbose output with tool details"
    ),
):
    asyncio.run(main(prompt=prompt, config_name=config, verbose=verbose))


async def main(prompt: str, config_name: str | None, verbose: bool):
    load_env()
    setup_logging()

    chat_id = ID_USER_MAP[USER_ANTON]
    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    chat_config = (
        CONFIGURED_CHATS[config_name] if config_name else DEFAULT_COMPLEX_CHAT_CONFIG
    )

    with connect() as conn:
        with conn.cursor() as curr:
            # Load history from DB
            db_messages = get_messages(
                curr, chat_id=chat_id, limit=chat_config.max_history_length_turns
            )
            initial_db_message = DbMessage(
                created_at=now,
                chat_id=chat_id,
                user_id=chat_id,
                message=prompt,
            )
            db_messages = [*db_messages, initial_db_message][
                -chat_config.max_history_length_turns :
            ]

            scheduled_invocations = get_schedules(curr, chat_id)

            # Build system prompt + history
            invocation = Invocation(invocation_type="reply")
            system, message_params = build_claude_input(
                db_messages,
                chat_config,
                invocation=invocation,
                put_context_at_the_beginning=COMPLEX_CHAT_PUT_CONTEXT_AT_THE_BEGINNING,
                put_context_at_the_end=COMPLEX_CHAT_PUT_CONTEXT_AT_THE_END,
                scheduled_invocations=scheduled_invocations,
                forced_now_date=now,
            )

            client = get_anthropic_client()
            scope = InterruptionScope(chat_id=chat_id, message_id=None)

            from telegram import Bot

            bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])

            # Build tools, then intercept send_message to print to terminal
            all_local_tool_objects = get_tools_for_config(
                chat_config, curr, chat_id, bot
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

                (
                    result_params,
                    claude_calls,
                ) = await tool_sampler._process_query_with_tools(
                    system=system,
                    messages=message_params,
                    claude_tools=claude_tools,
                    all_local_tool_objects=tool_map,
                    on_update=tool_sampler.dummy_on_update,
                    scope=scope,
                    model_name=chat_config.model_name,
                    job_queue=_DummyJobQueue(),
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
                cost = tool_sampler.estimate_cost(claude_calls, chat_config.model_name)
                cost_str = f", ${cost:.4f}" if cost is not None else ""
                print(
                    f"\n[{len(claude_calls)} API call(s), {total_prompt} prompt tokens, {total_cached} cached, {total_output} output{cost_str}]",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    app()
