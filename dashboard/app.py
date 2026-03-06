import hashlib
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

# Load .env from project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

# Add yarvis_ptb to path so we can import settings
sys.path.insert(0, os.path.join(PROJECT_ROOT, "yarvis_ptb"))

DATABASE_URL = os.environ.get("DATABASE_URL")
BOT_USER_ID = -1
SYSTEM_USER_ID = -2
TOOL_CALL_USER_ID = -3

os.environ.setdefault("SETTINGS_NAME", "anton")
from yarvis_ptb.agent_config import AgentConfig
from yarvis_ptb.complex_chat import DEFAULT_AGENT_CONFIG
from yarvis_ptb.prompting import (
    build_claude_input,
    convert_db_messages_to_claude_messages,
)
from yarvis_ptb.settings import DEFAULT_TIMEZONE, ROOT_USER_ID, USER_ID_MAP
from yarvis_ptb.settings.main import HISTORY_LENGTH_LONG_TURNS
from yarvis_ptb.storage import (
    DbMessage,
    get_messages,
    get_schedules,
)
from yarvis_ptb.tool_sampler import get_tools_for_agent_config


def get_tool_specs_for_agent_config(agent_config):
    """Build Claude tool spec dicts from agent config (for dashboard display/token counting)."""
    # Dashboard doesn't have a real cursor/bot, but we need tool specs.
    # Pass None for curr/bot — tools that need them will fail at execution, not spec time.
    tools = get_tools_for_agent_config(agent_config, curr=None, chat_id=0, bot=None)
    return [t.spec().to_claude_tool() for t in tools]


DEFAULT_CHAT_ID = ROOT_USER_ID
PER_PAGE = 500


def strip_thinking_blocks(messages: list[dict]) -> list[dict]:
    """Remove thinking/redacted_thinking blocks and empty text blocks from messages for token counting."""
    cleaned = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            filtered = [
                b
                for b in content
                if not (
                    isinstance(b, dict)
                    and (
                        b.get("type") in ("thinking", "redacted_thinking")
                        or (b.get("type") == "text" and not b.get("text", "").strip())
                    )
                )
            ]
            if filtered:
                cleaned.append({**msg, "content": filtered})
            # Skip messages that become empty after stripping
        elif isinstance(content, str) and not content.strip():
            # Skip empty string content
            continue
        else:
            cleaned.append(msg)
    return cleaned


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
    api_msgs = convert_db_messages_to_claude_messages([db_msg])
    # Truncate base64 image data for display
    for msg in api_msgs:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        source["data"] = "[truncated]"
    return api_msgs


app = Flask(__name__)


anthropic_client = anthropic.Anthropic()
TOKEN_COUNT_MODEL = "claude-sonnet-4-20250514"

# Scrappy on-disk token cache
TOKEN_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".token_cache"
)
os.makedirs(TOKEN_CACHE_DIR, exist_ok=True)


def _cache_key(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def _cache_get(key: str) -> int | None:
    path = os.path.join(TOKEN_CACHE_DIR, key)
    if os.path.exists(path):
        return int(open(path).read())
    return None


def _cache_set(key: str, tokens: int):
    path = os.path.join(TOKEN_CACHE_DIR, key)
    with open(path, "w") as f:
        f.write(str(tokens))


def count_tokens_cached(
    *, system: str | None = None, messages: list[dict], tools: list[dict] | None = None
) -> int:
    """Count tokens with on-disk caching keyed by content hash."""
    cache_data = json.dumps(
        {"system": system, "messages": messages, "tools": tools},
        sort_keys=True,
        default=str,
    )
    key = _cache_key(cache_data)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    kwargs = {"model": TOKEN_COUNT_MODEL, "messages": messages}
    if system is not None:
        kwargs["system"] = system
    if tools is not None:
        kwargs["tools"] = tools
    resp = anthropic_client.messages.count_tokens(**kwargs)
    _cache_set(key, resp.input_tokens)
    return resp.input_tokens


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def get_sender_name(user_id: int) -> str:
    if user_id == BOT_USER_ID:
        return "Bot"
    if user_id == SYSTEM_USER_ID:
        return "System"
    if user_id == TOOL_CALL_USER_ID:
        return "Tool Call"
    return USER_ID_MAP.get(user_id, f"User {user_id}")


# ── HTML Routes ──────────────────────────────────────────────────────────────


@app.route("/")
@app.route("/messages")
def messages_page():
    return render_template("messages.html")


@app.route("/schedules")
def schedules_page():
    return render_template("schedules.html")


@app.route("/agent")
def agent_page():
    return render_template("agent.html")


@app.route("/agents")
def agents_page():
    return render_template("agents.html")


# ── API Routes ───────────────────────────────────────────────────────────────


@app.route("/api/chats")
def api_chats():
    """Return list of chats with message counts, plus the default chat_id."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT chat_id, COUNT(*) as msg_count
                FROM messages
                GROUP BY chat_id
                ORDER BY msg_count DESC
            """)
            rows = cur.fetchall()

        chats = []
        for row in rows:
            cid = row["chat_id"]
            label = USER_ID_MAP.get(cid, str(cid))
            chats.append(
                {
                    "chat_id": cid,
                    "label": label,
                    "msg_count": row["msg_count"],
                }
            )

        return jsonify(
            {
                "chats": chats,
                "default_chat_id": DEFAULT_CHAT_ID,
            }
        )
    finally:
        conn.close()


