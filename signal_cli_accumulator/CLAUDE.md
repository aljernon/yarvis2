# Signal CLI Accumulator (v2)

Replaces `signal_accumulator/`. Uses `signal-cli receive` directly instead of REST API websocket.

## Architecture

- **Capture** (`capture.py`): Runs `signal-cli receive`, dumps raw JSON envelopes into `raw_envelopes` SQLite table. Called by cron every 5 minutes. Dead simple — never parses, never loses data.
- **Serve** (`serve.py`): Flask HTTP API, parses raw_envelopes on the fly. Same `/messages` and `/health` endpoints as old accumulator. Temporarily also reads from old accumulator's `messages` table (remove after 2026-03-23).

## View-Once Echo

When a view-once (one-time-show) image arrives as a DM from a configured sender, `capture.py` immediately sends the attachment back as a regular message.

- **Config**: `VIEW_ONCE_ECHO_SENDERS` env var — comma-separated UUIDs or phone numbers
- **Scope**: DMs only (skips groups), skips chats with self-destruct timer
- **Defensive**: wrapped in try/except so bugs never break accumulation
- Self-to-self view-once doesn't work (Signal marks it "viewed" instantly on linked devices and doesn't include the attachment in sync)

## Important

**NEVER use `--send-read-receipts`** with signal-cli. It breaks sync message delivery from the primary device permanently. See `signal_accumulator/CLAUDE.md` for details.

## Deployment

```bash
# Copy files to VM
gcloud compute scp signal_cli_accumulator/* signal-api:~/signal_cli_accumulator/ --zone us-central1-a --tunnel-through-iap

# On the VM: stop old container, build and deploy new one
sudo docker stop signal-combined 2>/dev/null
sudo docker rm signal-combined 2>/dev/null
cd ~/signal_cli_accumulator
sudo docker build -t signal-cli-accum .
sudo docker stop signal-cli-accum 2>/dev/null
sudo docker rm signal-cli-accum 2>/dev/null
sudo docker run -d --name signal-cli-accum \
  --restart=unless-stopped \
  -p 100.108.7.78:8081:8081 \
  -e VIEW_ONCE_ECHO_SENDERS="6efcbb90-f260-48eb-8c1a-a022c9a76435" \
  -v signal-cli-data-v2:/signal-cli-config \
  -v signal-accumulator-data-v2:/data \
  -v signal-accumulator-data:/data-legacy \
  signal-cli-accum

# Check capture logs
sudo docker exec signal-cli-accum cat /data/capture.log

# Check raw envelopes
sudo docker exec signal-cli-accum /opt/accumulator-venv/bin/python -c "import sqlite3; conn=sqlite3.connect('/data/signal_messages.db'); print(conn.execute('SELECT COUNT(*) FROM raw_envelopes').fetchone())"
```

## Re-linking the device

```bash
sudo docker exec -it signal-cli-accum signal-cli --config /signal-cli-config link -n signal-cli-accum-v2
```
