"""Connect to signal-cli JSON-RPC directly (port 6001) and print all messages.
Run: python debug_signal_jsonrpc.py
Then send a message from your phone.
"""

import json
import socket
import sys

sock = socket.create_connection(("100.108.7.78", 6001), timeout=60)
print("Connected to JSON-RPC. Send a message from your phone.")
sys.stdout.flush()

buf = b""
while True:
    try:
        data = sock.recv(4096)
    except socket.timeout:
        print("(timeout, still waiting...)")
        sys.stdout.flush()
        continue
    if not data:
        print("Connection closed")
        break
    buf += data
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            method = msg.get("method", "")
            params = msg.get("params", {})
            env = params.get("envelope", params)
            sync = env.get("syncMessage", {})
            sent = sync.get("sentMessage", {})
            print(
                f"method={method} keys={sorted(env.keys())} sync_keys={sorted(sync.keys())} sent_keys={sorted(sent.keys())}"
            )
            if sent:
                print(f"  sentMessage: {json.dumps(sent)[:300]}")
        except json.JSONDecodeError:
            print(f"raw: {line[:200]}")
        sys.stdout.flush()
