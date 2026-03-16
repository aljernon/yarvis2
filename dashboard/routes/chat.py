"""Ephemeral agent chat: send a message, get response, no DB save."""

import asyncio
import datetime
import os
from contextlib import AsyncExitStack

from flask import Blueprint, jsonify, request

from dashboard.helpers import get_db
from yarvis_ptb import tool_sampler
from yarvis_ptb.agent_config import AgentMeta
from yarvis_ptb.complex_chat import DEFAULT_AGENT_CONFIG
from yarvis_ptb.prompting import (
    build_claude_input,
    render_claude_response_short,
)
from yarvis_ptb.ptb_util import InterruptionScope
from yarvis_ptb.sampling import NoOpHooks
from yarvis_ptb.settings import DEFAULT_TIMEZONE, ROOT_AGENT_USER_ID, ROOT_USER_ID
from yarvis_ptb.settings.main import SUBAGENT_MODEL_MAP
from yarvis_ptb.storage import DbMessage, Invocation, get_messages, get_schedules
from yarvis_ptb.tool_sampler import _DummyJobQueue, get_tools_for_agent_config
from yarvis_ptb.tools.tool_spec import LocalTool, ToolResult
from yarvis_ptb.turns import UserTurn

bp = Blueprint("chat", __name__)


class _CliSendMessageTool(LocalTool):
    """Intercepts send_message: captures text instead of sending to Telegram."""

    def __init__(self):
        self._original_spec = None

    def set_original_spec(self, spec):
        self._original_spec = spec

    def spec(self):
        return self._original_spec

    async def _execute(self, **kwargs) -> ToolResult:
        return ToolResult.success(
            "Message sent successfully.",
            stop_after=bool(kwargs.get("final", False)),
        )


async def _run_ephemeral_chat(
    prompt: str, agent_id: int | None = None, model: str | None = None
) -> dict:
    """Run a single Claude exchange ephemerally (no DB save), like cli_prompt.py."""
    chat_id = ROOT_USER_ID
    now = datetime.datetime.now(DEFAULT_TIMEZONE)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Resolve agent config
            if agent_id is not None:
                import psycopg2.extras

                with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor
                ) as dict_cur:
                    dict_cur.execute(
                        "SELECT meta FROM agents WHERE id = %s", (agent_id,)
                    )
                    row = dict_cur.fetchone()
                if not row:
                    return {"error": f"Agent #{agent_id} not found"}
                agent_meta = AgentMeta.model_validate(row["meta"] or {})
                agent_config = agent_meta.agent_config
            else:
                agent_config = DEFAULT_AGENT_CONFIG

            rendering_config = agent_config.rendering
            sampling_config = agent_config.sampling

            db_messages = get_messages(
                cur,
                chat_id=chat_id,
                limit=rendering_config.max_history_length_turns,
                agent_id=agent_id,
            )
            msg_user_id = ROOT_AGENT_USER_ID if agent_id is not None else chat_id
            initial_db_message = DbMessage(
                created_at=now,
                chat_id=chat_id,
                user_id=msg_user_id,
                message=prompt,
            )
            db_messages = [*db_messages, initial_db_message][
                -rendering_config.max_history_length_turns :
            ]

            scheduled_invocations = get_schedules(cur, chat_id)

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

            all_local_tool_objects = get_tools_for_agent_config(
                agent_config, cur, chat_id, bot
            )
            tool_map: dict[str, LocalTool] = {}
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

            async with AsyncExitStack() as stack:
                for t in tool_map.values():
                    await stack.enter_async_context(t.context())

                if model:
                    model_name = SUBAGENT_MODEL_MAP.get(model, model)
                else:
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

            response_text = (
                render_claude_response_short(result_params) if result_params else ""
            )

            # Render the user turn with <system> prefix
            user_turn = UserTurn(
                created_at=now,
                marked_for_archive=False,
                user_id=msg_user_id,
                message=prompt,
            )
            user_turn_params = user_turn.render()

            usage = {}
            if claude_calls:
                total_prompt = sum(c.num_prompt_tokens for c in claude_calls)
                total_cached = sum(c.num_cached_tokens for c in claude_calls)
                total_output = sum(c.num_output_tokens for c in claude_calls)
                cost = tool_sampler.estimate_cost(claude_calls, model_name)
                usage = {
                    "num_calls": len(claude_calls),
                    "prompt_tokens": total_prompt,
                    "cached_tokens": total_cached,
                    "output_tokens": total_output,
                    "cost_usd": cost,
                    "model": model_name,
                }

            return {
                "response_text": response_text,
                "message_params": user_turn_params + (result_params or []),
                "usage": usage,
            }
    finally:
        conn.close()


@bp.route("/api/agent-chat", methods=["POST"])
def api_agent_chat():
    """Ephemeral chat: send a message to an agent, get response, no DB save."""
    data = request.get_json()
    if not data or not data.get("message", "").strip():
        return jsonify({"error": "message is required"}), 400

    prompt = data["message"].strip()
    agent_id = data.get("agent_id")
    model = data.get("model")

    try:
        result = asyncio.run(_run_ephemeral_chat(prompt, agent_id, model))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
