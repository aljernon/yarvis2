"""Replay a specific message: load history up to a given message ID, invoke the model, print response.

Usage:
    SETTINGS_NAME=anton conda run -n clam python replay_message.py 16956
    SETTINGS_NAME=anton conda run -n clam python replay_message.py 16956 --thinking=none
    SETTINGS_NAME=anton conda run -n clam python replay_message.py 16956 --model=sonnet
    SETTINGS_NAME=anton conda run -n clam python replay_message.py 16956 --system-suffix="Always verify claims with tools before responding."
    SETTINGS_NAME=anton conda run -n clam python replay_message.py 16957 --append-message="You don't remember?"

Nothing is saved to DB — connection is rolled back on exit.
send_message is replaced with CollectMessageTool (no Telegram send).
CKR (core_knowledge/) is reset to CKR_RESET_COMMIT after each run.
"""

import asyncio
import datetime
import json
import os
import subprocess
import sys

import typer

from replay_prompts import PROMPTS
from yarvis_ptb import tool_sampler
from yarvis_ptb.complex_chat import DEFAULT_AGENT_CONFIG
from yarvis_ptb.prompting import build_claude_input, render_claude_response_verbose
from yarvis_ptb.ptb_util import InterruptionScope
from yarvis_ptb.sampling import NoOpHooks, SamplingConfig
from yarvis_ptb.settings import ID_USER_MAP
from yarvis_ptb.settings.anton import USER_ANTON
from yarvis_ptb.settings.main import load_env
from yarvis_ptb.storage import (
    DbMessage,
    Invocation,
    connect,
    get_messages,
    get_schedules,
)
from yarvis_ptb.tool_sampler import _DummyJobQueue, get_tools_for_agent_config
from yarvis_ptb.tools.collect_message_tool import NoOpSendMessageTool
from yarvis_ptb.yarvis_ptb.logging import setup_logging

CKR_PATH = os.path.join(os.path.dirname(__file__), "core_knowledge")


def _is_context_message(msg: dict) -> bool:
    """Return True if this message contains a <context> injection block."""
    content = msg.get("content")
    if isinstance(content, str):
        return "<context>" in content
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and "<context>" in b.get("text", "") for b in content
        )
    return False


def _clean_message_params_for_jsonl(message_params: list) -> list:
    """Strip images and thinking signatures; preserve context messages as-is for correct formatting."""
    cleaned = []
    for msg in message_params:
        content = msg.get("content")
        if isinstance(content, list):
            new_blocks = []
            for block in content:
                if not isinstance(block, dict):
                    new_blocks.append(block)
                    continue
                btype = block.get("type")
                if btype == "image":
                    continue
                if btype == "thinking":
                    block = {k: v for k, v in block.items() if k != "signature"}
                if btype == "tool_result":
                    inner = block.get("content")
                    if isinstance(inner, list):
                        inner = [
                            b
                            for b in inner
                            if not (isinstance(b, dict) and b.get("type") == "image")
                        ]
                        block = {**block, "content": inner}
                new_blocks.append(block)
            msg = {**msg, "content": new_blocks}
        cleaned.append(msg)
    return cleaned


app = typer.Typer()


def _get_ckr_commit_before(dt: datetime.datetime) -> str:
    """Find the last CKR commit on main before the given datetime."""
    iso = dt.isoformat()
    result = subprocess.run(
        ["git", "log", "main", f"--before={iso}", "--format=%H", "-1"],
        cwd=CKR_PATH,
        capture_output=True,
        text=True,
        check=True,
    )
    commit = result.stdout.strip()
    if not commit:
        raise RuntimeError(f"No CKR commit found before {iso}")
    return commit


def _ckr_checkout(commit: str):
    """Check out a specific CKR commit, discarding any local changes."""
    subprocess.run(
        ["git", "checkout", "."], cwd=CKR_PATH, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "clean", "-fd"], cwd=CKR_PATH, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "checkout", commit], cwd=CKR_PATH, check=True, capture_output=True
    )
    short = commit[:7]
    print(f"[CKR checked out to {short}]", file=sys.stderr)


