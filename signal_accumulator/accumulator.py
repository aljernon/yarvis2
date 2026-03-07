"""
Signal message accumulator.
Listens to signal-cli-rest-api websocket and stores messages in SQLite.
Exposes a simple HTTP API for querying message history.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import websocket
from flask import Flask, jsonify, request

DB_PATH = os.environ.get("SIGNAL_DB_PATH", "/data/signal_messages.db")
SIGNAL_WS_URL = os.environ.get("SIGNAL_WS_URL", "ws://localhost:8080")
SIGNAL_PHONE = os.environ.get("SIGNAL_PHONE_NUMBER", "+16506603785")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8081"))

app = Flask(__name__)

# Websocket connection state, updated by ws_listener thread
ws_connected = False
ws_last_connected_at = None
ws_last_error = None


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_ms INTEGER,
            source_number TEXT,
            source_name TEXT,
            destination_number TEXT,
            destination_name TEXT DEFAULT '',
            group_id TEXT DEFAULT '',
            group_name TEXT DEFAULT '',
            message TEXT,
            is_sync INTEGER DEFAULT 0,
            is_group INTEGER DEFAULT 0,
            raw_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON messages(timestamp_ms)")
    # Migrations for existing databases
    for col, typedef in [
        ("destination_name", "TEXT DEFAULT ''"),
        ("group_id", "TEXT DEFAULT ''"),
        ("group_name", "TEXT DEFAULT ''"),
        ("is_group", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()


def _resolve_contact_name(conn, number):
    """Look up a contact name from previously received messages."""
    if not number:
        return ""
    row = conn.execute(
        "SELECT source_name FROM messages WHERE source_number = ? AND source_name != '' ORDER BY timestamp_ms DESC LIMIT 1",
        (number,),
    ).fetchone()
    return row[0] if row else ""


def _extract_group_info(msg_obj):
    """Extract group ID and name from a dataMessage or sentMessage."""
    group = msg_obj.get("groupInfo") or msg_obj.get("groupV2") or {}
    group_id = group.get("groupId", "")
    group_name = group.get("groupName", "") or group.get("name", "")
    return group_id, group_name


# Cache of group_id -> group_name learned from messages
_group_name_cache = {}


def _resolve_group_name(conn, group_id):
    """Look up a group name from cache or previously stored messages."""
    if group_id in _group_name_cache:
        return _group_name_cache[group_id]
    row = conn.execute(
        "SELECT group_name FROM messages WHERE group_id = ? AND group_name != '' ORDER BY timestamp_ms DESC LIMIT 1",
        (group_id,),
    ).fetchone()
    name = row[0] if row else ""
    if name:
        _group_name_cache[group_id] = name
    return name


def store_message(envelope):
    source = envelope.get("sourceNumber", "")
    source_name = envelope.get("sourceName", "")
    ts = envelope.get("timestamp", 0)

    # Direct incoming message
    dm = envelope.get("dataMessage", {})
    # Sync message (sent by account owner from another device)
    sync = envelope.get("syncMessage", {}).get("sentMessage", {})

    text = dm.get("message") or sync.get("message")
    if not text:
        return

    is_sync = 1 if sync.get("message") else 0
    dest = sync.get("destinationNumber", "") if is_sync else ""

    # Group info from whichever message object has content
    msg_obj = sync if is_sync else dm
    group_id, group_name = _extract_group_info(msg_obj)
    is_group = 1 if group_id else 0

    conn = sqlite3.connect(DB_PATH)

    # Resolve destination name for sync DMs
    dest_name = ""
    if is_sync and dest:
        dest_name = _resolve_contact_name(conn, dest)

    # Resolve group name if we got an ID but no name
    if group_id and not group_name:
        group_name = _resolve_group_name(conn, group_id)
    elif group_id and group_name:
        _group_name_cache[group_id] = group_name

    conn.execute(
        "INSERT INTO messages (timestamp_ms, source_number, source_name, destination_number, destination_name, group_id, group_name, message, is_sync, is_group, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            source,
            source_name,
            dest,
            dest_name,
            group_id,
            group_name,
            text,
            is_sync,
            is_group,
            json.dumps(envelope),
        ),
    )
    conn.commit()
    conn.close()

    label = "[sync]" if is_sync else "[recv]"
    who = source_name or source
    if is_group:
        where = f" in {group_name or group_id}"
    elif is_sync:
        where = f" -> {dest_name or dest}"
    else:
        where = ""
    print(f"Stored: {label} {who}{where}: {text[:80]}")


RETENTION_DAYS = 7


def cleanup_old_messages():
    while True:
        try:
            cutoff_ms = int(
                (
                    datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
                ).timestamp()
                * 1000
            )
            conn = sqlite3.connect(DB_PATH)
            deleted = conn.execute(
                "DELETE FROM messages WHERE timestamp_ms < ?", (cutoff_ms,)
            ).rowcount
            conn.commit()
            conn.close()
            if deleted:
                print(f"Cleaned up {deleted} messages older than {RETENTION_DAYS} days")
        except Exception as e:
            print(f"Cleanup error: {e}")
        time.sleep(3600)  # run every hour


def ws_listener():
    global ws_connected, ws_last_connected_at, ws_last_error
    url = f"{SIGNAL_WS_URL}/v1/receive/{SIGNAL_PHONE}"
    while True:
        try:
            print(f"Connecting to {url}")
            ws_connected = False
            ws = websocket.create_connection(url, timeout=30)
            ws.settimeout(None)  # block indefinitely on recv
            ws_connected = True
            ws_last_connected_at = datetime.now(timezone.utc)
            ws_last_error = None
            print("Connected, listening for messages...")
            while True:
                raw = ws.recv()
                if raw:
                    data = json.loads(raw)
                    envelope = data.get("envelope", {})
                    store_message(envelope)
        except Exception as e:
            ws_connected = False
            ws_last_error = str(e)
            print(f"Websocket error: {e}, reconnecting in 5s...")
            time.sleep(5)


@app.route("/messages")
def get_messages():
    """Query messages. Params: hours (default 24), source, limit (default 100)"""
    hours = float(request.args.get("hours", 24))
    source = request.args.get("source", None)
    limit = int(request.args.get("limit", 100))

    since_ms = int(
        (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000
    )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM messages WHERE timestamp_ms >= ?"
    params = [since_ms]

    if source:
        query += " AND source_number LIKE ?"
        params.append(f"%{source}%")

    query += " ORDER BY timestamp_ms DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    messages = []
    for r in rows:
        messages.append(
            {
                "timestamp": datetime.fromtimestamp(
                    r["timestamp_ms"] / 1000, tz=timezone.utc
                ).isoformat(),
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

    return jsonify(messages)


@app.route("/health")
def health():
    status = "ok" if ws_connected else "degraded"
    code = 200 if ws_connected else 503
    resp = {
        "status": status,
        "websocket_connected": ws_connected,
        "connected_since": ws_last_connected_at.isoformat()
        if ws_last_connected_at
        else None,
        "last_error": ws_last_error,
    }
    return jsonify(resp), code


if __name__ == "__main__":
    init_db()
    listener_thread = threading.Thread(target=ws_listener, daemon=True)
    listener_thread.start()
    cleanup_thread = threading.Thread(target=cleanup_old_messages, daemon=True)
    cleanup_thread.start()
    print(f"Starting HTTP API on {LISTEN_HOST}:{LISTEN_PORT}")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
