"""Migrate bot messages (user_id=-1) that lack message_params in meta.

Wraps the plain-text message body into the standard message_params format:
  [{"role": "assistant", "content": [{"type": "text", "text": "<original>"}]}]
and sets message="USE_CONTENT_FROM_META", meta={"message_params": ...}.

Safe to run multiple times (idempotent — only touches rows without message_params).
"""

import json
import os

import psycopg2

BOT_USER_ID = -1


def migrate():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute(
        "SELECT count(*) FROM messages WHERE user_id = %s"
        " AND (meta IS NULL OR meta->'message_params' IS NULL)",
        (BOT_USER_ID,),
    )
    count = cur.fetchone()[0]
    print(f"Found {count} bot messages to migrate")
    if count == 0:
        return

    # Fetch all rows to migrate
    cur.execute(
        "SELECT id, message, meta FROM messages WHERE user_id = %s"
        " AND (meta IS NULL OR meta->'message_params' IS NULL)",
        (BOT_USER_ID,),
    )
    rows = cur.fetchall()

    for msg_id, message, meta_json in rows:
        message_params = [
            {"role": "assistant", "content": [{"type": "text", "text": message}]}
        ]
        existing_meta = json.loads(meta_json) if meta_json else {}
        existing_meta["message_params"] = message_params
        cur.execute(
            "UPDATE messages SET message = %s, meta = %s WHERE id = %s",
            ("USE_CONTENT_FROM_META", json.dumps(existing_meta), msg_id),
        )

    conn.commit()
    print(f"Migrated {len(rows)} messages")
    conn.close()


if __name__ == "__main__":
    migrate()
