"""Schedule listing route."""

import psycopg2.extras
from flask import Blueprint, jsonify

from dashboard.helpers import get_db

bp = Blueprint("schedules", __name__)


@bp.route("/api/schedules")
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