@app.route("/api/messages")
def api_messages():
    page = request.args.get("page", 1, type=int)
    chat_id = request.args.get("chat_id", DEFAULT_CHAT_ID, type=int)
    search = request.args.get("search", "", type=str).strip()
    min_bytes = request.args.get("min_bytes", 0, type=int)
    offset = (page - 1) * PER_PAGE

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where_clauses = ["m.chat_id = %s"]
            params: list = [chat_id]

            if search:
                where_clauses.append("m.message ILIKE %s")
                params.append(f"%{search}%")
            if min_bytes > 0:
                where_clauses.append(
                    "octet_length(m.meta::text) + octet_length(m.message) >= %s"
                )
                params.append(min_bytes)

            where_sql = "WHERE " + " AND ".join(where_clauses)

            # Count
            cur.execute(f"SELECT COUNT(*) as cnt FROM messages m {where_sql}", params)
            total = cur.fetchone()["cnt"]

            # Fetch page
            cur.execute(
                f"""
                SELECT m.id, m.created_at, m.chat_id, m.user_id, m.message, m.meta, m.marked_for_archive,
                       octet_length(m.meta::text) + octet_length(m.message) as total_bytes,
                       m.agent_id, a.slug as agent_slug
                FROM messages m
                LEFT JOIN agents a ON m.agent_id = a.id
                {where_sql}
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT %s OFFSET %s
                """,
                params + [PER_PAGE, offset],
            )
            rows = cur.fetchall()

        messages = []
        for row in rows:
            meta = row["meta"] or {}
            api_msgs = turn_to_api_messages(row)
            messages.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"].isoformat(),
                    "chat_id": row["chat_id"],
                    "user_id": row["user_id"],
                    "sender": get_sender_name(row["user_id"]),
                    "message": row["message"],
                    "meta": meta,
                    "api_messages": api_msgs,
                    "marked_for_archive": row["marked_for_archive"],
                    "has_message_params": "message_params" in meta,
                    "has_image": "image_b64" in meta,
                    "total_bytes": row["total_bytes"],
                    "agent_id": row["agent_id"],
                    "agent_slug": row["agent_slug"],
                }
            )

        return jsonify(
            {
                "messages": messages,
                "total": total,
                "page": page,
                "per_page": PER_PAGE,
                "total_pages": math.ceil(total / PER_PAGE) if total else 1,
                "chat_id": chat_id,
            }
        )
    finally:
        conn.close()