def _ckr_restore_main():
    """Restore CKR to main HEAD, discarding any changes."""
    subprocess.run(
        ["git", "checkout", "."], cwd=CKR_PATH, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "clean", "-fd"], cwd=CKR_PATH, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=CKR_PATH, check=True, capture_output=True
    )
    print("[CKR restored to main]", file=sys.stderr)


@app.command()
def run(
    last_message_id: int = typer.Argument(
        ...,
        help="Last user message ID to include (response will be generated after this)",
    ),
    model: str = typer.Option("opus", help="Model: opus, sonnet, haiku, or full ID"),
    thinking: str = typer.Option("adaptive", help="Thinking mode: adaptive, none"),
    max_tokens: int = typer.Option(16000, help="Max output tokens"),
    system_suffix: str = typer.Option("", help="Extra text appended to system prompt"),
    append_message: str = typer.Option(
        "", help="Append a synthetic user message after last_message_id"
    ),
    jsonl_history: bool = typer.Option(
        False,
        "--jsonl-history",
        help="Inject history as JSONL document in system prompt; only append_message is a native API turn",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print system prompt and message params, then exit without calling the API",
    ),
    verbose: bool = typer.Option(False, "-v", help="Show full JSON response"),
):
    asyncio.run(
        main(
            last_message_id=last_message_id,
            model=model,
            thinking=thinking,
            max_tokens=max_tokens,
            system_suffix=system_suffix,
            append_message=append_message,
            jsonl_history=jsonl_history,
            dry_run=dry_run,
            verbose=verbose,
        )
    )


async def main(
    last_message_id: int,
    model: str,
    thinking: str,
    max_tokens: int,
    system_suffix: str,
    append_message: str,
    jsonl_history: bool,
    dry_run: bool,
    verbose: bool,
):
    load_env()
    setup_logging()

    chat_id = ID_USER_MAP[USER_ANTON]
    # Use last message's timestamp so context looks realistic, not current time
    msg_time = _get_message_time(chat_id, last_message_id)
    now = msg_time

    # Find and checkout the CKR state at the time of the target message
    ckr_commit = _get_ckr_commit_before(now)
    _ckr_checkout(ckr_commit)

    try:
        await _run(
            last_message_id,
            model,
            thinking,
            max_tokens,
            system_suffix,
            append_message,
            jsonl_history,
            dry_run,
            verbose,
            chat_id,
            now,
        )
    finally:
        _ckr_restore_main()


