"""Agent POV: full context window view and token counting."""

from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify

from dashboard.helpers import (
    extract_api_msg_db_ids,
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
from yarvis_ptb.prompting import build_claude_input
from yarvis_ptb.settings.main import HISTORY_LENGTH_LONG_TURNS
from yarvis_ptb.storage import get_messages, get_schedules

bp = Blueprint("agent_view", __name__)

DEFAULT_CHAT_ID = None  # Set lazily to avoid circular import


def _get_default_chat_id():
    from yarvis_ptb.settings import ROOT_USER_ID

    return ROOT_USER_ID


def _load_agent_context():
    """Shared logic: load messages, build system prompt + history."""
    chat_id = _get_default_chat_id()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            messages = get_messages(cur, chat_id, limit=HISTORY_LENGTH_LONG_TURNS)
            scheduled_invocations = get_schedules(cur)

        system_prompt, history = build_claude_input(
            messages,
            DEFAULT_AGENT_CONFIG.rendering,
            scheduled_invocations=scheduled_invocations,
        )
        truncate_base64_images(history)
        return messages, system_prompt, history
    finally:
        conn.close()


@bp.route("/api/agent-view")
def api_agent_view():
    """Return the full agent view: system prompt + message history as Claude sees it."""
    messages, system_prompt, history = _load_agent_context()

    tool_specs = get_tool_specs_for_agent_config(DEFAULT_AGENT_CONFIG)
    tool_names = [t["name"] for t in tool_specs]

    return jsonify(
        {
            "system_prompt": system_prompt,
            "history": history,
            "turn_usages": extract_turn_usages(messages),
            "db_ids": extract_api_msg_db_ids(messages),
            "num_messages": len(history),
            "num_db_turns": len(messages),
            "tools": tool_names,
        }
    )


@bp.route("/api/agent-view/tokens")
def api_agent_view_tokens():
    """Count tokens for the full agent input, with per-message breakdown."""
    messages, system_prompt, history = _load_agent_context()
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
