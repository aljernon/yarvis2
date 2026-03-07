# Signal Accumulator

Python (Flask) service that listens to the Signal API websocket, stores messages in SQLite, and exposes a query API on port 8081.

## Architecture

- Connects to `bbernhard/signal-cli-rest-api` via websocket
- Stores incoming Signal messages in SQLite
- Exposes HTTP query API

### Signal CLI REST API (upstream dependency)
- Docker container: `bbernhard/signal-cli-rest-api`, running in JSON-RPC mode
- Bound to Tailscale IP only (`-p 100.108.7.78:8080:8080`)
- Signal number: `+16506603785`
- Data volume: `signal-cli-data`

## Key files

- `accumulator.py` — entire service (single file): websocket listener, SQLite storage, Flask API
- `Dockerfile` — Python slim image
- `requirements.txt` — Flask + websocket dependencies

## HTTP API

### `GET /messages`
Query params (all optional):
- `hours` (float, default 24) — lookback window
- `sender` (string) — partial match on sender
- `limit` (int, default 100)

### `GET /health`
Returns `{"status": "ok"}`

## Deployment

Runs on the GCP VM (`signal-api`, `100.108.7.78`). Must be on `signal-net` Docker network (same as `signal-connection-server`) and use Docker DNS, not Tailscale IP.

```bash
# Copy files to VM
gcloud compute scp signal_accumulator/* signal-api:~/signal_accumulator/ --zone us-central1-a --tunnel-through-iap

# On the VM: build and redeploy
cd ~/signal_accumulator
sudo docker build -t signal-accumulator .
sudo docker stop signal-accumulator && sudo docker rm signal-accumulator
sudo docker run -d --name signal-accumulator \
  --restart=unless-stopped \
  --network signal-net \
  -p 100.108.7.78:8081:8081 \
  -v signal-accumulator-data:/data \
  -e SIGNAL_WS_URL=ws://signal-connection-server:8080 \
  signal-accumulator

# Verify
sudo docker logs --tail 10 signal-accumulator
```
