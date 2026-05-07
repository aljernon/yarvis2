"""Agent listing and subagent detail routes."""

import psycopg2.extras
from flask import Blueprint, jsonify, request

from dashboard.helpers import (
    compute_full_turns_diff,
    extract_api_msg_db_ids,
    extract_turn_usages,
    get_db,
    lookup_agent_by_ref,
    truncate_base64_images,
)
from yarvis_ptb.agent_config import AgentConfig
from yarvis_ptb.prompting import (
    build_claude_input,
    convert_db_messages_to_claude_messages,
)
from yarvis_ptb.storage import get_messages

bp = Blueprint("agents", __name__)


@bp.route("/api/agents")
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


@bp.route("/api/subagent/<path:agent_ref>")
def api_subagent(agent_ref: str):
    """Return a subagent's config and message history.

    `agent_ref` is either a numeric agent id or a slug (slugs may contain '/',
    e.g. ``archive/2026-05-05``).
    """
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as dict_cur:
            agent_row = lookup_agent_by_ref(
                dict_cur, agent_ref, columns="id, chat_id, created_at, meta, slug"
            )
            if not agent_row:
                return jsonify({"error": "Agent not found"}), 404

            agent_id = agent_row["id"]
            agent_meta = agent_row["meta"] or {}

        with conn.cursor() as cur:
            db_messages = get_messages(cur, agent_row["chat_id"], agent_id=agent_id)

        agent_config_dict = agent_meta.get("agent_config", {})
        skip_forget = request.args.get("full") == "1"

        def _render(skip: bool):
            try:
                agent_config = AgentConfig.model_validate(agent_config_dict)
                sp, msgs, anns = build_claude_input(
                    db_messages,
                    agent_config.rendering,
                    skip_forget_above=skip,
                    keep_thinking=True,
                )
            except Exception:
                sp = None
                msgs, anns = convert_db_messages_to_claude_messages(
                    db_messages, skip_forget_above=skip, keep_thinking=True
                )
            truncate_base64_images(msgs)
            return sp, msgs, anns

        system_prompt, api_messages, annotations = _render(skip_forget)

        full_turns: dict[str, list] = {}
        if not skip_forget:
            _, full_history, full_annotations = _render(True)
            full_turns = compute_full_turns_diff(
                api_messages, annotations, full_history, full_annotations
            )

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
                "turn_usages": extract_turn_usages(db_messages),
                "db_ids": extract_api_msg_db_ids(db_messages),
                "num_messages": len(api_messages),
                "num_db_turns": len(db_messages),
                "full_turns": full_turns,
            }
        )
    finally:
        conn.close()
