"""
Step 1: Capture raw signal-cli envelopes into SQLite.

Runs `signal-cli receive`, dumps every JSON line verbatim into raw_envelopes table.
Designed to be called by cron every 5 minutes.

Usage:
    python capture.py
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

DB_PATH = os.environ.get("SIGNAL_DB_PATH", "/data/signal_messages.db")
SIGNAL_CLI = os.environ.get("SIGNAL_CLI_PATH", "signal-cli")
SIGNAL_CONFIG = os.environ.get("SIGNAL_CLI_CONFIG", "/home/.local/share/signal-cli")
RECEIVE_TIMEOUT = int(os.environ.get("SIGNAL_RECEIVE_TIMEOUT", "10"))
SIGNAL_ACCOUNT = os.environ.get("SIGNAL_ACCOUNT", "")
LOCK_PATH = os.environ.get("SIGNAL_LOCK_PATH", "/tmp/signal_capture.lock")
VIEW_ONCE_ECHO_SENDERS = [
    s.strip()
    for s in os.environ.get("VIEW_ONCE_ECHO_SENDERS", "").split(",")
    if s.strip()
]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_envelopes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            envelope_json TEXT NOT NULL,
            processed INTEGER DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_processed ON raw_envelopes(processed)"
    )
    conn.commit()
    conn.close()


def acquire_lock() -> bool:
    """Simple file-based lock to prevent overlapping runs."""
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Check if the holding process is still alive
        try:
            with open(LOCK_PATH) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # check if alive
            return False  # still running
        except (ValueError, ProcessLookupError, PermissionError):
            # Stale lock
            os.unlink(LOCK_PATH)
            return acquire_lock()


def release_lock():
    try:
        os.unlink(LOCK_PATH)
    except FileNotFoundError:
        pass


def _echo_view_once(envelope_json: str) -> None:
    """If this is a view-once DM from an echo-enabled sender, send attachments back."""
    if not VIEW_ONCE_ECHO_SENDERS:
        return

    raw = json.loads(envelope_json)
    envelope = raw.get("envelope", {})

    # Check both incoming (dataMessage) and self-sent sync (syncMessage.sentMessage)
    dm = envelope.get("dataMessage")
    sync_sent = (envelope.get("syncMessage") or {}).get("sentMessage")

    if dm and dm.get("viewOnce"):
        msg_obj = dm
        # Identify sender by UUID (preferred) or phone number
        sender_id = envelope.get("sourceUuid") or envelope.get("sourceNumber") or ""
    elif sync_sent and sync_sent.get("viewOnce"):
        msg_obj = sync_sent
        sender_id = SIGNAL_ACCOUNT  # self-sent
    else:
        return

    # Skip if chat has self-destruct timer
    if msg_obj.get("expiresInSeconds", 0) > 0:
        return

    # Only echo for configured senders (UUIDs or phone numbers)
    if sender_id not in VIEW_ONCE_ECHO_SENDERS:
        return

    # Skip group messages
    if msg_obj.get("groupInfo") or msg_obj.get("groupV2"):
        return

    attachments = msg_obj.get("attachments", [])
    if not attachments:
        return

    for att in attachments:
        att_id = att.get("id")
        if not att_id:
            continue
        att_path = os.path.join(SIGNAL_CONFIG, "attachments", att_id)
        if not os.path.isfile(att_path):
            print(f"View-once attachment not found: {att_path}")
            continue

        print(f"Echoing view-once {att.get('contentType', '?')} from {sender_id}")
        sys.stdout.flush()

        cmd = [SIGNAL_CLI, "--config", SIGNAL_CONFIG]
        if SIGNAL_ACCOUNT:
            cmd += ["-a", SIGNAL_ACCOUNT]
        cmd += ["send", "-m", "", "--attachment", att_path, sender_id]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"View-once echo sent to {sender_id}")
        else:
            print(
                f"View-once echo failed (rc={result.returncode}): {result.stderr[:300]}"
            )
        sys.stdout.flush()


def capture():
    cmd = [
        SIGNAL_CLI,
        "--output=json",
        "--config",
        SIGNAL_CONFIG,
    ]
    if SIGNAL_ACCOUNT:
        cmd += ["-a", SIGNAL_ACCOUNT]
    cmd += [
        "receive",
        "--timeout",
        str(RECEIVE_TIMEOUT),
    ]

    print(f"[{datetime.now(timezone.utc).isoformat()}] Running: {' '.join(cmd)}")
    sys.stdout.flush()

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=RECEIVE_TIMEOUT + 30,  # extra margin
    )

    if result.returncode != 0:
        print(f"signal-cli exited with code {result.returncode}")
        if result.stderr:
            print(f"stderr: {result.stderr[:500]}")
        sys.stdout.flush()

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        print(f"No envelopes received")
        sys.stdout.flush()
        return 0

    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for line in lines:
        # Validate it's JSON before storing
        try:
            json.loads(line)
        except json.JSONDecodeError:
            print(f"Skipping non-JSON line: {line[:100]}")
            continue

        # Echo view-once messages before storing
        if VIEW_ONCE_ECHO_SENDERS:
            try:
                _echo_view_once(line)
            except Exception as e:
                print(f"View-once echo error: {e}")
                sys.stdout.flush()

        conn.execute(
            "INSERT INTO raw_envelopes (captured_at, envelope_json) VALUES (?, ?)",
            (now, line),
        )
        count += 1
    conn.commit()
    conn.close()
    print(f"Captured {count} envelopes")
    sys.stdout.flush()
    return count


def main():
    init_db()

    if "--init-db-only" in sys.argv:
        print("DB initialized")
        return

    if not acquire_lock():
        print("Another capture is already running, skipping")
        sys.stdout.flush()
        return

    try:
        capture()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
