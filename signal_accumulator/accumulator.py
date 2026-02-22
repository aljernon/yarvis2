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
            message TEXT,
            is_sync INTEGER DEFAULT 0,
            raw_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON messages(timestamp_ms)")
    conn.commit()
    conn.close()


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

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (timestamp_ms, source_number, source_name, destination_number, message, is_sync, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, source, source_name, dest, text, is_sync, json.dumps(envelope)),
    )
    conn.commit()
    conn.close()
    print(
        f"Stored: {'[sync]' if is_sync else '[recv]'} {source_name or source}: {text[:80]}"
    )


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
    url = f"{SIGNAL_WS_URL}/v1/receive/{SIGNAL_PHONE}"
    while True:
        try:
            print(f"Connecting to {url}")
            ws = websocket.create_connection(url, timeout=None)
            print("Connected, listening for messages...")
            while True:
                raw = ws.recv()
                if raw:
                    data = json.loads(raw)
                    envelope = data.get("envelope", {})
                    store_message(envelope)
        except Exception as e:
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
                "message": r["message"],
                "is_sync": bool(r["is_sync"]),
            }
        )

    return jsonify(messages)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    init_db()
    listener_thread = threading.Thread(target=ws_listener, daemon=True)
    listener_thread.start()
    cleanup_thread = threading.Thread(target=cleanup_old_messages, daemon=True)
    cleanup_thread.start()
    print(f"Starting HTTP API on {LISTEN_HOST}:{LISTEN_PORT}")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
