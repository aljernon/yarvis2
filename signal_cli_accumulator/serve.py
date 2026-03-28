"""
HTTP API for querying Signal messages.

Parses raw_envelopes on the fly — same /messages API as the old accumulator.
Temporarily also reads from old accumulator's messages table (LEGACY_DB_PATH).
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

DB_PATH = os.environ.get("SIGNAL_DB_PATH", "/data/signal_messages.db")
LEGACY_DB_PATH = os.environ.get(
    "SIGNAL_LEGACY_DB_PATH", "/data-legacy/signal_messages.db"
)
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8081"))
RETENTION_DAYS = 7

app = Flask(__name__)


def _ensure_contacts_table(conn: sqlite3.Connection):
    # Check if existing table has uuid column; if not, recreate
    try:
        conn.execute("SELECT uuid FROM contacts LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("DROP TABLE IF EXISTS contacts")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            uuid TEXT PRIMARY KEY,
            phone TEXT,
            name TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()


def _parse_envelope(envelope_json: str) -> dict | None:
    """Parse a raw envelope JSON into a message dict, or None if not a text message."""
    raw = json.loads(envelope_json)
    envelope = raw.get("envelope", {})

    source = envelope.get("sourceNumber", "")
    source_name = envelope.get("sourceName", "")
    source_uuid = envelope.get("sourceUuid", "")
    ts = envelope.get("timestamp", 0)

    dm = envelope.get("dataMessage") or {}
    sync = envelope.get("syncMessage", {}).get("sentMessage") or {}

    # Pick whichever has a text message
    msg_obj = sync if sync.get("message") else dm
    text = msg_obj.get("message")
    if not text:
        return None

    # Skip disappearing messages
    if msg_obj.get("expiresInSeconds", 0) > 0:
        return None

    is_sync = bool(sync.get("message"))
    dest = sync.get("destinationNumber", "") if is_sync else ""
    dest_uuid = sync.get("destinationUuid", "") if is_sync else ""

    # Group info
    group = msg_obj.get("groupInfo") or msg_obj.get("groupV2") or {}
    group_id = group.get("groupId", "")
    group_name = group.get("groupName", "") or group.get("name", "")
    is_group = bool(group_id)

    return {
        "timestamp": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
        "timestamp_ms": ts,
        "source_number": source,
        "source_name": source_name,
        "source_uuid": source_uuid,
        "destination_number": dest,
        "destination_uuid": dest_uuid,
        "destination_name": "",
        "group_id": group_id,
        "group_name": group_name,
        "is_group": is_group,
        "message": text,
        "is_sync": is_sync,
    }


def _get_new_messages(since_ms: int, source: str | None) -> list[dict]:
    """Read from new raw_envelopes table."""
    since_iso = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(DB_PATH)
        _ensure_contacts_table(conn)
        rows = conn.execute(
            "SELECT envelope_json FROM raw_envelopes WHERE captured_at >= ? ORDER BY id DESC",
            (since_iso,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    messages = []
    for (envelope_json,) in rows:
        msg = _parse_envelope(envelope_json)
        if msg is None:
            continue
        if msg["timestamp_ms"] < since_ms:
            continue
        if source and source not in (msg["source_number"] or ""):
            continue
        # Update contacts cache from incoming messages (keyed by UUID)
        if not msg["is_sync"] and msg["source_uuid"] and msg["source_name"]:
            conn.execute(
                "INSERT OR REPLACE INTO contacts (uuid, phone, name, updated_at) VALUES (?, ?, ?, ?)",
                (
                    msg["source_uuid"],
                    msg["source_number"] or None,
                    msg["source_name"],
                    msg["timestamp"],
                ),
            )
        messages.append(msg)

    conn.commit()

    # Resolve destination names for outgoing messages from contacts cache
    for msg in messages:
        if msg["is_sync"] and not msg["destination_name"]:
            dest_uuid = msg.get("destination_uuid", "")
            if dest_uuid:
                row = conn.execute(
                    "SELECT name FROM contacts WHERE uuid = ?",
                    (dest_uuid,),
                ).fetchone()
                if row:
                    msg["destination_name"] = row[0]

    conn.close()
    return messages


def _get_legacy_messages(since_ms: int, source: str | None) -> list[dict]:
    """Read from old accumulator's messages table. Temporary — remove after 2026-03-23."""
    if not os.path.exists(LEGACY_DB_PATH):
        return []
    try:
        conn = sqlite3.connect(LEGACY_DB_PATH)
        conn.row_factory = sqlite3.Row

        query = "SELECT * FROM messages WHERE timestamp_ms >= ?"
        params: list = [since_ms]
        if source:
            query += " AND source_number LIKE ?"
            params.append(f"%{source}%")
        query += " ORDER BY timestamp_ms DESC"

        rows = conn.execute(query, params).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return []

    messages = []
    for r in rows:
        messages.append(
            {
                "timestamp": datetime.fromtimestamp(
                    r["timestamp_ms"] / 1000, tz=timezone.utc
                ).isoformat(),
                "timestamp_ms": r["timestamp_ms"],
                "source_number": r["source_number"],
                "source_name": r["source_name"],
                "destination_number": r["destination_number"],
                "destination_name": r["destination_name"] or "",
                "group_id": r["group_id"] or "",
                "group_name": r["group_name"] or "",
                "is_group": bool(r["is_group"]),
                "message": r["message"],
                "is_sync": bool(r["is_sync"]),
            }
        )
    return messages