@app.route("/api/turn/<int:turn_id>/tokens")
def api_turn_tokens(turn_id: int):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, created_at, chat_id, user_id, message, meta, marked_for_archive FROM messages WHERE id = %s",
                (turn_id,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404

        api_msgs = turn_to_api_messages(row)
        if not api_msgs:
            return jsonify({"id": turn_id, "messages": [], "total_tokens": 0})

        def has_tool_use(msg: dict) -> bool:
            content = msg.get("content", [])
            if isinstance(content, str):
                return False
            return any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content
            )

        def is_countable_boundary(i: int) -> bool:
            msg = api_msgs[i]
            if msg["role"] == "user":
                return True
            return not has_tool_use(msg)

        def msg_to_text(msg: dict) -> str:
            content = msg.get("content", [])
            if isinstance(content, str):
                return content
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(json.dumps(block.get("input", {})))
                elif block.get("type") == "tool_result":
                    rc = block.get("content", "")
                    if isinstance(rc, list):
                        parts.append(
                            " ".join(
                                b.get("text", "") for b in rc if isinstance(b, dict)
                            )
                        )
                    else:
                        parts.append(str(rc))
                elif block.get("type") in ("thinking", "redacted_thinking"):
                    pass
            return "\n".join(parts)

        def has_text_blocks(msg: dict) -> bool:
            content = msg.get("content", [])
            if not isinstance(content, list):
                return False
            return any(isinstance(b, dict) and b.get("type") == "text" for b in content)

        def count_per_block(msg: dict) -> list[dict] | None:
            """For assistant messages with mixed content (text + tool_use),
            return per-block token info. Only text blocks get counted;
            tool_use blocks are skipped (covered by the result's call+result count)."""
            content = msg.get("content", [])
            if not isinstance(content, list):
                return None
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    tokens = count_tokens_cached(
                        messages=[{"role": "user", "content": text}]
                    )
                    blocks.append({"tokens": tokens, "approx": False})
                elif btype == "tool_use":
                    # Skip — the paired tool_result's call+result count covers this
                    blocks.append(None)
                elif btype in ("thinking", "redacted_thinking"):
                    blocks.append(None)
            return blocks if blocks else None

        # Counting strategies:
        # - For tool_use-only (assistant, no text): skip — covered by result's call+result
        # - For mixed (assistant, text + tool_use): exact count for text blocks only
        # - For tool_result (user) and other boundaries: exact incremental count
        results = [None] * len(api_msgs)
        prev_total = 0

        for i in range(len(api_msgs)):
            if has_tool_use(api_msgs[i]):
                if has_text_blocks(api_msgs[i]):
                    # Mixed message: count text blocks exactly, skip tool_use
                    blocks = count_per_block(api_msgs[i])
                    results[i] = {
                        "role": "assistant",
                        "tokens": None,
                        "blocks": blocks,
                    }
                else:
                    # Tool_use only — skip, covered by result's call+result
                    results[i] = {"role": "assistant", "tokens": None}
                continue

            if not is_countable_boundary(i):
                continue

            conversation = strip_thinking_blocks(api_msgs[: i + 1])
            total = count_tokens_cached(messages=conversation)
            segment_tokens = total - prev_total
            is_pair = i > 0 and has_tool_use(api_msgs[i - 1])
            results[i] = {
                "role": api_msgs[i]["role"],
                "tokens": segment_tokens,
                "pair": is_pair,
            }
            prev_total = total

        for i in range(len(results)):
            if results[i] is None:
                results[i] = {"role": api_msgs[i]["role"], "tokens": None}

        return jsonify({"id": turn_id, "messages": results, "total_tokens": prev_total})
    finally:
        conn.close()


@app.route("/api/schedules")
def api_schedules():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, created_at, next_run_at, chat_id, is_active,
                       title, context, schedule_type, schedule_spec, meta
                FROM schedules
                ORDER BY is_active DESC, next_run_at ASC
            """)
            rows = cur.fetchall()

        schedules = []
        for row in rows:
            schedules.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"].isoformat()
                    if row["created_at"]
                    else None,
                    "next_run_at": row["next_run_at"].isoformat()
                    if row["next_run_at"]
                    else None,
                    "chat_id": row["chat_id"],
                    "is_active": row["is_active"],
                    "schedule_type": row["schedule_type"],
                    "schedule_spec": row["schedule_spec"],
                    "title": row["title"],
                    "context": row["context"],
                    "meta": row["meta"] or {},
                }
            )

        return jsonify({"schedules": schedules})
    finally:
        conn.close()


@app.route("/api/agents")
def api_agents():
    """Return all agents with their fields and message counts."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT a.id, a.chat_id, a.created_at, a.meta, a.slug,
                       COUNT(m.id) as msg_count
                FROM agents a
                LEFT JOIN messages m ON m.agent_id = a.id
                GROUP BY a.id
                ORDER BY a.created_at DESC
            """)
            rows = cur.fetchall()

        agents = []
        for row in rows:
            meta = row["meta"] or {}
            agents.append(
                {
                    "id": row["id"],
                    "slug": row["slug"],
                    "chat_id": row["chat_id"],
                    "created_at": row["created_at"].isoformat()
                    if row["created_at"]
                    else None,
                    "meta": meta,
                    "msg_count": row["msg_count"],
                    "type": meta.get("type", ""),
                    "agent_config": meta.get("agent_config", {}),
                }
            )

        return jsonify({"agents": agents})
    finally:
        conn.close()


@app.route("/api/subagent/<int:agent_id>")
def api_subagent(agent_id: int):
    """Return a subagent's config and message history."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as dict_cur:
            # Get agent record
            dict_cur.execute(
                "SELECT id, chat_id, created_at, meta, slug FROM agents WHERE id = %s",
                (agent_id,),
            )
            agent_row = dict_cur.fetchone()
            if not agent_row:
                return jsonify({"error": "Agent not found"}), 404

            agent_meta = agent_row["meta"] or {}

        # get_messages uses positional indexing, needs a regular cursor
        with conn.cursor() as cur:
            db_messages = get_messages(cur, agent_row["chat_id"], agent_id=agent_id)

        # Build system prompt from agent config
        agent_config_dict = agent_meta.get("agent_config", {})
        try:
            agent_config = AgentConfig.model_validate(agent_config_dict)
            system_prompt, api_messages = build_claude_input(
                db_messages, agent_config.rendering
            )
        except Exception:
            system_prompt = None
            api_messages = convert_db_messages_to_claude_messages(db_messages)

        # Truncate base64 image data for display
        for msg in api_messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image":
                        source = block.get("source", {})
                        if source.get("type") == "base64":
                            source["data"] = "[truncated]"

        return jsonify(
            {
                "agent_id": agent_id,
                "agent_slug": agent_row["slug"],
                "chat_id": agent_row["chat_id"],
                "created_at": agent_row["created_at"].isoformat()
                if agent_row["created_at"]
                else None,
                "agent_config": agent_config_dict,
                "agent_meta": agent_meta,
                "system_prompt": system_prompt,
                "history": api_messages,
                "num_messages": len(api_messages),
                "num_db_turns": len(db_messages),
            }
        )
    finally:
        conn.close()