def _get_message_time(chat_id: int, message_id: int) -> datetime.datetime:
    """Look up the created_at timestamp of a message."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT created_at FROM messages WHERE id = %s", (message_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Message {message_id} not found")
            return row[0]


async def _run(
    last_message_id: int,
    model: str,
    thinking: str,
    max_tokens: int,
    system_suffix: str,
    append_message: str,
    jsonl_history: bool,
    dry_run: bool,
    verbose: bool,
    chat_id: int,
    now: datetime.datetime,
):
    agent_config = DEFAULT_AGENT_CONFIG
    rendering_config = agent_config.rendering

    with connect() as conn:
        # Never commit — rollback on exit so no tool side-effects persist
        conn.autocommit = False
        with conn.cursor() as curr:
            # Load all messages, then truncate to last_message_id
            db_messages = get_messages(
                curr,
                chat_id=chat_id,
                limit=rendering_config.max_history_length_turns,
            )
            db_messages = [m for m in db_messages if m.message_id <= last_message_id]
            if not db_messages:
                print(f"No messages found up to id={last_message_id}", file=sys.stderr)
                raise typer.Exit(1)

            if append_message:
                db_messages.append(
                    DbMessage(
                        created_at=now,
                        chat_id=chat_id,
                        user_id=chat_id,
                        message=append_message,
                    )
                )
                print(
                    f"[appended synthetic message: '{append_message[:80]}']",
                    file=sys.stderr,
                )

            print(
                f"[{len(db_messages)} messages, last: id={db_messages[-1].message_id} "
                f"'{db_messages[-1].message[:80]}']",
                file=sys.stderr,
            )

            scheduled_invocations = get_schedules(curr, chat_id)

            invocation = Invocation(invocation_type="reply")
            system, message_params = build_claude_input(
                db_messages,
                rendering_config,
                invocation=invocation,
                scheduled_invocations=scheduled_invocations,
                forced_now_date=now,
            )

            # Resolve named prompts from registry
            if system_suffix in PROMPTS:
                print(f"[resolved prompt: {system_suffix}]", file=sys.stderr)
                system_suffix = PROMPTS[system_suffix]

            if jsonl_history:
                if not append_message:
                    print("--jsonl-history requires --append-message", file=sys.stderr)
                    raise typer.Exit(1)
                cleaned = _clean_message_params_for_jsonl(message_params)
                ctx_entries = [m for m in cleaned if _is_context_message(m)]
                history_entries = [m for m in cleaned if not _is_context_message(m)]
                history_jsonl = "\n".join(
                    json.dumps(mp, default=str) for mp in history_entries
                )
                system += f"\n\n<conversation_history>\n{history_jsonl}\n</conversation_history>"
                # Native API: context + crafted prompt
                if not system_suffix:
                    print(
                        "--jsonl-history: use --system-suffix for the native prompt",
                        file=sys.stderr,
                    )
                    raise typer.Exit(1)
                message_params = ctx_entries + [
                    {"role": "user", "content": system_suffix}
                ]
                system_suffix = (
                    ""  # already used as native message, don't append to system
                )
                print(
                    "[history as JSONL; native turns: context + prompt]",
                    file=sys.stderr,
                )

            if system_suffix:
                system += f"\n\n{system_suffix}"
                print(
                    f"[system suffix appended: {system_suffix[:80]}]", file=sys.stderr
                )

            # Override sampling params (keep original agent_config for tool building)
            sampling_config = SamplingConfig(
                model=model,
                tool_subset="all",
                output_mode="tool_message",
                max_tokens=max_tokens,
                thinking=thinking,
            )
            model_name = sampling_config.resolve_model_name()
            print(
                f"[model={model_name} thinking={thinking} max_tokens={max_tokens}]",
                file=sys.stderr,
            )

            if dry_run:
                print("=== SYSTEM ===")
                print(system)
                print("\n=== MESSAGES ===")
                for mp in message_params:
                    print(json.dumps(mp, indent=2, default=str))
                return

            scope = InterruptionScope(chat_id=chat_id, message_id=None)

            from telegram import Bot

            bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])

            # Build real tools, then swap send_message with a no-op that keeps the original spec
            all_local_tool_objects = get_tools_for_agent_config(
                agent_config, curr, chat_id, bot
            )
            tool_map = {}
            for t in all_local_tool_objects:
                if t.name == "send_message":
                    noop = NoOpSendMessageTool(t.spec())
                    tool_map[t.name] = noop
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
                    on_update=NoOpHooks().on_update,
                    scope=scope,
                    model_name=model_name,
                    job_queue=_DummyJobQueue(),
                    max_tokens=max_tokens,
                    thinking=thinking,
                )

            if not result_params:
                print("(no response)")
                return

            if verbose:
                print(json.dumps(result_params, indent=2, default=str))
            else:
                print("\n\n".join(render_claude_response_verbose(result_params)))

            if claude_calls:
                total_prompt = sum(c.num_prompt_tokens for c in claude_calls)
                total_cached = sum(c.num_cached_tokens for c in claude_calls)
                total_output = sum(c.num_output_tokens for c in claude_calls)
                cost = tool_sampler.estimate_cost(claude_calls, model_name)
                cost_str = f", ${cost:.4f}" if cost is not None else ""
                print(
                    f"\n[{len(claude_calls)} call(s), {total_prompt} prompt, {total_cached} cached, {total_output} output{cost_str}]",
                    file=sys.stderr,
                )

        # Rollback any DB writes from tools
        conn.rollback()


if __name__ == "__main__":
    app()
