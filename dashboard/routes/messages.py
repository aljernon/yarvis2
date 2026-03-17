"""Message browsing and turn token counting routes."""

import base64
import math

import psycopg2.extras
from flask import Blueprint, Response, jsonify, request

from dashboard.helpers import (
    PER_PAGE,
    get_db,
    get_sender_name,
    turn_to_api_messages,
)
from dashboard.token_counting import (
    count_tokens_cached,
    has_tool_use,
    strip_thinking_blocks,
)
from yarvis_ptb.settings import ROOT_USER_ID

bp = Blueprint("messages", __name__)
DEFAULT_CHAT_ID = ROOT_USER_ID


@bp.route("/api/chats")
def api_chats():
    """Return list of chats with message counts, plus the default chat_id."""
    from yarvis_ptb.settings import USER_ID_MAP

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

        return jsonify({"chats": chats, "default_chat_id": DEFAULT_CHAT_ID})
    finally:
        conn.close()


@bp.route("/api/messages")
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

            cur.execute(f"SELECT COUNT(*) as cnt FROM messages m {where_sql}", params)
            total = cur.fetchone()["cnt"]

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


@bp.route("/api/turn/<int:turn_id>/tokens")
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

        def is_countable_boundary(i: int) -> bool:
            msg = api_msgs[i]
            if msg["role"] == "user":
                return True
            return not has_tool_use(msg)

        def has_text_blocks(msg: dict) -> bool:
            content = msg.get("content", [])
            if not isinstance(content, list):
                return False
            return any(isinstance(b, dict) and b.get("type") == "text" for b in content)

        def count_per_block(msg: dict) -> list[dict] | None:
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
                    blocks.append(None)
                elif btype in ("thinking", "redacted_thinking"):
                    blocks.append(None)
            return blocks if blocks else None

        results = [None] * len(api_msgs)
        prev_total = 0

        for i in range(len(api_msgs)):
            if has_tool_use(api_msgs[i]):
                if has_text_blocks(api_msgs[i]):
                    blocks = count_per_block(api_msgs[i])
                    results[i] = {
                        "role": "assistant",
                        "tokens": None,
                        "blocks": blocks,
                    }
                else:
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


@bp.route("/api/stats")
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


@bp.route("/api/message/<int:msg_id>/image")
def api_message_image(msg_id: int):
    """Serve the raw image bytes for a message that has image_b64 in meta."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT meta FROM messages WHERE id = %s", (msg_id,))
            row = cur.fetchone()
            if not row:
                return Response("not found", status=404)
        meta = row["meta"] or {}
        image_b64 = meta.get("image_b64")
        if not image_b64:
            return Response("no image", status=404)
        image_bytes = base64.b64decode(image_b64)
        return Response(image_bytes, content_type="image/jpeg")
    finally:
        conn.close()
