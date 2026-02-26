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
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Add yarvis_ptb to path so we can import settings
sys.path.insert(0, os.path.join(PROJECT_ROOT, "yarvis_ptb"))

DATABASE_URL = os.environ.get("DATABASE_URL")
BOT_USER_ID = -1
SYSTEM_USER_ID = -2
TOOL_CALL_USER_ID = -3

os.environ.setdefault("SETTINGS_NAME", "anton")
from yarvis_ptb.complex_chat import DEFAULT_COMPLEX_CHAT_CONFIG
from yarvis_ptb.prompting import (
    build_claude_input,
    convert_db_messages_to_claude_messages,
)
from yarvis_ptb.settings import DEFAULT_TIMEZONE, ROOT_USER_ID, USER_ID_MAP
from yarvis_ptb.settings.main import HISTORY_LENGTH_LONG_TURNS
from yarvis_ptb.storage import (
    DbMessage,
    get_messages,
    get_scheduled_invocations,
)

DEFAULT_CHAT_ID = ROOT_USER_ID
PER_PAGE = 500


def strip_thinking_blocks(messages: list[dict]) -> list[dict]:
    """Remove thinking/redacted_thinking blocks from messages for token counting."""
    cleaned = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            filtered = [
                b
                for b in content
                if not (
                    isinstance(b, dict)
                    and b.get("type") in ("thinking", "redacted_thinking")
                )
            ]
            if filtered:
                cleaned.append({**msg, "content": filtered})
            # Skip messages that become empty after stripping
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


def count_tokens_cached(*, system: str | None = None, messages: list[dict]) -> int:
    """Count tokens with on-disk caching keyed by content hash."""
    cache_data = json.dumps(
        {"system": system, "messages": messages}, sort_keys=True, default=str
    )
    key = _cache_key(cache_data)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    kwargs = {"model": TOKEN_COUNT_MODEL, "messages": messages}
    if system is not None:
        kwargs["system"] = system
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


@app.route("/invocations")
def invocations_page():
    return render_template("invocations.html")


@app.route("/agent")
def agent_page():
    return render_template("agent.html")


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
            where_clauses = ["chat_id = %s"]
            params: list = [chat_id]

            if search:
                where_clauses.append("message ILIKE %s")
                params.append(f"%{search}%")
            if min_bytes > 0:
                where_clauses.append(
                    "octet_length(meta::text) + octet_length(message) >= %s"
                )
                params.append(min_bytes)

            where_sql = "WHERE " + " AND ".join(where_clauses)

            # Count
            cur.execute(f"SELECT COUNT(*) as cnt FROM messages {where_sql}", params)
            total = cur.fetchone()["cnt"]

            # Fetch page
            cur.execute(
                f"""
                SELECT id, created_at, chat_id, user_id, message, meta, marked_for_archive,
                       octet_length(meta::text) + octet_length(message) as total_bytes,
                       agent_id
                FROM messages
                {where_sql}
                ORDER BY created_at DESC, id DESC
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


@app.route("/api/invocations")
def api_invocations():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, created_at, scheduled_at, chat_id, is_active, is_recurring, reason, meta
                FROM invocations
                ORDER BY is_active DESC, scheduled_at DESC
            """)
            rows = cur.fetchall()

        invocations = []
        for row in rows:
            invocations.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"].isoformat()
                    if row["created_at"]
                    else None,
                    "scheduled_at": row["scheduled_at"].isoformat()
                    if row["scheduled_at"]
                    else None,
                    "chat_id": row["chat_id"],
                    "is_active": row["is_active"],
                    "is_recurring": row["is_recurring"],
                    "reason": row["reason"],
                    "meta": row["meta"] or {},
                }
            )

        return jsonify({"invocations": invocations})
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
            scheduled_invocations = get_scheduled_invocations(cur)

        system_prompt, history = build_claude_input(
            messages,
            DEFAULT_COMPLEX_CHAT_CONFIG,
            put_context_at_the_end=True,
            put_context_at_the_beginning=False,
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

        return jsonify(
            {
                "system_prompt": system_prompt,
                "history": history,
                "num_messages": len(history),
                "num_db_turns": len(messages),
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
            scheduled_invocations = get_scheduled_invocations(cur)

        system_prompt, history = build_claude_input(
            messages,
            DEFAULT_COMPLEX_CHAT_CONFIG,
            put_context_at_the_end=True,
            put_context_at_the_beginning=False,
            scheduled_invocations=scheduled_invocations,
        )

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

        # System prompt tokens
        system_tokens = count_tokens_cached(
            system=system_prompt,
            messages=[{"role": "user", "content": "x"}],
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
            total = count_tokens_cached(system=system_prompt, messages=conversation)
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
                "system_tokens": system_tokens,
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

            cur.execute(
                "SELECT COUNT(*) as cnt FROM invocations WHERE is_active = true"
            )
            active_invocations = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(DISTINCT chat_id) as cnt FROM messages")
            unique_chats = cur.fetchone()["cnt"]

        return jsonify(
            {
                "total_messages": total_messages,
                "visible_messages": visible_messages,
                "active_invocations": active_invocations,
                "unique_chats": unique_chats,
            }
        )
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True, port=5001)
