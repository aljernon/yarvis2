"""Agent POV: full context window view and token counting."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2.extras
from flask import Blueprint, jsonify, request

from dashboard.helpers import (
    extract_turn_usages,
    get_db,
    get_tool_specs_for_agent_config,
    truncate_base64_images,
)
from dashboard.token_counting import (
    count_tokens_cached,
    has_tool_use,
    msg_to_text,
    strip_thinking_blocks,
)
from yarvis_ptb.complex_chat import DEFAULT_AGENT_CONFIG
from yarvis_ptb.prompting import (
    build_claude_input,
    convert_db_messages_to_claude_messages,
)
from yarvis_ptb.settings.main import HISTORY_LENGTH_LONG_TURNS, ROOT_AGENT_SLUG
from yarvis_ptb.storage import DbMessage, get_messages, get_schedules

bp = Blueprint("agent_view", __name__)

DEFAULT_CHAT_ID = None  # Set lazily to avoid circular import


def _get_default_chat_id():
    from yarvis_ptb.settings import ROOT_USER_ID

    return ROOT_USER_ID


def _load_agent_context(*, skip_forget_above: bool = False):
    """Shared logic: load messages, build system prompt + history."""
    chat_id = _get_default_chat_id()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            messages = get_messages(cur, chat_id, limit=HISTORY_LENGTH_LONG_TURNS)
            scheduled_invocations = get_schedules(cur)

        system_prompt, history, annotations = build_claude_input(
            messages,
            DEFAULT_AGENT_CONFIG.rendering,
            scheduled_invocations=scheduled_invocations,
            agent_slug=ROOT_AGENT_SLUG,
            skip_forget_above=skip_forget_above,
        )
        truncate_base64_images(history)
        return messages, system_prompt, history, annotations
    finally:
        conn.close()


def _load_subagent_groups(chat_id: int, min_time, max_time) -> list[dict]:
    """Fetch subagent messages between min_time and max_time, grouped by agent."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT m.id, m.created_at, m.chat_id, m.user_id, m.message, m.meta,
                       m.marked_for_archive, m.agent_id,
                       a.slug as agent_slug
                FROM messages m
                JOIN agents a ON m.agent_id = a.id
                WHERE m.chat_id = %s
                  AND m.agent_id IS NOT NULL
                  AND m.is_visible = true
                  AND m.created_at >= %s
                  AND m.created_at <= %s
                ORDER BY m.agent_id, m.created_at ASC, m.id ASC
                """,
                (chat_id, min_time, max_time),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    from yarvis_ptb.settings import DEFAULT_TIMEZONE

    # Group by agent_id
    agents: dict[int, dict] = {}
    agent_messages: dict[int, list[DbMessage]] = defaultdict(list)
    for row in rows:
        aid = row["agent_id"]
        if aid not in agents:
            agents[aid] = {"agent_id": aid, "agent_slug": row["agent_slug"]}
        agent_messages[aid].append(
            DbMessage(
                created_at=row["created_at"].astimezone(DEFAULT_TIMEZONE),
                chat_id=row["chat_id"],
                user_id=row["user_id"],
                message=row["message"],
                meta=row["meta"],
                message_id=row["id"],
                marked_for_archive=row["marked_for_archive"],
                agent_id=row["agent_id"],
            )
        )

    groups = []
    for aid, info in agents.items():
        db_msgs = agent_messages[aid]
        api_msgs, _ = convert_db_messages_to_claude_messages(db_msgs)
        truncate_base64_images(api_msgs)
        first_time = db_msgs[0].created_at.isoformat()
        last_time = db_msgs[-1].created_at.isoformat()
        groups.append(
            {
                **info,
                "first_time": first_time,
                "last_time": last_time,
                "num_messages": len(api_msgs),
                "num_db_turns": len(db_msgs),
                "history": api_msgs,
                "turn_usages": extract_turn_usages(db_msgs),
            }
        )
    return groups


@bp.route("/api/agent-view")
def api_agent_view():
    """Return the full agent view: system prompt + message history as Claude sees it."""
    skip_forget = request.args.get("full") == "1"
    messages, system_prompt, history, annotations = _load_agent_context(
        skip_forget_above=skip_forget
    )

    tool_specs = get_tool_specs_for_agent_config(DEFAULT_AGENT_CONFIG)
    tool_names = [t["name"] for t in tool_specs]

    # Derive db_ids and db_times from annotations (aligned with history)
    db_ids = [a.db_msg_id for a in annotations]
    # Build db_msg_id→timestamp map for time lookup
    id_to_time: dict[int | None, str] = {}
    for msg in messages:
        if msg.message_id is not None:
            id_to_time[msg.message_id] = msg.created_at.isoformat()
    db_times = [id_to_time.get(a.db_msg_id) for a in annotations]

    # Fetch subagent messages in the same time range
    subagent_groups = []
    if messages:
        chat_id = _get_default_chat_id()
        min_time = messages[0].created_at
        max_time = messages[-1].created_at
        subagent_groups = _load_subagent_groups(chat_id, min_time, max_time)

    return jsonify(
        {
            "system_prompt": system_prompt,
            "history": history,
            "turn_usages": extract_turn_usages(messages),
            "db_ids": db_ids,
            "db_times": db_times,
            "num_messages": len(history),
            "num_db_turns": len(messages),
            "tools": tool_names,
            "subagent_groups": subagent_groups,
        }
    )


@bp.route("/api/agent-view/tokens")
def api_agent_view_tokens():
    """Count tokens for the full agent input, with per-message breakdown."""
    messages, system_prompt, history, _annotations = _load_agent_context()
    tool_specs = get_tool_specs_for_agent_config(DEFAULT_AGENT_CONFIG)

    def is_countable_boundary(i: int) -> bool:
        msg = history[i]
        if msg["role"] == "user":
            return True
        return not has_tool_use(msg)

    # System prompt tokens (without tools)
    system_tokens_no_tools = count_tokens_cached(
        system=system_prompt,
        messages=[{"role": "user", "content": "x"}],
    )
    # System prompt tokens (with tools)
    system_tokens = count_tokens_cached(
        system=system_prompt,
        messages=[{"role": "user", "content": "x"}],
        tools=tool_specs,
    )

    # Phase 1: parallel approximate counts for tool_use messages
    approx_indices = [i for i in range(len(history)) if has_tool_use(history[i])]
    results = [None] * len(history)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {}
        for i in approx_indices:
            text = msg_to_text(history[i])
            fut = pool.submit(
                count_tokens_cached, messages=[{"role": "user", "content": text}]
            )
            futures[fut] = i
        for fut in as_completed(futures):
            i = futures[fut]
            results[i] = {
                "role": "assistant",
                "tokens": fut.result(),
                "approx": True,
            }

    # Phase 2: sequential incremental counts at boundaries
    prev_total = system_tokens
    for i in range(len(history)):
        if results[i] is not None:
            continue
        if not is_countable_boundary(i):
            continue

        conversation = strip_thinking_blocks(history[: i + 1])
        total = count_tokens_cached(
            system=system_prompt, messages=conversation, tools=tool_specs
        )
        segment_tokens = total - prev_total
        is_pair = i > 0 and has_tool_use(history[i - 1])
        results[i] = {
            "role": history[i]["role"],
            "tokens": segment_tokens,
            "pair": is_pair,
        }
        prev_total = total

    total_tokens = prev_total or system_tokens

    for i in range(len(results)):
        if results[i] is None:
            results[i] = {"role": history[i]["role"], "tokens": None}

    return jsonify(
        {
            "total_tokens": total_tokens,
            "system_tokens": system_tokens_no_tools,
            "tool_tokens": system_tokens - system_tokens_no_tools,
            "num_tools": len(tool_specs),
            "messages": results,
        }
    )
