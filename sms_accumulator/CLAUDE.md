# SMS Accumulator

Go service that captures SMS/RCS messages from Anton's phone via Google Messages web pairing protocol (`libgm`).

## How it works

Uses `go.mau.fi/mautrix-gmessages/pkg/libgm` — a Go library that reverse-engineers the Google Messages web client protocol (same as messages.google.com). The service pairs with the phone once via QR code, then maintains a persistent connection receiving all message events in both directions.

## Key files

- `main.go` — entire service (single file): pairing, event handling, SQLite storage, HTTP API
- `Dockerfile` — multi-stage Go build (CGO enabled for sqlite3)
- `Dockerfile.prebuilt` — lightweight image using pre-compiled binary (for the e2-micro VM)

## Architecture

### Message flow
1. Phone ↔ Google servers ↔ libgm long-polling connection → `WrappedMessage` events
2. `processMessage()` extracts sender, body, direction, timestamp → SQLite
3. HTTP API serves queries from SQLite

### Direction detection
Derived from `MessageStatusType` (a protobuf enum):
- 1–99: **outgoing** (sent by us)
- 100–199: **incoming** (received)
- 200–299: tombstone/system (skipped)
- 300: deleted (skipped)

### Participant resolution
libgm identifies senders by opaque participant IDs (not phone numbers). On startup and when conversations update, we cache participant ID → phone number mappings from conversation metadata. This cache is in-memory only — rebuilt on each restart.

### Auth lifecycle
- First run: `NewAuthData()` generates AES + ECDSA keys → QR pairing → saves to `/data/auth.json`
- Subsequent runs: loads `auth.json` → `Connect()` refreshes tachyon auth token automatically
- `AuthTokenRefreshed` event → re-saves `auth.json` (token expires ~4h, auto-refreshed by libgm)
- If phone unpairs (`RevokePairData`), deletes `auth.json` → next restart triggers fresh pairing

### Old message filtering
On connect, libgm replays recent messages with `IsOld=true`. We skip these to avoid duplicates.

## HTTP API

### `GET /messages`
Query params (all optional):
- `hours` (float, default 24) — lookback window
- `conversation_id` (string) — filter by conversation ID
- `sender` (string) — partial match on sender or sender_name (SQL LIKE)
- `limit` (int, default 100)

Response: JSON array of `{timestamp, sender, sender_name, message, direction, conversation_id}`

### `GET /health`
Returns `{"status": "ok"}`

## SQLite schema
```sql
messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER,       -- unix ms
    sender TEXT,                 -- phone number or participant ID
    sender_name TEXT,            -- display name (often empty)
    message TEXT,                -- body text or "[media: mime/type]"
    direction TEXT,              -- "incoming", "outgoing", or "unknown"
    conversation_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
```
100-day retention, cleaned up hourly.

## Deployment

Runs on the same GCP VM as the Signal accumulator (`signal-api`, `100.108.7.78`).

### Building
The VM is an `e2-micro` (0.25 vCPU, 1GB RAM) — **too small to compile Go with CGO**. Cross-compile locally instead:
```bash
# From sms_accumulator/ directory
docker run --rm -v "$(pwd):/build" -w /build golang:1.23 sh -c \
  "apt-get update -qq && apt-get install -y -qq gcc-x86-64-linux-gnu >/dev/null 2>&1 && \
   CGO_ENABLED=1 CC=x86_64-linux-gnu-gcc GOOS=linux GOARCH=amd64 go build -o sms-accumulator ."
```

### Deploying
```bash
# Copy binary + Dockerfile to VM
gcloud compute scp sms-accumulator Dockerfile.prebuilt \
  signal-api:~/sms_accumulator/ --zone us-central1-a --tunnel-through-iap

# SSH to VM
gcloud compute ssh signal-api --zone us-central1-a --tunnel-through-iap

# On VM:
cd ~/sms_accumulator
sudo docker stop sms-accumulator && sudo docker rm sms-accumulator
sudo docker build -f Dockerfile.prebuilt -t sms-accumulator .
sudo docker run -d --name sms-accumulator \
  --restart=unless-stopped \
  -p 100.108.7.78:8082:8082 \
  -v sms-accumulator-data:/data \
  sms-accumulator
```

### First-run pairing
Must run interactively to see the QR code:
```bash
sudo docker run -it --name sms-accumulator \
  -p 100.108.7.78:8082:8082 \
  -v sms-accumulator-data:/data \
  sms-accumulator
# QR appears → Google Messages app → ⋮ menu → Device Pairing → QR Scanner
# After pairing succeeds, Ctrl+C then: sudo docker start sms-accumulator
```

### Re-pairing
If pairing breaks (phone unpaired, fatal auth error):
```bash
# Delete auth and restart interactively
sudo docker run --rm -v sms-accumulator-data:/data alpine rm -f /data/auth.json
sudo docker stop sms-accumulator && sudo docker rm sms-accumulator
# Then follow first-run pairing steps above
```

### Troubleshooting
- **"no such column: direction"** — old DB schema from Python accumulator. Delete the DB:
  ```bash
  sudo docker run --rm -v sms-accumulator-data:/data alpine \
    rm -f /data/sms_messages.db /data/sms_messages.db-shm /data/sms_messages.db-wal
  ```
  Then restart the container.
- **SSH to VM hangs** — the e2-micro may be overloaded. Reset via:
  `gcloud compute instances reset signal-api --zone us-central1-a`
- **Logs**: `sudo docker logs --tail 50 sms-accumulator`

## libgm dependency notes
- Library: `go.mau.fi/mautrix-gmessages v0.5.1`
- `NewClient(authData, logger)` — no PushKeys param in this version
- `FetchConfig()` — no context param, returns `(config, error)`
- `WrappedMessage` embeds `*gmproto.Message` directly (access via `.Message`, not `.GetData()`)
- Timestamps are **microseconds** — divide by 1000 for milliseconds