@app.route("/messages")
def get_messages():
    """Query messages. Params: hours (default 24), source, limit (default 100)"""
    hours = float(request.args.get("hours", 24))
    source = request.args.get("source", None)
    limit = int(request.args.get("limit", 100))

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    effective_since = max(since, cutoff)
    since_ms = int(effective_since.timestamp() * 1000)

    new_msgs = _get_new_messages(since_ms, source)
    legacy_msgs = _get_legacy_messages(since_ms, source)

    # Merge and deduplicate by timestamp_ms + message text
    seen = set()
    all_msgs = []
    for m in new_msgs + legacy_msgs:
        key = (m["timestamp_ms"], m["message"])
        if key in seen:
            continue
        seen.add(key)
        all_msgs.append(m)

    # Sort by timestamp descending, apply limit
    all_msgs.sort(key=lambda m: m["timestamp_ms"], reverse=True)
    all_msgs = all_msgs[:limit]

    # Remove internal fields
    for m in all_msgs:
        del m["timestamp_ms"]
        m.pop("source_uuid", None)
        m.pop("destination_uuid", None)

    return jsonify(all_msgs)


@app.route("/health")
def health():
    """Health check — reports capture status."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT MAX(captured_at) FROM raw_envelopes").fetchone()
        last_capture = row[0] if row else None
        total = conn.execute("SELECT COUNT(*) FROM raw_envelopes").fetchone()[0]
    except sqlite3.OperationalError:
        last_capture = None
        total = 0
    conn.close()

    return jsonify(
        {
            "status": "ok",
            "last_capture": last_capture,
            "total_envelopes": total,
        }
    )


def _seed_contacts():
    """Populate contacts table from all historical envelopes (run once at startup)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        _ensure_contacts_table(conn)
        rows = conn.execute("SELECT envelope_json FROM raw_envelopes").fetchall()
    except sqlite3.OperationalError:
        return

    count = 0
    for (envelope_json,) in rows:
        raw = json.loads(envelope_json)
        envelope = raw.get("envelope", {})
        source_uuid = envelope.get("sourceUuid", "")
        source_name = envelope.get("sourceName", "")
        source_number = envelope.get("sourceNumber")
        if source_uuid and source_name:
            conn.execute(
                "INSERT OR REPLACE INTO contacts (uuid, phone, name, updated_at) VALUES (?, ?, ?, ?)",
                (
                    source_uuid,
                    source_number,
                    source_name,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            count += 1
    conn.commit()
    conn.close()
    print(f"Seeded contacts from {count} envelopes")


if __name__ == "__main__":
    _seed_contacts()
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
