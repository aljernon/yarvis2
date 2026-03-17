"""Connect to signal-cli websocket and print all envelope keys.
Run: conda run -n clam python debug_signal_ws.py
Then send a message from your phone and watch the output.
"""

import json
import sys

import websocket

ws = websocket.create_connection(
    "ws://100.108.7.78:8080/v1/receive/+16506603785", timeout=60
)
print("Connected. Send a message from your phone.")
sys.stdout.flush()

while True:
    try:
        raw = ws.recv()
    except websocket.WebSocketTimeoutException:
        print("(timeout, still waiting...)")
        sys.stdout.flush()
        continue

    env = json.loads(raw).get("envelope", {})
    sync = env.get("syncMessage", {})
    sent = sync.get("sentMessage", {})
    print(
        f"keys={sorted(env.keys())} "
        f"sync_keys={sorted(sync.keys())} "
        f"sent_keys={sorted(sent.keys())}"
    )
    if sent:
        print(f"  sentMessage: {json.dumps(sent)[:300]}")
    sys.stdout.flush()
