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
from yarvis_ptb.settings import DEFAULT_TIMEZONE, ID_USER_MAP, SYSTEM_USER_ID
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
from yarvis_ptb.tools.collect_message_tool import NoOpSendMessageTool
from yarvis_ptb.tools.tool_spec import LocalTool, ToolResult, ToolSpec
from yarvis_ptb.turns import db_message_to_turn
from yarvis_ptb.yarvis_ptb.logging import setup_logging

app = typer.Typer()


class InteractiveTool(LocalTool):
    """Wraps a tool to print calls and ask for confirmation before executing."""

    def __init__(self, inner: LocalTool):
        self._inner = inner

    def spec(self) -> ToolSpec:
        return self._inner.spec()

    async def init(self):
        await self._inner.init()

    async def close(self):
        await self._inner.close()

    def context(self):
        return self._inner.context()

    async def _execute(self, **kwargs) -> ToolResult:
        # Format the call for display
        name = self._inner.name
        parts = []
        for k, v in kwargs.items():
            s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            if len(s) > 120 or "\n" in s:
                parts.append(
                    f"  \033[1m{k}\033[0m\n  {s[:200]}{'...' if len(s) > 200 else ''}"
                )
            else:
                parts.append(f"  \033[1m{k}:\033[0m {s}")
        args_str = "\n".join(parts)
        print(f"\n\033[33m▶ {name}\033[0m\n{args_str}", file=sys.stderr)

        # Ask for confirmation
        try:
            answer = input("\033[36m  [y/n/q] \033[0m").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "q"

        if answer == "q":
            raise KeyboardInterrupt("User quit")
        if answer != "y" and answer != "":
            return ToolResult.error("Tool call rejected by user")

        return await self._inner._execute(**kwargs)


class CliHooks:
    """Hooks that print tool results as they come in."""

    async def on_update(self, accumulated_params: list) -> None:
        # Print the latest turn if it's a tool result
        if not accumulated_params:
            return
        last = accumulated_params[-1]
        if last.get("role") == "user" and isinstance(last.get("content"), list):
            for block in last["content"]:
                if block.get("type") == "tool_result":
                    text = ""
                    if isinstance(block.get("content"), list):
                        text = " ".join(c.get("text", "") for c in block["content"])
                    elif isinstance(block.get("content"), str):
                        text = block["content"]
                    preview = text
                    color = "\033[31m" if block.get("is_error") else "\033[32m"
                    print(f"{color}  ← {preview}\033[0m", file=sys.stderr)

    @property
    def is_interrupted(self) -> bool:
        return False


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
    system_msg: bool = typer.Option(
        False, "--system", "-s", help="Send as system message (SYSTEM_USER_ID)"
    ),
    interactive: bool = typer.Option(
        False, "--interactive", "-i", help="Confirm each tool call before executing"
    ),
    after: int = typer.Option(
        None,
        "--after",
        help="Insert prompt after this DB message ID (truncates later history)",
    ),
    sticky: bool = typer.Option(
        False,
        "--sticky",
        help="Set message time to 5s after the last message in history",
    ),
):
    asyncio.run(
        main(
            prompt=prompt,
            verbose=verbose,
            agent=agent,
            system_msg=system_msg,
            interactive=interactive,
            after=after,
            sticky=sticky,
        )
    )


async def main(
    prompt: str,
    verbose: bool,
    agent: str | None = None,
    system_msg: bool = False,
    interactive: bool = False,
    after: int | None = None,
    sticky: bool = False,
):
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
            if after is not None:
                cut = next(
                    (i + 1 for i, m in enumerate(db_messages) if m.message_id == after),
                    None,
                )
                if cut is None:
                    print(
                        f"Message ID {after} not found in history.",
                        file=sys.stderr,
                    )
                    raise typer.Exit(1)
                db_messages = db_messages[:cut]
                print(
                    f"[history truncated after message #{after}, {cut} messages kept]",
                    file=sys.stderr,
                )
            if db_messages:
                last = db_messages[-1]
                turn = db_message_to_turn(last)
                preview = render_claude_response_short(turn.render())[:120].replace(
                    "\n", "\\n"
                )
                print(
                    f"[last msg: #{last.message_id} user={last.user_id} {last.created_at:%H:%M:%S}] {preview}",
                    file=sys.stderr,
                )
                if sticky:
                    now = last.created_at + datetime.timedelta(seconds=5)
            msg_user_id = SYSTEM_USER_ID if system_msg else chat_id
            msg_meta = {"turn_type": "notification"} if system_msg else None
            print(f"[injected msg time: {now:%Y-%m-%d %H:%M:%S}]", file=sys.stderr)
            initial_db_message = DbMessage(
                created_at=now,
                chat_id=chat_id,
                user_id=msg_user_id,
                message=prompt,
                meta=msg_meta,
            )
            db_messages = [*db_messages, initial_db_message][
                -rendering_config.max_history_length_turns :
            ]

            scheduled_invocations = get_schedules(curr, chat_id)

            # Build system prompt + history
            invocation = Invocation(invocation_type="reply")
            system_prompt, message_params = build_claude_input(
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
            tool_map: dict[str, LocalTool] = {}
            for t in all_local_tool_objects:
                if t.name == "send_message":
                    tool_map[t.name] = NoOpSendMessageTool(t.spec())
                elif interactive:
                    tool_map[t.name] = InteractiveTool(t)
                else:
                    tool_map[t.name] = t

            claude_tools = [
                tool_map[t.name].spec().to_claude_tool() for t in all_local_tool_objects
            ]

            hooks = CliHooks() if interactive else NoOpHooks()

            from contextlib import AsyncExitStack

            async with AsyncExitStack() as stack:
                for t in tool_map.values():
                    await stack.enter_async_context(t.context())

                model_name = sampling_config.resolve_model_name()
                (
                    result_params,
                    claude_calls,
                    _interrupted,
                ) = await tool_sampler._process_query_with_tools(
                    system=system_prompt,
                    messages=message_params,
                    claude_tools=claude_tools,
                    all_local_tool_objects=tool_map,
                    on_update=hooks.on_update,
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
