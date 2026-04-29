"""Shared helpers for dashboard routes."""

import os

import psycopg2

from yarvis_ptb.prompting import convert_db_messages_to_claude_messages
from yarvis_ptb.settings import DEFAULT_TIMEZONE, USER_ID_MAP
from yarvis_ptb.storage import DbMessage
from yarvis_ptb.tool_sampler import get_tools_for_agent_config

DATABASE_URL = os.environ.get("DATABASE_URL")
BOT_USER_ID = -1
SYSTEM_USER_ID = -2
PER_PAGE = 500


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def get_sender_name(user_id: int) -> str:
    if user_id == BOT_USER_ID:
        return "Bot"
    if user_id == SYSTEM_USER_ID:
        return "System"
    return USER_ID_MAP.get(user_id, f"User {user_id}")


def get_tool_specs_for_agent_config(agent_config):
    """Build Claude tool spec dicts from agent config (for dashboard display/token counting)."""
    tools = get_tools_for_agent_config(agent_config, curr=None, chat_id=0, bot=None)
    return [t.spec().to_claude_tool() for t in tools]


def turn_to_api_messages(row: dict) -> list[dict]:
    """Convert a single DB row to Claude API MessageParam list using the real codepath."""
    db_msg = DbMessage(
        created_at=row["created_at"].astimezone(DEFAULT_TIMEZONE),
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        message=row["message"],
        meta=row["meta"],
        message_id=row["id"],
        marked_for_archive=row["marked_for_archive"],
    )
    api_msgs, _ = convert_db_messages_to_claude_messages([db_msg])
    truncate_base64_images(api_msgs)
    return api_msgs


def truncate_base64_images(api_messages: list[dict]) -> None:
    """Truncate base64 image data in-place for display."""
    for msg in api_messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        source["data"] = "[truncated]"


def extract_api_msg_db_ids(db_messages: list[DbMessage]) -> list[int | None]:
    """Map each API message index to its source DB message ID."""
    db_ids: list[int | None] = []
    for msg in db_messages:
        if msg.user_id == BOT_USER_ID:
            if msg.meta and "message_params" in msg.meta:
                params = msg.meta["message_params"]
                n = len(params)
                if (
                    n > 0
                    and not params[-1].get("content")
                    and params[-1].get("role") == "assistant"
                ):
                    n -= 1
                for _ in range(n):
                    db_ids.append(msg.message_id)
            else:
                db_ids.append(msg.message_id)
        else:
            db_ids.append(msg.message_id)
    return db_ids


def extract_api_msg_db_times(db_messages: list[DbMessage]) -> list[str | None]:
    """Map each API message index to its source DB message timestamp (ISO format)."""
    db_times: list[str | None] = []
    for msg in db_messages:
        ts = msg.created_at.isoformat()
        if msg.user_id == BOT_USER_ID:
            if msg.meta and "message_params" in msg.meta:
                params = msg.meta["message_params"]
                n = len(params)
                if (
                    n > 0
                    and not params[-1].get("content")
                    and params[-1].get("role") == "assistant"
                ):
                    n -= 1
                for _ in range(n):
                    db_times.append(ts)
            else:
                db_times.append(ts)
        else:
            db_times.append(ts)
    return db_times


def compute_full_turns_diff(
    filtered_history: list[dict],
    filtered_annotations: list,
    full_history: list[dict],
    full_annotations: list,
) -> dict[str, list[dict]]:
    """Group both renderings by db_msg_id; return {db_id_str: full_msgs} for ids that differ."""
    from collections import defaultdict

    f: dict[int, list] = defaultdict(list)
    for ann, msg in zip(filtered_annotations, filtered_history):
        if ann.db_msg_id is not None:
            f[ann.db_msg_id].append(msg)
    g: dict[int, list] = defaultdict(list)
    for ann, msg in zip(full_annotations, full_history):
        if ann.db_msg_id is not None:
            g[ann.db_msg_id].append(msg)
    out: dict[str, list[dict]] = {}
    for db_id, full_msgs in g.items():
        if f.get(db_id, []) != full_msgs:
            out[str(db_id)] = full_msgs
    return out


def extract_turn_usages(db_messages: list[DbMessage]) -> list[dict]:
    """Extract usage data from bot DB messages with their API message index ranges."""
    usages = []
    api_idx = 0
    for msg in db_messages:
        start = api_idx
        if msg.user_id == BOT_USER_ID:
            if msg.meta and "message_params" in msg.meta:
                params = msg.meta["message_params"]
                n = len(params)
                if (
                    n > 0
                    and not params[-1].get("content")
                    and params[-1].get("role") == "assistant"
                ):
                    n -= 1
                api_idx += n
            else:
                api_idx += 1
        else:
            api_idx += 1
        end = api_idx
        usage = (msg.meta or {}).get("usage")
        if usage and start < end:
            usages.append({"api_start": start, "api_end": end, "usage": usage})
    return usages