@app.route("/api/agent-view")
def api_agent_view():
    """Return the full agent view: system prompt + message history as Claude sees it."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            messages = get_messages(
                cur, DEFAULT_CHAT_ID, limit=HISTORY_LENGTH_LONG_TURNS
            )
            scheduled_invocations = get_schedules(cur)

        system_prompt, history = build_claude_input(
            messages,
            DEFAULT_AGENT_CONFIG.rendering,
            scheduled_invocations=scheduled_invocations,
        )

        # Truncate base64 image data in history for display
        for msg in history:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image":
                        source = block.get("source", {})
                        if source.get("type") == "base64":
                            source["data"] = "[truncated]"

        tool_specs = get_tool_specs_for_agent_config(DEFAULT_AGENT_CONFIG)
        tool_names = [t["name"] for t in tool_specs]

        return jsonify(
            {
                "system_prompt": system_prompt,
                "history": history,
                "num_messages": len(history),
                "num_db_turns": len(messages),
                "tools": tool_names,
            }
        )
    finally:
        conn.close()


@app.route("/api/agent-view/tokens")
def api_agent_view_tokens():
    """Count tokens for the full agent input, with per-message breakdown."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            messages = get_messages(
                cur, DEFAULT_CHAT_ID, limit=HISTORY_LENGTH_LONG_TURNS
            )
            scheduled_invocations = get_schedules(cur)

        system_prompt, history = build_claude_input(
            messages,
            DEFAULT_AGENT_CONFIG.rendering,
            scheduled_invocations=scheduled_invocations,
        )

        tool_specs = get_tool_specs_for_agent_config(DEFAULT_AGENT_CONFIG)

        def has_tool_use(msg: dict) -> bool:
            content = msg.get("content", [])
            if isinstance(content, str):
                return False
            return any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content
            )

        def is_countable_boundary(i: int) -> bool:
            msg = history[i]
            if msg["role"] == "user":
                return True
            return not has_tool_use(msg)

        def msg_to_text(msg: dict) -> str:
            content = msg.get("content", [])
            if isinstance(content, str):
                return content
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(json.dumps(block.get("input", {})))
                elif block.get("type") == "tool_result":
                    rc = block.get("content", "")
                    if isinstance(rc, list):
                        parts.append(
                            " ".join(
                                b.get("text", "") for b in rc if isinstance(b, dict)
                            )
                        )
                    else:
                        parts.append(str(rc))
                elif block.get("type") in ("thinking", "redacted_thinking"):
                    pass
            return "\n".join(parts)

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
        # Start from system token count so first message doesn't include system tokens
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
    finally:
        conn.close()


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM messages")
            total_messages = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) as cnt FROM messages WHERE is_visible = true")
            visible_messages = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) as cnt FROM schedules WHERE is_active = true")
            active_schedules = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(DISTINCT chat_id) as cnt FROM messages")
            unique_chats = cur.fetchone()["cnt"]

        return jsonify(
            {
                "total_messages": total_messages,
                "visible_messages": visible_messages,
                "active_schedules": active_schedules,
                "unique_chats": unique_chats,
            }
        )
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True, port=5001)
